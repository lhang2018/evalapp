"""Durable run identity, clarification and checkpoint continuation."""
import copy
import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from evoagent.app import App
from evoagent.api.server import ApiServer
from evoagent.core.persistence import Database
from evoagent.core.runs import RunManager
from evoagent.core.task import RiskLevel, Task, TaskStatus
from evoagent.models.base import Action
from evoagent.models.mock import MockModel
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.workflows.models import WorkflowDef, WorkflowNode


class ClarifyingModel(MockModel):
    def suggest_action(self, messages, available_tools):
        if any(m.role == "user" and m.content == "使用北京时间" for m in messages):
            return Action(kind="finish", answer="北京时间已确认")
        if any(m.role == "tool" for m in messages):
            return Action(kind="request_user", answer="需要哪个时区？")
        return Action(kind="tool_call", tool="current_time")


def use_clarifying_model(app):
    app.controller.router.route = lambda *a, **k: ClarifyingModel()


@pytest.fixture
def app(tmp_path):
    instance = App(data_dir=str(tmp_path))
    yield instance
    instance.db.close()


def waiting_task(app):
    use_clarifying_model(app)
    return app.submit("现在几点了", intent="time_query",
                      acceptance=[{"type": "tool_succeeded"}])


def test_answer_continues_history_and_preserves_tool_evidence(app):
    task = waiting_task(app)
    assert task.task_status == TaskStatus.WAITING_USER
    old_run = task.current_run_id
    original_manifest = app.db.get_runtime_manifest("manifest_" + old_run)
    cp = app.controller.cp_mgr.latest(task.task_id)
    assert cp.react_state["next_iteration"] == 3
    assert cp.budget_state["usage"]["tool_calls"] == 1
    assert task.failure_reason is None and task.result_evidence == []
    resumed, accepted = app.controller.answer_task(
        task.task_id, task.waiting_request["request_id"], "使用北京时间")
    assert accepted and resumed.task_status == TaskStatus.SUCCEEDED
    assert resumed.current_run_id != old_run
    assert resumed.parent_run_id == old_run
    assert resumed.waiting_request is None
    usage = resumed.result_evidence[-1]["budget"]["usage"]
    assert usage["tool_calls"] == 1  # previously completed tool was not rerun
    assert usage["model_calls"] == cp.budget_state["usage"]["model_calls"] + 1
    runs = app.db.list_runs(task.task_id)
    assert [r["outcome"] for r in runs] == ["WAITING_USER", "SUCCEEDED"]
    assert runs[1]["parent_run_id"] == runs[0]["run_id"]
    assert app.db.get_runtime_manifest("manifest_" + old_run) == original_manifest
    events = app.bus.query(task_id=task.task_id)
    assert {e.run_id for e in events if e.event_type == "react_finished"} == {
        old_run, resumed.current_run_id}


def test_answer_retry_is_idempotent_and_conflicting_answer_rejected(app):
    task = waiting_task(app)
    question = task.waiting_request["request_id"]
    app.controller.answer_task(task.task_id, question, "使用北京时间")
    same, accepted = app.controller.answer_task(task.task_id, question, "使用北京时间")
    assert not accepted and same.task_status == TaskStatus.SUCCEEDED
    assert len(app.db.list_runs(task.task_id)) == 2
    with pytest.raises(ValueError, match="不同答案"):
        app.controller.answer_task(task.task_id, question, "其他时区")


@pytest.mark.parametrize("request_id,answer,error", [
    ("stale", "answer", ValueError), ("", "answer", TypeError),
    ("request", "   ", TypeError), ("request", 123, TypeError),
    ("request", "a" * 16001, TypeError)])
def test_invalid_answer_does_not_change_task(app, request_id, answer, error):
    task = waiting_task(app)
    before = app.db.get_task(task.task_id).to_dict()
    with pytest.raises(error):
        app.controller.answer_task(task.task_id, request_id, answer)
    assert app.db.get_task(task.task_id).to_dict() == before


def test_answer_survives_application_restart(tmp_path):
    first = App(data_dir=str(tmp_path))
    task = waiting_task(first)
    first.db.close()
    second = App(data_dir=str(tmp_path))
    try:
        use_clarifying_model(second)
        result, accepted = second.controller.answer_task(
            task.task_id, task.waiting_request["request_id"], "使用北京时间")
        assert accepted and result.task_status == TaskStatus.SUCCEEDED
        assert result.result_evidence[-1]["budget"]["usage"]["tool_calls"] == 1
    finally:
        second.db.close()


def test_classification_answer_reenters_planning_with_original_goal(app):
    seen = []

    class Classifier:
        def classify(self, goal, **kwargs):
            seen.append(goal)
            return "time_query", "general", (0.99 if "使用北京时间" in goal else 0.1), "model"

    app.controller.classifier = Classifier()
    task = app.submit("现在几点了", risk_level=RiskLevel.CRITICAL)
    assert task.task_status == TaskStatus.WAITING_USER
    assert task.waiting_request["phase"] == "classification"
    resumed, _ = app.controller.answer_task(
        task.task_id, task.waiting_request["request_id"], "使用北京时间")
    assert resumed.task_status == TaskStatus.SUCCEEDED
    assert resumed.user_goal == "现在几点了"
    assert "使用北京时间" in seen[-1]


def test_resume_rejects_changed_expert_before_new_tool_call(app):
    task = waiting_task(app)
    app.experts.get(task.domain).version = "changed"
    resumed, _ = app.controller.answer_task(
        task.task_id, task.waiting_request["request_id"], "使用北京时间")
    assert resumed.task_status == TaskStatus.FAILED
    assert "expert_ref" in resumed.failure_reason
    assert len(app.db.list_runs(task.task_id)) == 2
    assert task.task_id not in app.controller._active


def test_atomic_run_claim_rejects_stale_second_connection(tmp_path):
    first, second = Database(str(tmp_path / "runs.db")), Database(str(tmp_path / "runs.db"))
    try:
        task = Task()
        first.save_task(task)
        stale = second.get_task(task.task_id)
        manager = RunManager(first)
        rid, started = manager.begin(task, {})
        manager.finish(task, rid, started)
        with pytest.raises(ValueError, match="状态已改变"):
            RunManager(second).begin(stale, {})
        assert len(first.list_runs(task.task_id)) == 1
    finally:
        first.close()
        second.close()


def test_immutable_manifest_cannot_be_replaced(app):
    task = app.submit("现在几点了", intent="time_query")
    data = copy.deepcopy(task.version_manifest)
    data["expert_ref"] = "other/version"
    with pytest.raises(ValueError, match="不可变"):
        app.db.save_runtime_manifest("manifest_" + task.current_run_id,
            task.task_id, task.current_run_id, None, None, data)


def test_terminal_task_cannot_be_executed_again(app):
    task = app.submit("现在几点了", intent="time_query")
    with pytest.raises(ValueError, match="仅新任务"):
        app.controller.execute(task)
    assert len(app.db.list_runs(task.task_id)) == 1


def test_answer_is_consumed_once_across_database_connections(app):
    task = waiting_task(app)
    other = Database(app.db.path)
    barrier = threading.Barrier(2)
    accepted = []

    def submit(db):
        barrier.wait()
        _, fresh = db.record_user_answer(task.task_id,
            task.waiting_request["request_id"], "使用北京时间")
        accepted.append(fresh)

    threads = [threading.Thread(target=submit, args=(db,)) for db in (app.db, other)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert sorted(accepted) == [False, True]
        assert len(app.db.get_task(task.task_id).user_answers) == 1
        assert app.controller.resume_task(task.task_id).task_status == TaskStatus.SUCCEEDED
    finally:
        other.close()


def test_legacy_run_schema_migrates_without_losing_history(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE task_runs(run_id TEXT PRIMARY KEY, task_id TEXT, started_at REAL, finished_at REAL, outcome TEXT, version_manifest TEXT)")
    conn.execute("INSERT INTO task_runs VALUES ('run_old','old',1,2,'SUCCEEDED','{}')")
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        row = db.list_runs("old")[0]
        assert row["run_id"] == "run_old" and row["parent_run_id"] is None
    finally:
        db.close()


def test_workflow_pause_restart_keeps_completed_outputs_and_budget(tmp_path):
    app = App(data_dir=str(tmp_path))
    calls = []
    wf = WorkflowDef(workflow_id="pause-flow", nodes=[
        WorkflowNode(node_id="first", tool="read"),
        WorkflowNode(node_id="second", tool="read", deps=["first"])])
    app._workflows[wf.workflow_id] = wf

    def runner(_):
        calls.append(1)
        assert app.controller.pause_task(task.task_id)["ok"]
        return ToolResult(ok=True, data="first")

    app.tools.register(ToolContract(tool_id="read"), runner)
    task = Task(user_goal="read", workflow_ref=wf.workflow_id)
    suspended = app.controller.execute(task, intent_hint="general")
    assert suspended.task_status == TaskStatus.SUSPENDED
    cp = app.controller.cp_mgr.latest(task.task_id)
    assert cp.task_state["workflow_outputs"] == {"first": "first"}
    assert cp.budget_state["usage"]["tool_calls"] == 1
    assert cp.budget_state["workflow_scope"]["usage"]["tool_calls"] == 1
    app.db.close()
    app = App(data_dir=str(tmp_path))
    try:
        app._workflows[wf.workflow_id] = wf
        app.tools.register(ToolContract(tool_id="read"),
            lambda _: calls.append(2) or ToolResult(ok=True, data="second"))
        resumed = app.controller.resume_task(task.task_id)
        assert resumed.task_status == TaskStatus.SUCCEEDED
        assert calls == [1, 2]
        assert resumed.result_evidence[-1]["budget"]["usage"]["tool_calls"] == 2
        assert resumed.runtime_manifest["workflow_budget"]["usage"]["tool_calls"] == 2
        assert resumed.parent_run_id == suspended.current_run_id
    finally:
        app.db.close()


def test_answer_api_status_validation_auth_and_run_history(app):
    task = waiting_task(app)
    app.config.api_token = "operator"
    app.config.api_admin_token = "admin"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, body=None, token="operator"):
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    try:
        path = f"/tasks/{task.task_id}/answer"
        body = {"request_id": task.waiting_request["request_id"], "answer": "使用北京时间"}
        assert request(path, body, token="bad")[0] == 403
        assert request(path, {"answer": 1})[0] == 400
        assert request(path, {**body, "request_id": "stale"})[0] == 409
        assert request(path, body)[0] == 202
        app.controller._wait_for_previous_run(task.task_id)
        assert request(path, body)[0] == 200
        status, payload = request(f"/tasks/{task.task_id}/runs")
        assert status == 200 and len(payload["runs"]) == 2
        assert request(f"/tasks/{task.task_id}")[0] == 200
        assert request(f"/tasks/{task.task_id}/events")[0] == 200
        assert request("/tasks/missing/runs")[0] == 404
    finally:
        server.shutdown()
        thread.join(5)
