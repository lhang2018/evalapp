"""Recovery outcomes stay actionable if their state/gate event cannot be written."""
import pytest

from evoagent.core.persistence import Database
from evoagent.core.task import TaskStatus
from test_recovery_gate_review_20261027 import app, abandon, task_with_plan


def prepare(app, status, problem):
    task = task_with_plan(app, ["current_time"])
    abandon(app, task)
    task.task_status = status
    if problem == "plan":
        task.runtime_manifest["execution_plan"] = {"steps": "broken"}
    elif problem == "divergence":
        app.db.save_side_effect("rogue", task.task_id, "", "http_post", None, "", "confirmed")
    app.db.save_task(task)
    return task


def persisted(app, task):
    # Independent connection rules out a correct-looking in-memory outcome.
    reader = Database(app.db.path)
    try:
        return reader.get_task(task.task_id)
    finally:
        reader.close()


def assert_stopped(app, task):
    assert task.task_id not in app.controller._active
    assert len(app.db.list_runs(task.task_id)) == 1
    assert not app.controller.runs._handles
    assert app.controller.cp_mgr.latest(task.task_id).checkpoint_id == "cp"
    assert not [e for e in app.bus.query() if e.event_type in ("tool_call", "task_resumed")]


@pytest.mark.parametrize("status", [TaskStatus.RUNNING, TaskStatus.SUSPENDED])
def test_divergence_from_either_recoverable_state_produces_a_question(app, status):
    task = prepare(app, status, "divergence")
    result = app.controller.recover_task(task.task_id)
    assert result.task_status == TaskStatus.WAITING_USER
    stored = persisted(app, task)
    assert stored.waiting_request["phase"] == "reconciliation"
    assert stored.waiting_request["summary"]["undeclared"] == ["rogue"]
    assert_stopped(app, task)


def fail_state_sink(app, monkeypatch, sink, state):
    emit = app.controller._emit
    def fail(task, event_type, payload, *args, **kwargs):
        if ((sink == "state_event" and event_type == "state_transition" and payload["to"] == state)
                or (sink == "gate_event" and event_type == "reconciliation_gate")):
            raise OSError("state sink unavailable")
        return emit(task, event_type, payload, *args, **kwargs)
    monkeypatch.setattr(app.controller, "_emit", fail)


@pytest.mark.parametrize("status", [TaskStatus.RUNNING, TaskStatus.SUSPENDED])
@pytest.mark.parametrize("sink", ["state_event", "gate_event"])
def test_waiting_state_is_committed_with_the_question_before_state_sinks(app, monkeypatch, status, sink):
    task = prepare(app, status, "divergence")
    fail_state_sink(app, monkeypatch, sink, "WAITING_USER")
    with pytest.raises(OSError, match="state sink unavailable"):
        app.controller.recover_task(task.task_id)
    stored = persisted(app, task)
    assert stored.task_status == TaskStatus.WAITING_USER
    assert stored.waiting_request is not None
    assert stored.waiting_request["request_id"] == f"recon_{task.task_id}"
    assert stored.waiting_request["phase"] == "reconciliation"
    assert stored.waiting_request["answered"] is False
    assert stored.waiting_request["summary"]["undeclared"] == ["rogue"]
    assert_stopped(app, task)


@pytest.mark.parametrize("status", [TaskStatus.RUNNING, TaskStatus.SUSPENDED])
@pytest.mark.parametrize("sink", ["state_event", "gate_event"])
@pytest.mark.parametrize("problem", ["plan", "bindings"])
def test_failed_state_is_committed_with_the_reason_before_state_sinks(app, monkeypatch, status, sink, problem):
    task = prepare(app, status, problem)
    if problem == "bindings":
        def unavailable(*args):
            raise ValueError("binding evidence unavailable")
        monkeypatch.setattr(app.controller.react.bindings, "heal_pending", unavailable)
    fail_state_sink(app, monkeypatch, sink, "FAILED")
    with pytest.raises(OSError, match="state sink unavailable"):
        app.controller.recover_task(task.task_id)
    stored = persisted(app, task)
    assert stored.task_status == TaskStatus.FAILED
    assert ("计划不可读" if problem == "plan" else "binding evidence unavailable") in (stored.failure_reason or "")
    assert_stopped(app, task)


@pytest.mark.parametrize("status", [TaskStatus.RUNNING, TaskStatus.SUSPENDED])
def test_audit_storage_error_retains_existing_best_effort_semantics(app, monkeypatch, status):
    task = prepare(app, status, "divergence")
    def unavailable(*args, **kwargs):
        raise OSError("audit storage unavailable")
    monkeypatch.setattr(app.controller.audit, "record", unavailable)
    result = app.controller.recover_task(task.task_id)
    assert result.task_status == TaskStatus.WAITING_USER
    assert persisted(app, task).waiting_request["phase"] == "reconciliation"
    assert_stopped(app, task)
