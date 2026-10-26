"""Recovery gate uses real contracts/storage and releases pre-run registrations."""
import copy
import sqlite3

import pytest

from evoagent.app import App
from evoagent.core.leases import CURRENT_LEASE
from evoagent.core.task import Task, TaskStatus
from evoagent.planning.planner import ExecutionPlan, PlanStep


@pytest.fixture
def app(tmp_path):
    instance = App(data_dir=str(tmp_path / "app"))
    try:
        yield instance
    finally:
        assert not instance.controller.runs._handles
        instance.db.close()


def task_with_plan(app, hints):
    steps = [PlanStep(f"s{i}", f"step {i}", tool_hint=hint, state="done")
             for i, hint in enumerate(hints)]
    plan = ExecutionPlan("p", "recover", steps,
                         completed_work=[s.step_id for s in steps],
                         acceptance_criteria=[{"type": "tool_succeeded"}])
    task = Task(task_id="recovery-review", user_goal="recover")
    task.runtime_manifest["execution_plan"] = plan.to_dict()
    app.db.save_task(task)
    return task


def abandon(app, task):
    controller = app.controller
    rid, _ = controller.runs.begin(task, {})
    _, reset, stop, thread = controller.runs._handles.pop(rid)
    stop.set()
    thread.join(2)
    CURRENT_LEASE.reset(reset)
    app.db._conn.execute("UPDATE run_leases SET expires_at=0 WHERE task_id=?",
                         (task.task_id,))
    app.db._conn.commit()
    task.task_status = TaskStatus.RUNNING
    app.db.save_task(task)
    app.db.save_checkpoint("cp", task.task_id, 1.0,
        {"checkpoint_id": "cp", "budget_state": {"usage": {"model_calls": 0}},
         "react_state": {}, "task_state": {}})


@pytest.mark.parametrize("hints", [
    ["current_time"], ["file_read"], ["current_time", "file_write"],
])
def test_completed_observations_do_not_require_side_effect_receipts(app, monkeypatch, hints):
    task = task_with_plan(app, hints)
    before = copy.deepcopy(task.runtime_manifest["execution_plan"])
    if "file_write" in hints:
        app.db.save_side_effect("fx", task.task_id, "", "file_write", None, "", "confirmed")
        app.controller.react.bindings.bind("fx", "s1", task_id=task.task_id, tool_id="file_write")
    abandon(app, task)
    calls = []

    def continue_run(current, budget, start=False):
        calls.append(current.task_id)
        current.task_status = TaskStatus.SUCCEEDED
        return current

    monkeypatch.setattr(app.controller, "_run", continue_run)
    result = app.controller.recover_task(task.task_id)
    assert result.task_status == TaskStatus.SUCCEEDED
    assert calls == [task.task_id]
    assert app.db.get_task(task.task_id).runtime_manifest["execution_plan"] == before
    assert task.task_id not in app.controller._active
    assert len(app.db.list_runs(task.task_id)) == 2
    events = [e for e in app.bus.query() if e.event_type == "reconciliation_gate"]
    assert events[-1].payload["decision"] == "resume"


@pytest.mark.parametrize("hint", ["file_write", "unregistered_tool"])
def test_only_known_observation_contracts_are_excluded(app, hint):
    task = task_with_plan(app, [hint])
    task.task_status = TaskStatus.RUNNING
    app.db.save_task(task)
    result = app.controller._reconciliation_gate(task)
    assert result.task_status == TaskStatus.WAITING_USER
    assert result.waiting_request["summary"]["unexecuted"] == ["s0"]


def test_receipt_on_observation_tool_remains_undeclared(app):
    task = task_with_plan(app, ["current_time"])
    task.task_status = TaskStatus.RUNNING
    app.db.save_task(task)
    app.db.save_side_effect("rogue", task.task_id, "", "current_time", None, "", "confirmed")
    result = app.controller._reconciliation_gate(task)
    assert result.task_status == TaskStatus.WAITING_USER
    assert result.waiting_request["summary"]["undeclared"] == ["rogue"]


def test_ledger_read_error_is_a_recorded_failure_without_active_leak(app, monkeypatch):
    task = task_with_plan(app, ["current_time"])
    abandon(app, task)
    # Recovery inspection still succeeds; the following history read fails.
    def unreadable(_):
        raise sqlite3.OperationalError("ledger read unavailable")
    monkeypatch.setattr(app.controller.side_fx, "history", unreadable)
    result = app.controller.recover_task(task.task_id)
    assert result.task_status == TaskStatus.FAILED
    assert "ledger read unavailable" in result.failure_reason
    assert task.task_id not in app.controller._active
    assert len(app.db.list_runs(task.task_id)) == 1
    events = [e for e in app.bus.query() if e.event_type == "reconciliation_gate"]
    assert events[-1].payload["decision"] == "error"


@pytest.mark.parametrize("status", [TaskStatus.RUNNING, TaskStatus.SUSPENDED])
@pytest.mark.parametrize("broken", ["plan", "bindings"])
def test_recovery_errors_follow_legal_failure_transitions(app, monkeypatch, status, broken):
    task = task_with_plan(app, ["current_time"])
    abandon(app, task)
    task.task_status = status
    if broken == "plan":
        task.runtime_manifest["execution_plan"] = {"steps": "corrupt"}
    else:
        def fail(*args):
            raise ValueError("binding evidence corrupt")
        monkeypatch.setattr(app.controller.react.bindings, "heal_pending", fail)
    app.db.save_task(task)
    result = app.controller.recover_task(task.task_id)
    assert result.task_status == TaskStatus.FAILED
    assert ("计划不可读" if broken == "plan" else "binding evidence corrupt") in result.failure_reason
    assert app.db.get_task(task.task_id).task_status == TaskStatus.FAILED
    assert task.task_id not in app.controller._active
    assert len(app.db.list_runs(task.task_id)) == 1
    assert app.controller.cp_mgr.latest(task.task_id).checkpoint_id == "cp"
    transitions = [e.payload["to"] for e in app.bus.query() if e.event_type == "state_transition"]
    assert transitions[-2:] == ["RECOVERING", "FAILED"]


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_gate_event_failure_releases_registration_even_when_not_caught(app, monkeypatch, failure):
    task = task_with_plan(app, [None])
    abandon(app, task)
    original_emit = app.controller._emit
    def emit(current, event_type, *args, **kwargs):
        if event_type == "reconciliation_gate":
            raise failure("event sink unavailable")
        return original_emit(current, event_type, *args, **kwargs)
    monkeypatch.setattr(app.controller, "_emit", emit)
    with pytest.raises(failure, match="event sink unavailable"):
        app.controller.recover_task(task.task_id)
    assert task.task_id not in app.controller._active
    assert len(app.db.list_runs(task.task_id)) == 1
    # No phantom registration blocks a retry of the same real recovery.
    monkeypatch.setattr(app.controller, "_emit", original_emit)
    monkeypatch.setattr(app.controller, "_run", lambda current, *a, **k: current)
    app.controller.recover_task(task.task_id)
    assert task.task_id not in app.controller._active
