"""ALN-05-I: real controller/SQLite/API identity consumers, not mock grants."""
import copy
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
import pytest
from evoagent.app import App
from evoagent.api.server import ApiServer
from evoagent.core.persistence import Database
from evoagent.core.task import Task, TaskStatus
from evoagent.memory.binding import MemoryBindingAuthority
from evoagent.memory.manifest import MemoryAccessError, MemoryIdentity
from evoagent.memory.store import MemoryItem, MemoryKind, Scope, Credibility
from evoagent.models.mock import MockModel
from evoagent.models.base import Action

POLICIES = {
    "local:host": [dict(user_id="alice", project_id="p1", session_id="s1")],
    "api:operator": [dict(user_id="alice", project_id="p1", session_id="s1"),
                     dict(user_id="alice", project_id="p2", session_id="s2"),
                     dict(user_id="alice", project_id="p1", session_id="s3")],
    "api:admin": [dict(user_id="bob", project_id="p1", session_id="s1")],
}


@pytest.fixture
def app(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(dict(memory_bindings=POLICIES)), encoding="utf-8")
    value = App(str(config), data_dir=str(tmp_path / "data"))
    yield value
    value.memory.close(); value.db.close()


def _identity(user="alice", project="p1", session="s1", task="prior", expert="general"):
    return MemoryIdentity(user, project, session, task, expert)


def _write(app, content, identity, *, scope=Scope.EXPERT, fact=""):
    return app.memory.write(MemoryItem(content, scope=scope, status=Credibility.VERIFIED,
        kind=MemoryKind.DOMAIN_KNOWLEDGE, applicable_domains=["general"], evidence={"test":True}),
        identity=identity, fact_key=fact)


def _run(app, **kw):
    task = app.submit("alpha knowledge", intent="general", domain="general", **kw)
    assert task.task_status == TaskStatus.SUCCEEDED, task.failure_reason
    return task


def _refs(app, task):
    records = app.db.list_context_manifests(task.task_id, task.current_run_id)
    assert records
    return {e["source_id"] for e in records[-1]["manifest"]["entries"] if e["kind"] == "memory" and e["included"]}


def test_real_controller_reads_only_current_owner_and_passes_same_identity_to_write(app, monkeypatch):
    mine = _write(app, "alpha knowledge mine", _identity())
    foreign = [_write(app, "alpha knowledge other user", _identity("bob")),
               _write(app, "alpha knowledge other project", _identity(project="p2")),
               _write(app, "alpha knowledge other session", _identity(session="s2")),
               _write(app, "alpha knowledge other hive", _identity(expert="travel"))]
    reads, writes, prompts = [], [], []
    search, write, run = app.memory.search, app.memory.write, app.react.run
    def spy_search(*a, **kw):
        reads.append(kw["identity"]); return search(*a, **kw)
    def spy_write(*a, **kw):
        writes.append(kw["identity"]); return write(*a, **kw)
    def spy_run(messages, *a, **kw):
        prompts.append("\n".join(m.content for m in messages)); return run(messages,*a,**kw)
    monkeypatch.setattr(app.memory,"search",spy_search)
    monkeypatch.setattr(app.memory,"write",spy_write)
    monkeypatch.setattr(app.react,"run",spy_run)
    task = _run(app)
    assert reads and writes and reads[0] == writes[-1]
    assert reads[0].task_id == task.task_id and reads[0].expert_id == "general"
    assert "alpha knowledge mine" in prompts[0]
    assert all(m.content not in prompts[0] for m in foreign)
    assert mine.memory_id in _refs(app, task)
    assert not ({m.memory_id for m in foreign} & _refs(app, task))
    stored = [m for m in app.memory.list_items(identity=reads[0]) if m.source == "runtime"]
    assert stored and stored[0].user_id == "alice" and stored[0].task_id == task.task_id


@pytest.mark.parametrize("changed", [dict(user_id="bob"), dict(project_id="p2"), dict(session_id="s2")])
def test_same_hive_same_text_does_not_cross_owner_dedupe_or_conflicts(app, changed):
    ident = _identity()
    first = _write(app, "alpha knowledge exact", ident, fact="fact")
    other = MemoryIdentity(**{**ident.to_dict(), **changed})
    second = _write(app, "alpha knowledge exact", other, fact="fact")
    third = _write(app, "alpha knowledge changed", other, fact="fact")
    assert first.memory_id != second.memory_id
    assert not app.memory.get(first.memory_id).has_conflict
    assert app.memory.get(third.memory_id).has_conflict
    assert app.memory.get(first.memory_id, identity=ident)
    with pytest.raises(MemoryAccessError):
        app.memory.get(first.memory_id, identity=other)


def test_legacy_unowned_private_hive_not_silently_granted(app):
    private = app.memory.write(MemoryItem("alpha knowledge legacy", scope=Scope.EXPERT,
        scope_key="general", applicable_domains=["general"]))
    task = _run(app)
    assert private.memory_id not in _refs(app, task)


@pytest.mark.parametrize("claims", [dict(user_id="bob"), dict(task_id="foreign"), dict(expert_id="travel"), dict(project_id="unknown"), dict(grant={"actor":"admin"})])
def test_untrusted_identity_claims_are_rejected_before_execution(app, claims):
    before = len(app.db.list_tasks())
    with pytest.raises((PermissionError, ValueError)):
        _run(app, memory_context=claims)
    assert len(app.db.list_tasks()) == before


def test_durable_binding_survives_reopen_and_rejects_missing_or_changed_snapshot(app):
    task = _run(app)
    other = Database(app.db.path)
    try:
        authority = MemoryBindingAuthority(other, POLICIES)
        restored = other.get_task(task.task_id)
        identity, _ = authority.resolve(restored, "general")
        assert identity == MemoryIdentity(**{**task.memory_identity, "expert_id":"general"})
        for field in ("memory_identity", "memory_binding_ref"):
            forged = copy.deepcopy(restored)
            setattr(forged, field, {} if field == "memory_identity" else None)
            with pytest.raises(PermissionError): authority.resolve(forged)
            with pytest.raises(PermissionError): other.save_task(forged)
    finally:
        other.close()


def test_old_task_gets_private_stable_legacy_binding_instead_of_broad_read(app):
    old = Task.from_dict(dict(task_id="old-task", user_goal="alpha knowledge"))
    app.db.save_task(old)
    identity, _ = app.memory_bindings.resolve(old, "general")
    assert identity.user_id == "legacy:old-task"
    again = app.db.get_task(old.task_id)
    assert app.memory_bindings.resolve(again, "general")[0] == identity
    assert again.memory_binding_ref == old.memory_binding_ref


def test_failed_retry_inherits_owner_with_new_task_id(app):
    parent = _run(app)
    parent.task_status = TaskStatus.FAILED
    parent.failure_reason = "injected failure"
    app.db.save_task(parent)
    retried = app.controller.resume_task(parent.task_id)
    assert retried.task_status == TaskStatus.SUCCEEDED, retried.failure_reason
    assert retried.task_id != parent.task_id
    assert retried.memory_identity["user_id"] == parent.memory_identity["user_id"]
    assert retried.memory_identity["task_id"] == retried.task_id


def test_manifest_persists_actual_refs_compression_and_no_sensitive_bodies(app):
    mem = _write(app, "alpha knowledge sensitive memory正文", _identity())
    task = _run(app, constraints=["confidential hard constraint"], acceptance=[{"type":"result_nonempty", "private":"confidential acceptance"}])
    record = app.db.list_context_manifests(task.task_id)[0]
    manifest = record["manifest"]
    assert manifest["manifest_id"] == task.runtime_manifest["context_manifest_ref"]
    assert mem.memory_id in _refs(app, task)
    text = json.dumps(manifest, ensure_ascii=False)
    assert all(secret not in text for secret in [mem.content,"confidential hard constraint","confidential acceptance"])
    assert manifest["compression"]["hard_constraints_retained"]
    assert manifest["assembly"]["identity"]["task_id"] == task.task_id
    assert record["run_id"] == task.current_run_id


def test_grant_is_server_issued_scoped_revocable_and_not_write_authority(app):
    foreign = _write(app, "alpha knowledge external", _identity("bob"))
    grantee = _identity(task="")
    ref = app.memory_bindings.issue_grant("api:operator", grantee, _identity("bob"), ["expert"],
        reason="incident inspection", expires_at=time.time()+60)
    task = _run(app, principal_id="api:operator", memory_context=POLICIES["api:operator"][0], memory_grant_ref=ref)
    assert foreign.memory_id in _refs(app, task)
    assert any(m.user_id == "alice" for m in app.memory.list_items() if m.task_id == task.task_id)
    with pytest.raises(PermissionError):
        _run(app, principal_id="api:operator", memory_context=POLICIES["api:operator"][1], memory_grant_ref=ref)
    with pytest.raises(PermissionError): _run(app, memory_grant_ref=ref)
    app.memory_bindings.revoke_grant(ref)
    with pytest.raises(PermissionError): app.memory_bindings.resolve(task)
    with pytest.raises(PermissionError): _run(app, principal_id="api:operator", memory_grant_ref=ref)


@pytest.mark.parametrize("ref", ["forged", {"actor":"admin", "scopes":["expert"]}, ""])
def test_forged_grant_not_accepted(app, ref):
    with pytest.raises(PermissionError): _run(app, memory_grant_ref=ref)


@pytest.fixture
def api(app):
    app.config.api_token="operator-secret"; app.config.api_admin_token="admin-secret"
    server=ApiServer(app,host="127.0.0.1",port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def request(path, body=None, token="operator-secret"):
        req=urllib.request.Request(f"http://127.0.0.1:{server.port}"+path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
        try: response=urllib.request.urlopen(req,timeout=10)
        except urllib.error.HTTPError as e: response=e
        with response:return response.status,json.loads(response.read())
    yield request
    server.shutdown();thread.join(5)


def _wait(app, task_id):
    for _ in range(200):
        task=app.db.get_task(task_id)
        if task and task.is_terminal():
            app.controller._wait_for_previous_run(task_id)
            return task
        time.sleep(.02)
    pytest.fail("Task did not finish")


def test_actual_api_binds_authenticated_subject_and_filters_task_reads(app, api):
    code, out=api("/tasks",dict(goal="alpha knowledge",intent="general"))
    assert code==202
    mine=_wait(app,out["task_id"])
    code, out=api("/tasks",dict(goal="alpha knowledge",intent="general"),token="admin-secret")
    assert code==202
    foreign=_wait(app,out["task_id"])
    assert mine.memory_identity["user_id"]=="alice"
    assert foreign.memory_identity["user_id"]=="bob"
    assert api(f"/tasks/{foreign.task_id}")[0]==403
    assert api(f"/tasks/{foreign.task_id}/context-manifests")[0]==403
    assert api(f"/tasks/{mine.task_id}/context-manifests")[0]==200
    assert {t["task_id"] for t in api("/tasks")[1]["tasks"]}=={mine.task_id}
    assert api("/memory")[0]==403


@pytest.mark.parametrize("forged", [dict(user_id="bob"),dict(principal_id="api:admin"),dict(grant={"actor":"admin"}),dict(memory_context={"user_id":"bob"}),dict(memory_context={"expert_id":"travel"}),dict(memory_grant_ref="forged")])
def test_api_rejects_forged_identity_and_grants(app, api, forged):
    before=len(app.db.list_tasks())
    assert api("/tasks",dict(goal="alpha knowledge",intent="general",**forged))[0]==403
    assert len(app.db.list_tasks())==before


def test_request_replay_cannot_bypass_identity_or_principal_binding(app, api):
    body=dict(goal="alpha knowledge",intent="general",client_request_id="same-request-0001")
    code,out=api("/tasks",body)
    assert code==202
    _wait(app,out["task_id"])
    assert api("/tasks",body)[1]["deduplicated"] is True
    assert api("/tasks",dict(body,memory_context=POLICIES["api:operator"][1]))[0]==403
    assert api("/tasks",body,token="admin-secret")[0]==403


class WaitingModel(MockModel):
    def suggest_action(self, messages, available_tools):
        if any(m.role == "user" and m.content == "continue" for m in messages):
            return Action(kind="finish", answer="resumed with original identity")
        return Action(kind="request_user", answer="continue?")


def test_actual_restarted_controller_preserves_identity_manifest_and_run_provenance(app):
    app.controller.router.route = lambda *a, **k: WaitingModel()
    task = app.submit("alpha knowledge", intent="general", domain="general")
    assert task.task_status == TaskStatus.WAITING_USER
    first_run = task.current_run_id
    reopened = App(str(Path(app.config.data_dir).parent / "config.json"), data_dir=str(app.config.data_dir))
    try:
        reopened.controller.router.route = lambda *a, **k: WaitingModel()
        restored = reopened.db.get_task(task.task_id)
        resumed, accepted = reopened.controller.answer_task(task.task_id, restored.waiting_request["request_id"], "continue")
        assert accepted and resumed.task_status == TaskStatus.SUCCEEDED, resumed.failure_reason
        assert resumed.memory_identity == task.memory_identity
        assert resumed.memory_binding_ref == task.memory_binding_ref
        records = reopened.db.list_context_manifests(task.task_id)
        assert {r["run_id"] for r in records} == {first_run, resumed.current_run_id}
        assert all(r["manifest"]["assembly"]["identity"]["user_id"] == "alice" for r in records)
    finally:
        reopened.memory.close(); reopened.db.close()


def test_grant_expiry_blocks_restore_instead_of_reusing_checkpoint_context(app):
    now = [time.time()]
    app.memory_bindings.clock = lambda:now[0]
    ref = app.memory_bindings.issue_grant("local:host", _identity(task=""), _identity("bob"), ["expert"],
        reason="temporary", expires_at=now[0]+1)
    app.controller.router.route = lambda *a, **k: WaitingModel()
    task = app.submit("alpha knowledge", intent="general", memory_grant_ref=ref)
    assert task.task_status == TaskStatus.WAITING_USER
    now[0] += 2
    with pytest.raises(PermissionError):
        app.controller.answer_task(task.task_id, task.waiting_request["request_id"], "continue")


def test_all_five_identity_scopes_reach_the_actual_assembled_context(app, monkeypatch):
    expected = set()
    bind = app.memory_bindings.bind_new
    def seed(task, *a, **kw):
        bind(task,*a,**kw)
        identity = MemoryIdentity(**{**task.memory_identity, "expert_id":"general"})
        for scope in (Scope.USER, Scope.PROJECT, Scope.SESSION, Scope.TASK, Scope.EXPERT):
            expected.add(_write(app,"alpha knowledge "+scope.value,identity,scope=scope).memory_id)
    monkeypatch.setattr(app.memory_bindings,"bind_new",seed)
    task = _run(app)
    assert _refs(app,task) == expected


def test_scope_grant_does_not_leak_other_scope_or_change_writer_owner(app):
    expert = _write(app,"alpha knowledge bob expert",_identity("bob"))
    private = _write(app,"alpha knowledge bob user",_identity("bob"),scope=Scope.USER)
    ref = app.memory_bindings.issue_grant("local:host", _identity(task=""), _identity("bob"), ["expert"],
        reason="expert inspection", expires_at=time.time()+60)
    task = _run(app,memory_grant_ref=ref)
    assert expert.memory_id in _refs(app,task)
    assert private.memory_id not in _refs(app,task)


@pytest.mark.parametrize("choice", [POLICIES["api:operator"][1],POLICIES["api:operator"][2]])
def test_project_and_session_switch_in_actual_controller_never_reads_previous_hive(app, choice):
    previous = _write(app,"alpha knowledge prior session",_identity())
    task = _run(app,principal_id="api:operator",memory_context=choice)
    assert previous.memory_id not in _refs(app,task)
    assert all(task.memory_identity[k] == v for k,v in choice.items())



def test_grant_revoked_after_tool_blocks_next_real_model_call(app):
    from evoagent.tools.contract import ToolContract, ToolResult
    calls=[]
    ref=app.memory_bindings.issue_grant("local:host",_identity(task=""),_identity("bob"),["expert"],
        reason="temporary",expires_at=time.time()+60)
    class RevokingModel(MockModel):
        def suggest_action(self,messages,available_tools):
            calls.append(1)
            return Action(kind="tool_call",tool="current_time")
    def revoke(args):
        app.memory_bindings.revoke_grant(ref)
        return ToolResult(ok=True,data="revoked")
    app.tools.register(ToolContract(tool_id="current_time"),revoke)
    app.controller.router.route=lambda *a,**k:RevokingModel()
    task=app.submit("alpha knowledge",intent="time_query",memory_grant_ref=ref)
    assert task.task_status==TaskStatus.FAILED
    assert len(calls)==1
    assert "Grant" in task.failure_reason


def test_manifest_cannot_be_rebound_or_mutated_and_transaction_remains_usable(app):
    from evoagent.memory.manifest import ContextEntry,ContextManifest
    task=_run(app)
    record=app.db.list_context_manifests(task.task_id)[0]["manifest"]
    manifest=ContextManifest.build(assembly=record["assembly"],budget_tokens=record["budget_tokens"],
        entries=[ContextEntry(**{**e,"conflict_ids":tuple(e["conflict_ids"])}) for e in record["entries"]],
        compression=record["compression"])
    app.db.save_context_manifest(task.task_id,task.current_run_id,manifest)
    app.db.save_context_manifest(task.task_id,task.current_run_id,manifest)
    assert not app.db._conn.in_transaction
    with pytest.raises(PermissionError):app.db.save_context_manifest(task.task_id,"wrong-run",manifest)
    assert not app.db._conn.in_transaction
    manifest.assembly["domain"]="tampered"
    with pytest.raises(ValueError):app.db.save_context_manifest(task.task_id,task.current_run_id,manifest)
    app.db.save_task(task)


def test_two_connections_bind_same_old_task_without_changing_identity(app):
    from concurrent.futures import ThreadPoolExecutor
    a=Task(task_id="racing-old",user_goal="old")
    app.db.save_task(a)
    second=Database(app.db.path)
    try:
        authorities=[app.memory_bindings,MemoryBindingAuthority(second,POLICIES)]
        tasks=[app.db.get_task(a.task_id),second.get_task(a.task_id)]
        with ThreadPoolExecutor(2) as pool:
            outcomes=list(pool.map(lambda pair:pair[0].resolve(pair[1]),zip(authorities,tasks)))
        assert outcomes[0][0]==outcomes[1][0]
        assert tasks[0].memory_binding_ref==tasks[1].memory_binding_ref
    finally:second.close()



def test_reconfigured_principal_cannot_take_over_previous_owner_tasks(app):
    task=_run(app,principal_id="api:operator",memory_context=POLICIES["api:operator"][0])
    changed=copy.deepcopy(POLICIES)
    changed["api:operator"]=[dict(user_id="new-owner",project_id="p1",session_id="s1")]
    authority=MemoryBindingAuthority(app.db,changed)
    with pytest.raises(PermissionError):authority.resolve(task,"general")
    with pytest.raises(PermissionError):authority.assert_principal(task,"api:operator")
    assert not authority.owns(task.task_id,"api:operator")
