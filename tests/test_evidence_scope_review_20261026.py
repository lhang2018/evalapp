"""Negative regressions for evidence API and principal policy migrations."""
import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.task import Task, TaskStatus
from evoagent.memory.scope_policy import POLICY_TABLE, ScopePolicyError, ScopePolicyStore
from evoagent.observability.repository import deletion_reconciliation


@pytest.fixture
def api(tmp_path):
    app = App(data_dir=str(tmp_path))
    app.config.api_admin_token = "review-admin"
    app.config.api_token = "review-operator"
    task = Task(user_goal="review", task_status=TaskStatus.SUCCEEDED)
    app.db.save_task(task)
    app.bus.emit(event_type="review_event", task_id=task.task_id, payload={})
    server = ApiServer(app, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.port}"

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method,
            headers={"Authorization": "Bearer review-admin", "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    yield app, task.task_id, call
    server.shutdown()
    app.db.close()


def test_execution_requires_preview_digest(api):
    app, task_id, call = api
    status, _ = call("POST", "/evidence/deletion", {
        "task_id": task_id, "reason": "review", "dry_run": False})
    assert status == 400
    assert app.bus._conn.execute("SELECT COUNT(*) FROM runtime_events WHERE task_id=?",
                                 (task_id,)).fetchone()[0] == 1


@pytest.mark.parametrize("dry_run", [None, 0, ""])
def test_non_boolean_dry_run_never_deletes(api, dry_run):
    app, task_id, call = api
    status, _ = call("POST", "/evidence/deletion", {
        "task_id": task_id, "reason": "review", "dry_run": dry_run})
    assert status == 400
    assert app.bus._conn.execute("SELECT COUNT(*) FROM runtime_events WHERE task_id=?",
                                 (task_id,)).fetchone()[0] == 1


def test_export_privacy_flags_require_boolean(api):
    _, task_id, call = api
    status, _ = call("POST", "/evidence/export", {
        "task_id": task_id, "reason": "review", "allow_restricted": "false"})
    assert status == 400


def test_export_audit_remains_verifiable_after_more_than_1000_entries(api):
    app, task_id, call = api
    status, result = call("POST", "/evidence/export", {"task_id": task_id, "reason": "review"})
    assert status == 200
    # Main audit table only; no need to create 1001 unrelated runtime events.
    with app.db._lock:
        app.db._conn.executemany(
            "INSERT INTO audit_log (audit_id,at,actor,action,target,outcome,detail) VALUES (?,?,?,?,?,?,?)",
            [(f"review_noise_{i}", 10**12 + i, "noise", "noise", "", "ok", "{}") for i in range(1001)])
        app.db._conn.commit()
    status, _ = call("GET", result["download"])
    assert status == 200


def test_unreadable_event_repository_does_not_return_partial_success(api, monkeypatch):
    app, task_id, call = api
    unavailable = sqlite3.connect(":memory:", check_same_thread=False)
    unavailable.close()
    with monkeypatch.context() as patch:
        patch.setattr(app.bus, "_conn", unavailable)
        status, _ = call("GET", f"/evidence/tasks/{task_id}")
        assert status == 503
        reconciliation = deletion_reconciliation(app.db, task_id, events_conn=unavailable)
        assert reconciliation["residuals"]["runtime_events"] is None
        assert reconciliation["fully_deleted"] is False
        assert "runtime_events" in reconciliation["unknown_layers"]


def legacy(conn, allow):
    conn.execute("INSERT OR REPLACE INTO memory_scope_policy VALUES (1,?,?,?,?,?)",
                 (json.dumps(allow), "[]", "review", "seed", 1))
    conn.commit()


@pytest.fixture
def scope():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = ScopePolicyStore(conn, clock=lambda: 100)
    legacy(conn, ["user"])
    yield conn, store
    conn.close()


def test_rollback_refuses_changed_legacy_policy(scope):
    conn, store = scope
    store.migrate_legacy(authenticated="review", dry_run=False, reason="seed")
    legacy(conn, ["user", "shared"])
    with pytest.raises(ScopePolicyError):
        store.rollback_migration(authenticated="review", reason="rollback")
    assert store.effective_policy("review")["allow"] == ["user"]


def test_disabled_partition_is_not_reported_as_already_migrated(scope):
    _, store = scope
    store.set_principal_policy(authenticated="review", multi_principal=False,
                               allow=["user"], deny=[], actor="review", reason="seed")
    with pytest.raises(ScopePolicyError):
        store.migrate_legacy(authenticated="review", dry_run=False, reason="apply")


def test_repeated_migration_preserves_all_apply_receipts(scope):
    _, store = scope
    store.migrate_legacy(authenticated="review", dry_run=False, reason="first")
    store.rollback_migration(authenticated="review", reason="rollback")
    store.migrate_legacy(authenticated="review", dry_run=False, reason="second")
    history = store.migration_history("review")["migrations"]
    assert [row["action"] for row in history] == ["apply", "rollback", "apply"]
    assert len({row["migration_id"] for row in history}) == 3


def test_policy_actor_comes_from_transport_identity(api):
    _, _, call = api
    status, result = call("PUT", "/memory/principal-policy", {
        "multi_principal": True, "allow": ["user"], "deny": [],
        "actor": "someone-else", "reason": "review"})
    assert status == 200
    assert result["policy"]["actor"] == "api:admin"


def test_failed_partition_write_rolls_back_its_migration_receipt(scope):
    conn, store = scope
    conn.execute("CREATE TRIGGER review_refuse_partition BEFORE INSERT ON memory_principal_scope_policy "
                 "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.migrate_legacy(authenticated="review", dry_run=False, reason="probe")
    assert not conn.in_transaction
    assert store.migration_history("review")["migrations"] == []
    assert store.principal_policy("review") is None


def test_migration_preconditions_are_inside_writer_transaction(scope, monkeypatch):
    conn, store = scope
    original = store.legacy_policy
    checked = []

    def checked_legacy():
        checked.append(conn.in_transaction)
        return original()

    monkeypatch.setattr(store, "legacy_policy", checked_legacy)
    store.migrate_legacy(authenticated="review", dry_run=False, reason="probe")
    store.rollback_migration(authenticated="review", reason="probe")
    assert checked and all(checked)


def test_rollback_does_not_discard_later_same_scope_policy_update(scope):
    _, store = scope
    store.migrate_legacy(authenticated="review", dry_run=False, reason="probe")
    store.set_principal_policy(authenticated="review", multi_principal=True,
                               allow=["user"], deny=[], actor="review", reason="new decision")
    with pytest.raises(ScopePolicyError):
        store.rollback_migration(authenticated="review", reason="probe")
    assert store.principal_policy("review")["version"] == 2


@pytest.mark.parametrize("dry_run", [None, 0, ""])
def test_policy_migration_rejects_non_boolean_dry_run(scope, dry_run):
    _, store = scope
    with pytest.raises(ScopePolicyError):
        store.migrate_legacy(authenticated="review", dry_run=dry_run, reason="probe")
    assert store.principal_policy("review") is None
