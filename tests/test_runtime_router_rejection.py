"""Runtime must honor router rejection without calling an unchecked adapter."""
import pytest

from evoagent.app import App
from evoagent.core.task import TaskStatus
from evoagent.evolution.routing_eval import ModelRoutingCase, ModelRoutingEvaluator


@pytest.fixture
def app(tmp_path):
    instance = App(data_dir=str(tmp_path))
    yield instance
    instance.db.close()


def forbid_fallback(app, monkeypatch, *, after=lambda: True):
    calls = []
    for method in ("generate", "suggest_action"):
        original = getattr(app.controller.react.model, method)
        def guard(*args, _original=original, _method=method, **kwargs):
            if after():
                calls.append(_method)
                raise AssertionError("unchecked fallback adapter was executed")
            return _original(*args, **kwargs)
        monkeypatch.setattr(app.controller.react.model, method, guard)
    return calls


def assert_failed_route(app, task, *, source, error_class):
    assert task.task_status == TaskStatus.FAILED
    saved = app.db.get_task(task.task_id)
    assert saved.task_status == TaskStatus.FAILED
    route = saved.runtime_manifest["model_route"]
    assert route["source"] == source and route["error_class"] == error_class
    assert route["model_id"] is None and route["provider"] is None
    events = app.bus.query(task_id=task.task_id, limit=100)
    failures = [e for e in events if e.event_type == "model_route_failed"]
    assert len(failures) == 1 and failures[0].payload == route
    assert failures[0].run_id == saved.current_run_id
    assert any(e.event_type == "task_failed" for e in events)


@pytest.mark.parametrize("override", [None, "missing-model"])
def test_real_router_unavailable_default_is_rejected_by_runtime_and_evaluator(app, monkeypatch, override):
    for profile in app.model_registry.all(): profile.available = False
    app.router.default_adapter.profile().available = False
    app.controller.react.model.profile().available = False
    calls = forbid_fallback(app, monkeypatch)
    result = ModelRoutingEvaluator(app.router).evaluate_case(
        ModelRoutingCase("unavailable", expected_model="mock-1"))
    assert not result.ok and not result.predicted_model and "ValueError" in result.error
    task = app.controller.submit_task("现在几点了", intent="time_query", wait=True,
                                     model_override=override)
    assert_failed_route(app, task, source="router_rejected", error_class="ValueError")
    assert not calls
    assert "拒绝" in task.failure_reason


@pytest.mark.parametrize("exception", [ValueError, RuntimeError])
def test_router_errors_never_execute_controller_fallback(app, monkeypatch, exception):
    def reject(*args, **kwargs):
        raise exception("routing failed")
    monkeypatch.setattr(app.router, "route", reject)
    calls = forbid_fallback(app, monkeypatch)
    task = app.controller.submit_task("现在几点了", intent="time_query", wait=True)
    assert_failed_route(app, task, source="router_rejected" if exception is ValueError else "router_error",
                        error_class=exception.__name__)
    assert not calls


def test_second_route_rejection_clears_the_successful_planning_route(app, monkeypatch):
    original = app.router.route
    attempts = []
    def route(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return original(*args, **kwargs)
        raise ValueError("model unavailable after planning")
    monkeypatch.setattr(app.router, "route", route)
    calls = forbid_fallback(app, monkeypatch, after=lambda: len(attempts) > 1)
    task = app.controller.submit_task("现在几点了", intent="time_query", wait=True)
    assert len(attempts) == 2 and not calls
    assert_failed_route(app, task, source="router_rejected", error_class="ValueError")


def test_router_authorized_fallback_still_executes_normally(app, monkeypatch):
    # Resolving candidates can fail; the router itself may approve an available
    # fallback. Only the controller's unchecked exception path is forbidden.
    monkeypatch.setattr(app.router, "adapter_factory", lambda _: None)
    task = app.controller.submit_task("现在几点了", intent="time_query", wait=True)
    assert task.task_status == TaskStatus.SUCCEEDED
    assert task.runtime_manifest["model_route"]["source"] == "router"
    assert not any(e.event_type == "model_route_failed" for e in app.bus.query(task_id=task.task_id, limit=100))
