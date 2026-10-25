"""Cross-boundary regression probes for the 2026-10-25 delivered slices."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from evoagent.app import App
from evoagent.api.capabilities_api import collect
from evoagent.api.feedback_api import submit_feedback
from evoagent.assets.consumption import ConsumptionError, RuntimeAssetBinder
from evoagent.assets.mapping import expert_to_asset, workflow_to_asset
from evoagent.assets.repository import repository_for
from evoagent.core.permissions import PermissionController
from evoagent.core.task import Task
from evoagent.observability.feedback import FeedbackError, FeedbackStore
from evoagent.tools.contract import ToolContract
from evoagent.workflows.models import WorkflowDef, WorkflowNode

@pytest.fixture
def app(tmp_path):
    value=App(data_dir=str(tmp_path))
    yield value
    value.memory.close()
    value.db.close()

def feedback(app,body):
    return submit_feedback(SimpleNamespace(app=app,body=body,json=lambda status,payload:(status,payload)))

def seed_runs(app):
    for suffix in ("a","b"):
        task=Task(task_id="review_"+suffix,user_goal="review")
        task.runtime_manifest["runtime_manifest_id"]="manifest_"+suffix
        app.db.save_task(task)
        app.db.save_run("run_"+suffix,task.task_id,1,2,"SUCCEEDED",
                        {"runtime_manifest_id":"manifest_"+suffix})

def promote(repo,draft):
    asset=repo.register_draft(draft)
    for state in ("validating","approved","active"):
        asset=repo.transition(asset.asset_id,asset.version,state,evidence_refs=("review-evaluation",))
    return asset

def test_capability_read_never_calls_confirmation_or_writes_approval(app):
    confirmation=Mock(return_value=True)
    notes=Mock()
    app.permissions=PermissionController("confirm",confirmation,SimpleNamespace(note_decision=notes))
    collect(SimpleNamespace(app=app))
    assert confirmation.call_count==0
    assert notes.call_count==0

def test_capability_projection_respects_declared_risk(app):
    app.tools.register(ToolContract(tool_id="review_risky",description="explicit high risk",
        input_schema={"type":"object"},risk_class="critical",health_check=lambda:True),lambda args:None)
    by_id={c["tool_id"]:c for c in collect(SimpleNamespace(app=app))["capabilities"]}
    assert by_id["review_risky"]["state"]=="permission_required"
    assert by_id["review_risky"]["high_risk"]

def test_feedback_rejects_mismatched_task_run(app):
    seed_runs(app)
    status,_=feedback(app,dict(category="bug",text="mismatch",task_id="review_a",run_id="run_b"))
    assert status==400

def test_run_only_feedback_uses_that_runs_task_and_manifest(app):
    seed_runs(app)
    status,result=feedback(app,dict(category="bug",text="run-only",run_id="run_a"))
    assert status==201
    stored=FeedbackStore(app.db._conn).get(result["feedback_id"])
    assert stored["task_id"]=="review_a"
    assert stored["manifest_id"]=="manifest_a"

def test_feedback_reference_cannot_smuggle_credentials(app):
    status,_=feedback(app,dict(category="bug",text="normal text",evidence_refs=["sk-"+"x"*24]))
    assert status==400

@pytest.mark.parametrize("bad",[
    {"category":["bug"]},{"text":{"nested":"x"}},{"evidence_refs":"evt_1"},
    {"evidence_refs":[7]},{"task_id":["bad"]},{"run_id":["bad"]},
])
def test_feedback_invalid_fields_are_client_errors(app,bad):
    status,_=feedback(app,{"category":"bug","text":"normal",**bad})
    assert status==400

def test_feedback_does_not_merge_distinct_long_messages(app):
    store=FeedbackStore(app.db._conn)
    first=store.submit(category="bug",text="x"*130+" first")
    second=store.submit(category="bug",text="x"*130+" second")
    assert first["feedback_id"]!=second["feedback_id"]

def test_feedback_aggregation_keeps_run_context(app):
    store=FeedbackStore(app.db._conn)
    first=store.submit(category="bug",text="same",task_id="task",run_id="run1")
    second=store.submit(category="bug",text="same",task_id="task",run_id="run2")
    assert first["feedback_id"]!=second["feedback_id"]

def test_feedback_cannot_commit_an_unrelated_transaction(app):
    conn=app.db._conn
    conn.execute("CREATE TABLE review_pending(value TEXT)")
    conn.commit()
    conn.execute("INSERT INTO review_pending VALUES ('uncommitted')")
    try:
        with pytest.raises(FeedbackError):
            FeedbackStore(conn).submit(category="bug",text="unrelated transaction")
    finally:
        conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM review_pending").fetchone()[0]==0

def test_new_binding_consumes_actual_expert_version_not_highest_catalog(app):
    repo=repository_for(app.db)
    expert=app.experts.get("general")
    promote(repo,expert_to_asset(expert))
    newer=copy.deepcopy(expert);newer.version="99.0.0"
    promote(repo,expert_to_asset(newer))
    app.controller.asset_binder=RuntimeAssetBinder(repo)
    task=Task(task_id="review_bind",user_goal="review")
    app.controller._bind_task_assets(task,expert=expert)
    assert set(task.runtime_manifest["asset_bindings"])=={f"expert:{expert.expert_id}@{expert.version}"}

def test_pinned_binding_rejects_changed_execution_identity(app):
    repo=repository_for(app.db)
    workflow=WorkflowDef(workflow_id="review_wf",version="1.0.0",
        nodes=[WorkflowNode(node_id="a",tool="calculate",args={"expression":"1+1"})])
    promote(repo,workflow_to_asset(workflow))
    app.controller.asset_binder=RuntimeAssetBinder(repo)
    task=Task(task_id="review_pinned",user_goal="review")
    app.controller._bind_task_assets(task,workflow=workflow)
    workflow.version="2.0.0"
    with pytest.raises(ConsumptionError):
        app.controller._bind_task_assets(task,workflow=workflow)

def test_pinned_record_identity_must_match_mapping_key(app):
    repo=repository_for(app.db)
    expert=app.experts.get("general");promote(repo,expert_to_asset(expert))
    binder=RuntimeAssetBinder(repo)
    bindings=binder.bind("expert",expert.expert_id)
    next(iter(bindings.values()))["ref"]="expert:other@1.0.0"
    with pytest.raises(ConsumptionError):
        binder.verify_pinned(bindings)

def test_resume_validation_failure_releases_active_registration(app):
    task=Task(task_id="review_resume",user_goal="review")
    task.runtime_manifest["asset_bindings"]={"bad":{}}
    app.db.save_task(task)
    app.controller.asset_binder=SimpleNamespace(
        verify_pinned=Mock(side_effect=ConsumptionError("bad pin")))
    with pytest.raises(ConsumptionError):
        app.controller._resume_suspended(task,Mock(),False)
    assert task.task_id not in app.controller._active

def test_concurrent_feedback_counts_are_atomic(app):
    from concurrent.futures import ThreadPoolExecutor
    store=FeedbackStore(app.db._conn,lock=app.db._lock)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results=list(executor.map(lambda _:store.submit(category="bug",text="concurrent"),
                                  range(12)))
    assert len({r["feedback_id"] for r in results})==1
    assert store.get(results[0]["feedback_id"])["count"]==12
