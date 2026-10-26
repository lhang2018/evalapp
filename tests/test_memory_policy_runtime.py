"""Persisted privacy scope controls must constrain actual retrieval and prompts."""
import json

import pytest

from evoagent.memory.context import ContextAssembler
from evoagent.memory.manifest import AccessGrant, MemoryAccessError, MemoryIdentity
from evoagent.memory.store import Credibility, MemoryItem, MemoryStore, Scope
from evoagent.memory.user_control import MemoryControlError, MemoryControlService


@pytest.fixture
def store(tmp_path):
    value = MemoryStore(str(tmp_path / "memory.db"))
    yield value
    value.close()


def seed(store, text, scope=Scope.SHARED, *, identity=None):
    return store.write(MemoryItem(text, scope=scope, applicable_domains=["general"],
                                 status=Credibility.VERIFIED),
                       require_verification=False, identity=identity)


def policy(store, allow, deny):
    return MemoryControlService(store).set_scope_policy(
        allow=allow, deny=deny, actor="test:owner", reason="local synthetic privacy test")


def test_saved_deny_filters_before_embedding_and_does_not_count_use(store, monkeypatch):
    blocked = seed(store, "alpha knowledge blocked shared")
    allowed = seed(store, "alpha knowledge allowed user", Scope.USER)
    policy(store, ["user"], ["shared"])
    candidates = []
    vectors = store._vectors_for

    def spy(items, model):
        candidates.extend(item.memory_id for item in items)
        return vectors(items, model)

    monkeypatch.setattr(store, "_vectors_for", spy)
    context = ContextAssembler(store).assemble("alpha knowledge", domain="general")
    prompts = "\n".join(message.content for message in context.to_messages())
    assert allowed.content in prompts
    assert blocked.content not in prompts
    assert blocked.memory_id not in candidates
    assert blocked.memory_id not in context.manifest.references()
    assert all(entry.source_id != blocked.memory_id for entry in context.manifest.entries)
    assert store.get(blocked.memory_id).use_count == 0
    assert store.get(allowed.memory_id).use_count == 1
    # Disabled memories remain manageable; disabling does not delete data.
    assert MemoryControlService(store).get_memory(blocked.memory_id)["content"] == blocked.content


def test_allowlist_filters_before_limit_instead_of_starving_allowed_memories(store):
    for index in range(8):
        seed(store, f"alpha knowledge shared {index}")
    allowed = seed(store, "alpha knowledge user", Scope.USER)
    policy(store, ["user"], [])
    assert [item.memory_id for item in store.search("alpha knowledge", limit=1)] == [allowed.memory_id]


def test_empty_allow_does_not_implicitly_enable_other_scopes(store):
    seed(store, "alpha knowledge shared")
    seed(store, "alpha knowledge user", Scope.USER)
    policy(store, [], ["shared"])
    assert store.search("alpha knowledge") == []


def test_existing_assembler_and_reopened_store_observe_policy_changes(store, tmp_path):
    item = seed(store, "alpha knowledge durable")
    assembler = ContextAssembler(store)
    assert item.content in " ".join(assembler.assemble("alpha knowledge").memories)
    policy(store, ["user"], ["shared"])
    assert assembler.assemble("alpha knowledge").memories == []
    other = MemoryStore(str(tmp_path / "memory.db"))
    try:
        assert ContextAssembler(other).assemble("alpha knowledge").memories == []
        policy(other, ["shared"], [])
        assert item.content in " ".join(assembler.assemble("alpha knowledge").memories)
    finally:
        other.close()


def test_grant_does_not_override_disabled_scope(store):
    me = MemoryIdentity(user_id="alice", expert_id="general")
    other = MemoryIdentity(user_id="bob", expert_id="general")
    item = seed(store, "alpha knowledge foreign", Scope.EXPERT, identity=other)
    grant = AccessGrant(("expert",), other, "test:owner", "explicit test grant")
    assert item.memory_id in {m.memory_id for m in store.search("alpha knowledge", identity=me, grant=grant)}
    policy(store, ["user"], ["expert"])
    assert store.search("alpha knowledge", identity=me, grant=grant) == []


@pytest.mark.parametrize("field", ["allow_json", "deny_json"])
@pytest.mark.parametrize("raw", ["{", "null", "{}", '"shared"', '["unknown"]', '[[]]'])
def test_malformed_persisted_policy_refuses_retrieval_instead_of_allowing(store, raw, field):
    seed(store, "alpha knowledge private")
    policy(store, ["user"], ["shared"])
    with store._lock:
        store._conn.execute(f"UPDATE memory_scope_policy SET {field}=? WHERE id=1", (raw,))
        store._conn.commit()
    with pytest.raises(MemoryAccessError):
        ContextAssembler(store).assemble("alpha knowledge")


@pytest.mark.parametrize("allow,deny", [([["user"]], []), ([1], []), (["user"], [None])])
def test_bad_policy_members_are_control_errors_and_preserve_previous_policy(store, allow, deny):
    control = MemoryControlService(store)
    before = control.get_scope_policy()
    with pytest.raises(MemoryControlError):
        control.set_scope_policy(allow=allow, deny=deny, actor="test:owner", reason="invalid")
    assert control.get_scope_policy() == before


def test_no_saved_policy_preserves_legacy_store_and_empty_retrieval(store):
    assert store.search("alpha knowledge") == []
    item = seed(store, "alpha knowledge default")
    assert item.memory_id in {m.memory_id for m in store.search("alpha knowledge")}
    assert MemoryControlService(store).get_scope_policy()["all_layers"] is True


def test_explicit_deny_wins_for_legacy_overlapping_policy(store):
    seed(store, "alpha knowledge shared")
    policy(store, ["shared"], [])
    with store._lock:
        store._conn.execute("UPDATE memory_scope_policy SET deny_json=? WHERE id=1", ('["shared"]',))
        store._conn.commit()
    assert store.search("alpha knowledge") == []


def test_empty_retrieval_policy_does_not_bypass_identity_validation(store):
    policy(store, [], ["shared"])
    with pytest.raises(MemoryAccessError):
        store.search("alpha knowledge", scope=Scope.PROJECT, scope_key="foreign",
                     identity=MemoryIdentity(user_id="alice", project_id="mine"))


def test_old_database_without_policy_table_is_upgraded_on_reopen(store, tmp_path):
    item = seed(store, "alpha knowledge legacy database")
    with store._lock:
        store._conn.execute("DROP TABLE memory_scope_policy")
        store._conn.commit()
    other = MemoryStore(str(tmp_path / "memory.db"))
    try:
        assert item.memory_id in {m.memory_id for m in other.search("alpha knowledge")}
        assert MemoryControlService(other).get_scope_policy()["all_layers"] is True
    finally:
        other.close()


def test_real_controller_uses_saved_policy_in_model_messages_and_persisted_manifest(tmp_path, monkeypatch):
    import threading
    import urllib.request
    from evoagent.api.server import ApiServer
    from evoagent.app import App
    from evoagent.core.task import TaskStatus

    app = App(data_dir=str(tmp_path / "app"))
    try:
        blocked = seed(app.memory, "alpha knowledge blocked runtime")
        seen = []
        run = app.react.run

        def capture(messages, *args, **kwargs):
            seen.append("\n".join(message.content for message in messages))
            return run(messages, *args, **kwargs)

        monkeypatch.setattr(app.react, "run", capture)
        first = app.submit("alpha knowledge", intent="general", domain="general")
        assert first.task_status == TaskStatus.SUCCEEDED, first.failure_reason
        assert seen and any(blocked.content in text for text in seen)
        seen.clear()
        app.config.api_admin_token = "synthetic-local-test-admin"
        server = ApiServer(app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.port}/memory/scope-policy", method="PUT",
                headers={"Authorization": "Bearer synthetic-local-test-admin",
                         "Content-Type": "application/json"},
                data=json.dumps({"allow": ["user"], "deny": ["shared"],
                                 "actor": "test:owner", "reason": "privacy"}).encode())
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
                assert json.load(response)["ok"] is True
        finally:
            server.shutdown()
            thread.join(timeout=5)
        task = app.submit("alpha knowledge", intent="general", domain="general")
        assert task.task_status == TaskStatus.SUCCEEDED, task.failure_reason
        assert seen and all(blocked.content not in text for text in seen)
        manifests = app.db.list_context_manifests(task.task_id, task.current_run_id)
        assert manifests
        assert all(entry["source_id"] != blocked.memory_id for record in manifests
                   for entry in record["manifest"]["entries"] if entry["kind"] == "memory")
        assert app.memory.get(blocked.memory_id).use_count == 1
    finally:
        app.memory.close()
        app.db.close()
