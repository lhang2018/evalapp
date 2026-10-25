"""Negative probes for new artifact, binding and routing boundaries."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from evoagent.api.artifacts_api import preview_artifact
from evoagent.core.persistence import Database
from evoagent.delivery.artifacts import ArtifactError, ArtifactStore
from evoagent.planning.binding import ActionBindings
from evoagent.routing.mode_router import ModeRouter, STABLE, DYNAMIC
from evoagent.workflows.models import WorkflowDef, WorkflowNode

@pytest.mark.parametrize("task_id,node_id",[
    ("../outside","report"),("task","../../outside"),("task","ABSOLUTE"),
    ("task",".."),("task","node/child"),
])
def test_artifact_identifiers_cannot_escape_store(tmp_path,task_id,node_id):
    store=ArtifactStore(str(tmp_path/"data"))
    if node_id == "ABSOLUTE":
        node_id = str(tmp_path / "controlled-absolute-target")
    with pytest.raises(ArtifactError):
        store.save(task_id,node_id,"text",b"must stay inside")

def test_artifact_read_rejects_forged_outside_metadata(tmp_path):
    outside=tmp_path/"private.txt";outside.write_bytes(b"private bytes")
    store=ArtifactStore(str(tmp_path/"data"))
    meta={"artifact_id":"fake","path":str(outside),"sha256":hashlib.sha256(outside.read_bytes()).hexdigest(),"bytes":outside.stat().st_size}
    assert store.read(meta) is None
    assert not store.verify(meta)

def test_artifact_versions_remain_readable(tmp_path):
    store=ArtifactStore(str(tmp_path))
    old=store.save("t","report","text",b"old")
    new=store.save("t","report","text",b"new")
    assert old["path"]!=new["path"]
    assert store.read(old)==b"old" and store.read(new)==b"new"

def test_missing_artifact_cannot_be_kept_by_digest_alone(tmp_path):
    store=ArtifactStore(str(tmp_path))
    old=store.save("t","a","text",b"same")
    (store.root/old["path"]).unlink()
    assert store.diff_for_rebuild([old],{"a":b"same"})=={"rebuild":["a"],"keep":[]}

def test_verified_read_returns_the_bytes_it_hashed(tmp_path,monkeypatch):
    from contextlib import contextmanager
    store=ArtifactStore(str(tmp_path))
    meta=store.save("t","a","text",b"trusted")
    target=(store.root/meta["path"]).resolve()
    original=Path.open
    changed=False
    @contextmanager
    def racing_open(path,*args,**kwargs):
        nonlocal changed
        with original(path,*args,**kwargs) as handle:
            yield handle
        mode=args[0] if args else kwargs.get("mode","r")
        if path.resolve()==target and "r" in mode and not changed:
            changed=True
            with original(target,"wb") as handle:handle.write(b"changed")
    monkeypatch.setattr(Path,"open",racing_open)
    assert store.read(meta)==b"trusted"

def test_artifact_task_scope_and_streamed_preview(tmp_path):
    store=ArtifactStore(str(tmp_path))
    meta=store.save("task_a","report","text",b"a"*5000)
    assert store.read(meta,task_id="task_b") is None
    assert store.read(meta,limit=4096,task_id="task_a")==b"a"*4096
    assert meta["bytes"]==5000 and store.verify(meta)

def test_binary_preview_is_base64_not_hex(tmp_path):
    store=ArtifactStore(str(tmp_path))
    content=b"\x00\x01\xab\xff"
    meta=store.save("t","img","binary",content)
    app=SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)),
                        db=SimpleNamespace(get_task=lambda _:SimpleNamespace(result_evidence=[meta])))
    status,payload=preview_artifact(SimpleNamespace(app=app,path=f"/tasks/t/artifacts/{meta['artifact_id']}/preview",
                               json=lambda status,payload:(status,payload)))
    assert status==200
    assert payload["base64"]==base64.b64encode(content).decode("ascii")

@pytest.fixture
def bindings():
    db=Database(":memory:")
    yield ActionBindings(db)
    db.close()

def test_one_effect_contested_by_two_intents_is_not_guessed(bindings):
    bindings.record_intent("t","s1","write",now=1)
    bindings.record_intent("t","s2","write",now=2)
    report=bindings.heal_pending("t",[{"task_id":"t","tool_id":"write","effect_id":"e1"}])
    assert report["healed"]=={} and len(report["ambiguous"])==2
    assert bindings.get("e1") is None

def test_resolve_rolls_back_binding_when_intent_update_fails(bindings):
    intent=bindings.record_intent("t","s","write")
    conn=bindings.db._conn
    conn.execute("CREATE TRIGGER review_fail_intent BEFORE UPDATE ON action_binding_intents BEGIN SELECT RAISE(ABORT,'injected'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        bindings.resolve_intent(intent,"e")
    assert bindings.get("e") is None
    assert bindings.intent(intent)["effect_id"] is None

def test_intent_identity_does_not_collide_across_tools_or_runs(bindings):
    ids={bindings.record_intent("t","s",tool,run_id=run,now=1)
         for tool,run in [("write","run1"),("send","run1"),("write","run2")]}
    assert len(ids)==3

def test_record_intent_does_not_commit_callers_transaction(bindings):
    conn=bindings.db._conn
    conn.execute("CREATE TABLE review_pending(x TEXT)");conn.commit()
    conn.execute("INSERT INTO review_pending VALUES ('not committed')")
    try:
        with pytest.raises(ValueError):
            bindings.record_intent("t","s","write")
    finally:conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM review_pending").fetchone()[0]==0

@pytest.mark.parametrize("run,created",[("other_run",3),("expected_run",1)])
def test_healing_does_not_claim_another_run_or_earlier_effect(bindings,run,created):
    bindings.record_intent("t","s","write",run_id="expected_run",now=2)
    result=bindings.heal_pending("t",[{"effect_id":"e","task_id":"t",
        "tool_id":"write","run_id":run,"created_at":created}])
    assert result["healed"]=={} and bindings.get("e") is None

def test_concurrent_resolve_cannot_replace_an_intents_effect(bindings):
    intent=bindings.record_intent("t","s","write")
    def attempt(effect):
        try:
            bindings.resolve_intent(intent,effect)
            return effect
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,["e1","e2"]))
    winner=[r for r in results if r is not None]
    assert len(winner)==1
    assert bindings.intent(intent)["effect_id"]==winner[0]
    assert bindings.count("t")==1

def budget(limits,usage=None):
    return SimpleNamespace(snapshot=lambda:{"limits":limits,"usage":usage or {}},exceeded=lambda:None)

def workflow():
    return WorkflowDef(workflow_id="review",version="1",
        nodes=[WorkflowNode(node_id="a",tool="calculate",args={"expression":"1+1"}),
               WorkflowNode(node_id="b",tool="calculate",args={"expression":"2+2"})])

def test_tool_only_workflow_does_not_need_remaining_model_calls():
    router=ModeRouter(lambda _:workflow())
    selection=router.decide(goal="calc",intent="general",workflow_ids=["review"],
        budget=budget({"max_model_calls":1,"max_tool_calls":5},{"model_calls":1}))
    assert selection.mode==STABLE

def test_zero_tool_budget_is_not_ignored():
    router=ModeRouter(lambda _:workflow())
    selection=router.decide(goal="calc",intent="general",workflow_ids=["review"],
        budget=budget({"max_model_calls":10,"max_tool_calls":0}))
    assert selection.mode==DYNAMIC

def test_router_uses_actual_tool_usage_keys():
    router=ModeRouter(lambda _:workflow())
    selection=router.decide(goal="calc",intent="general",workflow_ids=["review"],
        budget=budget({"max_model_calls":10,"max_tool_calls":2},{"tool_calls":1}))
    assert selection.mode==DYNAMIC

@pytest.fixture
def retry_app(tmp_path):
    from evoagent.app import App
    app=App(data_dir=str(tmp_path))
    yield app
    app.memory.close();app.db.close()

def scoped_retry(app,tmp_path):
    from evoagent.core.task import Task,TaskStatus
    from evoagent.tools.contract import ToolContract
    parent=Task(user_goal="same goal",task_status=TaskStatus.FAILED,
        expected_output="report",target_object="project",complexity="moderate",
        requires_network=True,open_questions=["source?"],
        required_tools=["write"],required_permissions=["file"],workflow_suitable=False)
    parent.runtime_manifest["principal_id"]="user:alice"
    app.db.save_task(parent)
    policy={"tools":["write"],"permissions":["file"],"path_roots":[str(tmp_path/"allowed")],"principal":"user:alice"}
    parent.authorization_ref=app.tools.authorizations.issue(parent.task_id,policy,actor="user:alice")
    app.db.save_task(parent)
    child=app.controller._build_retry(parent);app.db.save_task(child)
    contract=ToolContract(tool_id="write",description="write",input_schema={},
        permission_requirements=["file"],side_effect_level="local")
    return parent,child,contract

def test_retry_preserves_contract_identity_and_authorization_ceiling(retry_app,tmp_path):
    parent,child,contract=scoped_retry(retry_app,tmp_path)
    for field in ("expected_output","target_object","complexity","requires_network",
                  "open_questions","required_tools","required_permissions","workflow_suitable"):
        assert getattr(child,field)==getattr(parent,field)
    assert child.runtime_manifest["principal_id"]=="user:alice"
    grant=retry_app.tools.authorizations.current(child.task_id)
    assert grant["policy"]==retry_app.tools.authorizations.current(parent.task_id)["policy"]
    retry_app.tools.authorizations.check(child.task_id,contract,
        {"path":str(tmp_path/"allowed"/"a.txt")},principal="user:alice")
    with pytest.raises(PermissionError):
        retry_app.tools.authorizations.check(child.task_id,contract,
            {"path":str(tmp_path/"outside.txt")},principal="user:alice")

def test_revoking_source_authorization_invalidates_retry(retry_app,tmp_path):
    parent,child,contract=scoped_retry(retry_app,tmp_path)
    retry_app.tools.authorizations.revoke(parent.authorization_ref)
    with pytest.raises(PermissionError):
        retry_app.tools.authorizations.check(child.task_id,contract,
            {"path":str(tmp_path/"allowed"/"a.txt")},principal="user:alice")
    with pytest.raises(PermissionError):
        retry_app.controller._build_retry(parent)

def test_explicit_source_revision_change_invalidates_automatic_retry(retry_app,tmp_path):
    parent,child,contract=scoped_retry(retry_app,tmp_path)
    retry_app.tools.authorizations.issue(parent.task_id,
        {"tools":[],"permissions":[]},actor="user:alice")
    with pytest.raises(PermissionError):
        retry_app.tools.authorizations.check(child.task_id,contract,
            {"path":str(tmp_path/"allowed"/"a.txt")},principal="user:alice")
