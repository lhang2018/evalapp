"""Exercise direct controller consumers, not only the proactive wrapper."""
from types import SimpleNamespace
from unittest.mock import patch
import json
import time

import pytest

from evoagent.app import App
from evoagent.core.resource_admission import AdmissionDecision, ResourceGovernor
from evoagent.core.task import Task, TaskStatus


def sampler(value):
    def read():
        if value == "raises":
            raise RuntimeError("synthetic sensor failure")
        return value
    return read


@pytest.mark.parametrize("origin", ["scheduled", "derived", "opportunity"])
@pytest.mark.parametrize("value", [{}, {"cpu": float("nan")}, {"cpu": False}, "raises"])
def test_invalid_background_sample_never_admits_even_without_proactive_wrapper(origin, value):
    decision = ResourceGovernor(sampler(value)).admit(origin)
    assert decision.snapshot["sample_valid"] is False
    assert decision.decision == "defer"
    assert decision.reason


@pytest.fixture
def app(tmp_path):
    value = App(data_dir=str(tmp_path))
    yield value
    value.goal_scheduler.stop()
    value.memory.close()
    value.db.close()


def ready_goal(app):
    app.goal_governor.install_policy("admission/v1", {}, actor="review", reason="test")
    deadline = time.time() + 3600
    goal = app.goals.create("answer a general question", actor="review", goal_type="long_term",
                            success_criteria=["verified improvement"], deadline=deadline)
    goal = app.goals.authorize(goal["goal_id"],
        {"tools": [], "permissions": [], "max_risk": "low"}, actor="review", reason="test",
        level="continuous", expected_revision=goal["revision"], expires_at=deadline - 100,
        no_progress_policy_ref="admission/v1")
    return app.goals.transition(goal["goal_id"], "ready", actor="review", reason="test",
                                expected_revision=goal["revision"])


@pytest.mark.parametrize("origin", ["scheduled", "derived", "opportunity"])
def test_real_controller_defers_invalid_sampling_before_run_and_recovers(app, origin):
    goal = ready_goal(app)
    state = {"sample": {}}
    app.controller.resource_governor = ResourceGovernor(lambda: state["sample"])
    core = Task(user_goal="core task", task_status=TaskStatus.RUNNING)
    app.db.save_task(core)
    with patch.object(app.controller.runs, "begin", wraps=app.controller.runs.begin) as begin:
        task = app.controller.submit_task("answer a general question", origin=origin,
                                          goal_id=goal["goal_id"], wait=True)
        assert task.task_status == TaskStatus.CREATED
        assert task.runtime_manifest["resource_deferred"]["snapshot"]["sample_valid"] is False
        assert app.db._conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task.task_id,)).fetchone()[0] == 0
        begin.assert_not_called()
        assert task.task_id not in app.controller._active
        assert app.db.get_task(core.task_id).task_status == TaskStatus.RUNNING
        state["sample"] = {"cpu": 0.2, "memory": 0.2, "model_pool": 0.1}
        resumed = app.controller.execute(app.db.get_task(task.task_id))
        assert resumed.task_status != TaskStatus.CREATED
        assert "resource_deferred" not in resumed.runtime_manifest
        assert begin.call_count == 1


@pytest.mark.parametrize("mode", ["raises", "missing", "pause", "resume", "invalid_snapshot"])
def test_bad_admission_adapter_cannot_start_or_leave_stale_active_entry(app, mode):
    def admit(origin):
        if mode == "raises":
            raise RuntimeError("synthetic adapter failed")
        if mode == "missing":
            return None
        if mode == "invalid_snapshot":
            return AdmissionDecision("admit", origin, 0.0, snapshot={})
        return AdmissionDecision(mode, origin, 0.0, snapshot={"sample_valid": True})
    goal = ready_goal(app)
    app.controller.resource_governor = SimpleNamespace(admit=admit)
    with patch.object(app.controller.runs, "begin", wraps=app.controller.runs.begin) as begin:
        task = app.controller.submit_task("answer a general question", origin="opportunity",
                                          goal_id=goal["goal_id"], wait=True)
        assert task.task_status == TaskStatus.CREATED
        assert task.runtime_manifest["resource_deferred"]["decision"] == "defer"
        assert task.task_id not in app.controller._active
        begin.assert_not_called()


def test_foreground_unavailable_sampling_keeps_compatibility_but_explicitly_marks_unknown(app):
    app.controller.resource_governor = ResourceGovernor(lambda: {})
    task = app.controller.submit_task("answer a general question", wait=True)
    assert task.task_status != TaskStatus.CREATED
    assert "resource_deferred" not in task.runtime_manifest


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_invalid_measurements_in_decision_are_json_safe_for_events_and_http(value):
    decision = ResourceGovernor(lambda: {"cpu": value}).admit("opportunity")
    json.dumps(decision.to_dict(), allow_nan=False)
    assert decision.snapshot["sample_valid"] is False
