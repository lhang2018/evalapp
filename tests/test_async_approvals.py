"""Async approval decisions must resume the exact bound action once."""
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.approvals import ApprovalRequired, ApprovalStore
from evoagent.core.persistence import Database
from evoagent.core.permissions import PermissionDecision, ToolPolicy
from evoagent.core.task import Task, TaskStatus
from evoagent.models.base import Action
from evoagent.models.mock import MockModel
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.workflows.models import WorkflowDef, WorkflowNode


def ok(data="done"):
    return ToolResult(ok=True, data=data, executed=True, submitted=True,
                      contract_valid=True, goal_reached=True, independently_verified=True)


@pytest.fixture
def app(tmp_path):
    instance = App(data_dir=str(tmp_path))
    yield instance
    instance.db.close()


def start_flow(app, *, nodes=None, args=None, side_effect="none"):
    calls = []
    app.tools.register(ToolContract(tool_id="action", side_effect_level=side_effect),
                       lambda payload: calls.append(dict(payload)) or ok())
    wf = WorkflowDef(workflow_id="approval-flow", nodes=nodes or [
        WorkflowNode(node_id="action", tool="action", args=args or {"target": "A"},
                     requires_confirmation=True)])
    app._workflows[wf.workflow_id] = wf
    task = app.controller.execute(Task(user_goal="action", workflow_ref=wf.workflow_id),
                                  intent_hint="general")
    assert task.task_status == TaskStatus.SUSPENDED
    assert task.waiting_approvals and not calls
    return task, wf, calls


@pytest.mark.parametrize("side_effect,confirmation", [("none", True), ("external", False), ("external", True)])
def test_approval_resumes_node_once_and_duplicate_decision_is_idempotent(app, side_effect, confirmation):
    task, _, calls = start_flow(app, side_effect=side_effect, nodes=[
        WorkflowNode(node_id="node", tool="action", args={"target": "A"},
                     requires_confirmation=confirmation)])
    aid = task.waiting_approvals[0]
    request = app.approvals.get(aid)
    assert request["status"] == "pending" and request["requested_run_id"] == task.current_run_id
    assert json.loads(request["binding_json"])["args"] == {"target": "A"}
    assert request["action_digest"] and request["expires_at"] > time.time()
    assert app.controller.side_fx.history(task.task_id) == []
    resumed, accepted = app.controller.decide_approval(aid, True)
    assert accepted and resumed.task_status == TaskStatus.SUCCEEDED
    assert calls == [{"target": "A"}]
    assert resumed.parent_run_id == task.current_run_id
    assert app.approvals.get(aid)["status"] == "used"
    assert resumed.waiting_approvals == []
    again, accepted = app.controller.decide_approval(aid, True)
    assert not accepted and again.task_status == TaskStatus.SUCCEEDED
    assert len(app.db.list_runs(task.task_id)) == 2
    with pytest.raises(ValueError):
        app.controller.decide_approval(aid, False)


def test_reject_cancels_task_and_revokes_other_pending_requests(app):
    task, _, calls = start_flow(app, nodes=[
        WorkflowNode(node_id=str(index), tool="action", requires_confirmation=True)
        for index in range(2)])
    aid, other = task.waiting_approvals
    cancelled, accepted = app.controller.decide_approval(aid, False)
    assert accepted and cancelled.task_status == TaskStatus.CANCELLED
    assert cancelled.waiting_approvals == [] and calls == []
    assert app.approvals.get(aid)["status"] == "denied"
    assert app.approvals.get(other)["status"] == "revoked"
    assert not app.controller.decide_approval(aid, False)[1]
    with pytest.raises(ValueError):
        app.controller.decide_approval(other, True)
    assert len(app.db.list_runs(task.task_id)) == 1


def test_parallel_nodes_require_all_current_approvals(app):
    task, _, calls = start_flow(app, side_effect="external", nodes=[
        WorkflowNode(node_id=str(index), tool="action", args={"target": index},
                     requires_confirmation=True) for index in range(2)])
    first, second = task.waiting_approvals
    partial, accepted = app.controller.decide_approval(first, True)
    assert accepted and partial.task_status == TaskStatus.SUSPENDED and not calls
    assert len(app.db.list_runs(task.task_id)) == 1
    resumed, _ = app.controller.decide_approval(second, True)
    assert resumed.task_status == TaskStatus.SUCCEEDED
    assert sorted(call["target"] for call in calls) == [0, 1]
    assert all(app.approvals.get(aid)["status"] == "used" for aid in (first, second))


def test_dependency_args_are_resolved_before_approval_and_completed_node_not_repeated(app):
    reads = []
    app.tools.register(ToolContract(tool_id="read"), lambda _: reads.append(1) or ok("resolved"))
    task, _, calls = start_flow(app, nodes=[
        WorkflowNode(node_id="read", tool="read"),
        WorkflowNode(node_id="write", tool="action", args={"target": "$read"},
                     deps=["read"], requires_confirmation=True)])
    aid = task.waiting_approvals[0]
    assert json.loads(app.approvals.get(aid)["binding_json"])["args"]["target"] == "resolved"
    result, _ = app.controller.decide_approval(aid, True)
    assert result.task_status == TaskStatus.SUCCEEDED
    assert reads == [1] and calls == [{"target": "resolved"}]
    assert result.result_evidence[-1]["budget"]["usage"]["tool_calls"] == 2


def test_parameter_change_beyond_display_summary_cannot_use_old_approval(app):
    payload = {"body": "x" * 1000, "target": "A"}
    task, wf, calls = start_flow(app, args=payload)
    aid = task.waiting_approvals[0]
    app.db.decide_task_approval(aid, True, "test")
    wf.nodes[0].args = {**payload, "target": "B"}
    resumed = app.controller.resume_task(task.task_id)
    assert resumed.task_status == TaskStatus.SUSPENDED and not calls
    replacement = resumed.waiting_approvals[0]
    assert replacement != aid
    assert app.approvals.get(aid)["status"] == "approved"
    assert json.loads(app.approvals.get(replacement)["binding_json"])["args"]["target"] == "B"


def test_changed_tool_version_requires_new_approval(app):
    task, _, calls = start_flow(app)
    aid = task.waiting_approvals[0]
    app.db.decide_task_approval(aid, True, "test")
    app.tools.contract("action").tool_version = "2.0.0"
    resumed = app.controller.resume_task(task.task_id)
    assert resumed.task_status == TaskStatus.SUSPENDED and not calls
    assert resumed.waiting_approvals != [aid]
    assert app.approvals.get(aid)["status"] == "approved"


def test_expired_pending_request_cannot_be_approved(app):
    task, _, _ = start_flow(app)
    aid = task.waiting_approvals[0]
    with app.db._lock:
        app.db._conn.execute("UPDATE approvals SET expires_at=? WHERE approval_id=?", (time.time() - 1, aid))
        app.db._conn.commit()
    assert app.approvals.get(aid)["status"] == "expired"
    with pytest.raises(ValueError, match="过期"):
        app.controller.decide_approval(aid, True)
    resumed = app.controller.resume_task(task.task_id)
    assert resumed.task_status == TaskStatus.SUSPENDED
    assert resumed.waiting_approvals != [aid]


def test_expired_approved_grant_cannot_be_consumed(app):
    aid = app.approvals.grant("action", "expired", ttl_seconds=-1)
    assert not app.approvals.consume(aid)
    assert app.approvals.expire_stale() == 1


def test_expiry_is_checked_after_waiting_for_sqlite_write_lock(app):
    aid = app.approvals.grant("action", "expiring", ttl_seconds=0.05)
    other = Database(app.db.path)
    started = threading.Event()
    consumed = []

    def consume():
        started.set()
        consumed.append(other.consume_approval(aid))

    with app.db._lock:
        app.db._conn.execute("BEGIN IMMEDIATE")
        thread = threading.Thread(target=consume)
        thread.start()
        assert started.wait(5)
        time.sleep(0.1)
        app.db._conn.commit()
    try:
        thread.join(5)
        assert consumed == [False]
    finally:
        other.close()


def test_task_and_scope_bindings_prevent_cross_task_or_node_use(app):
    first, _, _ = start_flow(app)
    aid = first.waiting_approvals[0]
    app.db.decide_task_approval(aid, True, "test")
    second, _, _ = start_flow(app)
    assert second.waiting_approvals != [aid]
    binding = json.loads(app.approvals.get(aid)["binding_json"])
    context = {**binding["context"], "node": "different", "scope": "different"}
    with pytest.raises(ApprovalRequired) as required:
        app.approvals.authorize_action("action", binding["args"], task_id=first.task_id, context=context)
    assert required.value.approval_id != aid
    assert app.approvals.get(aid)["status"] == "approved"


def test_policy_deny_still_overrides_approved_request(app):
    task, _, calls = start_flow(app)
    aid = task.waiting_approvals[0]
    app.db.decide_task_approval(aid, True, "test")
    app.permissions.register_tool_policy(ToolPolicy("action", PermissionDecision.DENY))
    resumed = app.controller.resume_task(task.task_id)
    assert resumed.task_status == TaskStatus.FAILED and not calls
    assert app.approvals.get(aid)["status"] == "approved"


def test_approval_does_not_bypass_unknown_side_effect(app):
    task, _, calls = start_flow(app, side_effect="external")
    aid = task.waiting_approvals[0]
    app.controller.side_fx.record(task.task_id, "action", status="unknown")
    result, accepted = app.controller.decide_approval(aid, True)
    assert accepted and result.task_status == TaskStatus.SUSPENDED and not calls
    assert app.approvals.get(aid)["status"] == "approved"
    assert len(app.db.list_runs(task.task_id)) == 1


def test_waiting_approval_is_not_a_failure_and_resume_reuses_request(app):
    task, _, calls = start_flow(app)
    assert app.db.get_case_by_task(task.task_id) is None
    before = app.controller.cp_mgr.latest(task.task_id).budget_state["usage"]
    resumed = app.controller.resume_task(task.task_id)
    assert resumed.task_status == TaskStatus.SUSPENDED and not calls
    assert resumed.waiting_approvals == task.waiting_approvals
    assert len(app.approvals.list_for_task(task.task_id)) == 1
    assert app.controller.cp_mgr.latest(task.task_id).budget_state["usage"]["tool_calls"] == before["tool_calls"]


def test_cancelled_waiting_task_cannot_approve_old_request(app):
    task, _, calls = start_flow(app)
    assert app.controller.cancel_task(task.task_id)["ok"]
    assert app.db.get_task(task.task_id).waiting_approvals == []
    assert app.approvals.get(task.waiting_approvals[0])["status"] == "revoked"
    with pytest.raises(ValueError):
        app.controller.decide_approval(task.waiting_approvals[0], True)
    assert not calls


def test_function_node_with_confirmation_resumes_after_approval(app):
    calls = []
    wf = WorkflowDef(workflow_id="function-approval", nodes=[
        WorkflowNode(node_id="custom", fn=lambda context: calls.append(context) or "done",
                     requires_confirmation=True)])
    app._workflows[wf.workflow_id] = wf
    task = app.controller.execute(Task(user_goal="custom", workflow_ref=wf.workflow_id),
                                  intent_hint="general")
    assert task.task_status == TaskStatus.SUSPENDED and not calls
    result, _ = app.controller.decide_approval(task.waiting_approvals[0], True)
    assert result.task_status == TaskStatus.SUCCEEDED and len(calls) == 1


def test_decision_durable_before_restart_and_retry_resumes_once(tmp_path):
    first = App(data_dir=str(tmp_path))
    task, wf, _ = start_flow(first)
    aid = task.waiting_approvals[0]
    first.db.decide_task_approval(aid, True, "test")  # stop after decision, before scheduling
    first.db.close()
    second = App(data_dir=str(tmp_path))
    calls = []
    try:
        second._workflows[wf.workflow_id] = wf
        second.tools.register(ToolContract(tool_id="action"), lambda p: calls.append(p) or ok())
        resumed, accepted = second.controller.decide_approval(aid, True)
        assert not accepted and resumed.task_status == TaskStatus.SUCCEEDED
        assert len(calls) == 1 and len(second.db.list_runs(task.task_id)) == 2
    finally:
        second.db.close()


class ApprovalModel(MockModel):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def suggest_action(self, messages, available_tools):
        self.calls += 1
        if any(message.role == "tool" for message in messages):
            return Action(kind="finish", answer="done")
        return Action(kind="tool_call", tool="action", args={"target": "approved-target"})


def test_react_restart_executes_saved_action_without_new_model_decision(tmp_path):
    first = App(data_dir=str(tmp_path))
    model = ApprovalModel()
    first.controller.router.route = lambda *a, **k: model
    first.tools.register(ToolContract(tool_id="action", side_effect_level="external"), lambda _: ok())
    task = first.submit("action", intent="general", acceptance=[{"type": "tool_succeeded"}])
    assert task.task_status == TaskStatus.SUSPENDED and model.calls == 1
    aid = task.waiting_approvals[0]
    before = first.controller.cp_mgr.latest(task.task_id).budget_state["usage"]["model_calls"]
    first.db.close()
    second = App(data_dir=str(tmp_path))
    calls, contexts = [], []
    model = ApprovalModel()
    original = model.suggest_action

    def spy(messages, schemas):
        contexts.append(messages)
        return original(messages, schemas)

    model.suggest_action = spy
    second.controller.router.route = lambda *a, **k: model
    second.tools.register(ToolContract(tool_id="action", side_effect_level="external"),
                          lambda p: calls.append(p) or ok())
    try:
        resumed, _ = second.controller.decide_approval(aid, True)
        assert resumed.task_status == TaskStatus.SUCCEEDED
        assert calls == [{"target": "approved-target"}] and model.calls == 1
        assert any(message.role == "tool" for message in contexts[0])
        assert resumed.result_evidence[-1]["budget"]["usage"]["model_calls"] == before + 1
    finally:
        second.db.close()


def test_concurrent_decisions_and_consumptions_are_atomic(app):
    task, _, _ = start_flow(app)
    aid = task.waiting_approvals[0]
    other = Database(app.db.path)
    decisions = []
    barrier = threading.Barrier(2)

    def decide(db):
        barrier.wait()
        decisions.append(db.decide_task_approval(aid, True, "test")[1])

    threads = [threading.Thread(target=decide, args=(db,)) for db in (app.db, other)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert sorted(decisions) == [False, True]
        binding = json.loads(app.approvals.get(aid)["binding_json"])
        consumed = []
        barrier = threading.Barrier(2)

        def consume(db):
            barrier.wait()
            try:
                consumed.append(ApprovalStore(db).authorize_action(
                    "action", binding["args"], task_id=task.task_id, context=binding["context"]))
            except ApprovalRequired:
                consumed.append(False)

        threads = [threading.Thread(target=consume, args=(db,)) for db in (app.db, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert sorted(consumed) == [False, True]
    finally:
        other.close()


def test_decision_api_is_admin_only_and_validates_current_boundary(app):
    task, _, calls = start_flow(app)
    aid = task.waiting_approvals[0]
    app.config.api_token, app.config.api_admin_token = "operator", "admin"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, body=None, token="admin"):
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    try:
        path = f"/approvals/{aid}/decision"
        assert request(path, {"approved": True}, token="operator")[0] == 403
        assert request(path, {"approved": "true"})[0] == 400
        assert request("/approvals/missing/decision", {"approved": True})[0] == 404
        status, record = request(f"/approvals/{aid}")
        assert status == 200 and record["binding_json"]
        assert request(f"/approvals/{aid}", token="operator")[0] == 403
        assert request(path, {"approved": True})[0] == 202
        deadline = time.time() + 5
        while time.time() < deadline:
            if app.db.get_task(task.task_id).task_status == TaskStatus.SUCCEEDED:
                break
            time.sleep(0.01)
        assert calls == [{"target": "A"}]
        assert request(path, {"approved": True})[0] == 200
        assert request(path, {"approved": False})[0] == 409
    finally:
        server.shutdown()
        thread.join(5)


def test_old_approval_schema_migration_preserves_audit_rows(tmp_path):
    path = str(tmp_path / "old-approvals.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE approvals(approval_id TEXT PRIMARY KEY,task_id TEXT,op TEXT NOT NULL,target TEXT,scope TEXT,action_summary TEXT NOT NULL,status TEXT NOT NULL,granted_by TEXT,created_at REAL NOT NULL,expires_at REAL,consumed_at REAL,revoked_at REAL)")
    conn.execute("INSERT INTO approvals VALUES ('old','task','pay','target','pay','old grant','used','user',1,NULL,2,NULL)")
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        row = db.get_approval("old")
        assert row["status"] == "used" and row["action_digest"] is None
        assert ApprovalStore(db).grant("pay", "new")
    finally:
        db.close()
