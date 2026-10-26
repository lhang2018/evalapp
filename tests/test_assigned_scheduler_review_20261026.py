"""Assigned-review probes use real ledgers beyond former UI/list windows."""
import json
import time

import pytest

from evoagent.app import App
from evoagent.core.task import Task, TaskStatus
from evoagent.goals.scheduler import SchedulerPolicy


@pytest.fixture
def app(tmp_path):
    value = App(data_dir=str(tmp_path))
    yield value
    value.goal_scheduler.stop()
    value.memory.close()
    value.db.close()


def ready(app):
    app.goal_governor.install_policy("assigned/v1", {}, actor="review", reason="test")
    deadline = time.time() + 3600
    goal = app.goals.create("answer a general question", actor="review",
        goal_type="long_term", deadline=deadline, success_criteria=["verified improvement"])
    goal = app.goals.authorize(goal["goal_id"],
        {"tools": [], "permissions": [], "max_risk": "low"}, actor="review", reason="test",
        level="continuous", expected_revision=goal["revision"], expires_at=deadline - 100,
        no_progress_policy_ref="assigned/v1")
    return app.goals.transition(goal["goal_id"], "ready", actor="review", reason="test",
                                expected_revision=goal["revision"])


def add_terminal_tasks(app, count=501):
    rows = []
    for i in range(count):
        task = Task(task_id=f"recent_terminal_{i}", user_goal="synthetic terminal row",
                    task_status=TaskStatus.SUCCEEDED, created_at=100 + i)
        rows.append((task.task_id, task.task_status.value, task.created_at, json.dumps(task.to_dict())))
    with app.db._lock, app.db._conn:
        app.db._conn.executemany("INSERT INTO tasks(task_id,status,created_at,data) VALUES (?,?,?,?)", rows)


def test_old_running_primary_beyond_500_still_blocks_and_then_recovers(app):
    goal = ready(app)
    primary = Task(user_goal="old user job", task_status=TaskStatus.RUNNING, created_at=1)
    app.db.save_task(primary)
    add_terminal_tasks(app)
    result = app.goal_scheduler.tick(now=time.time())
    assert result["reason"] == "primary_load_guard"
    assert result["load"]["busy"] == 1
    assert primary.task_id in result["load"]["primary_task_ids"]
    assert app.db.list_tasks_by_goal(goal["goal_id"]) == []
    assert app.db.get_task(primary.task_id).task_status == TaskStatus.RUNNING
    primary.task_status = TaskStatus.SUCCEEDED
    app.db.save_task(primary)
    resumed = app.goal_scheduler.tick(now=time.time())
    assert resumed["reason"] is None and len(resumed["advances"]) == 1
    assert len(app.db.list_tasks_by_goal(goal["goal_id"])) == 1


def test_old_derived_in_flight_is_counted_beyond_500(app):
    task = Task(user_goal="old derived", task_status=TaskStatus.RUNNING,
                origin="derived", created_at=1)
    app.db.save_task(task)
    add_terminal_tasks(app)
    assert app.goal_scheduler.primary_load()["derived_in_flight"] == 1


def test_live_goal_beyond_1000_terminal_goals_is_registered(app):
    target = app.goals.create("synthetic active goal", actor="review", goal_id="zz_target",
                              success_criteria=["synthetic verification"])
    rows = []
    for i in range(1001):
        record = dict(target, goal_id=f"aa_{i:04}", status="completed")
        rows.append((record["goal_id"], record["owner"], record["status"],
                     record["revision"], json.dumps(record)))
    with app.db._lock, app.db._conn:
        app.db._conn.executemany("INSERT INTO goals(goal_id,owner,status,revision,data) VALUES (?,?,?,?,?)", rows)
    assert app.goal_scheduler.sync(now=time.time()) == 1
    row = app.db._conn.execute("SELECT goal_id FROM goal_schedules").fetchall()
    assert [r["goal_id"] for r in row] == [target["goal_id"]]


@pytest.mark.parametrize("field", ["interval_seconds", "lease_ttl_seconds",
                                  "failure_backoff_seconds", "max_backoff_seconds"])
@pytest.mark.parametrize("direct", [False, True])
def test_infinite_policy_cannot_create_immortal_schedule_or_lease(field, direct):
    with pytest.raises(ValueError):
        if direct:
            SchedulerPolicy(**{field: float("inf")})
        else:
            SchedulerPolicy.parse({field: float("inf")})


@pytest.mark.parametrize("method", ["sync", "tick"])
@pytest.mark.parametrize("now", [float("inf"), float("nan"), True])
def test_invalid_schedule_time_is_rejected_before_persisting(app, method, now):
    app.goals.create("synthetic pending", actor="review", success_criteria=["synthetic verification"])
    with pytest.raises(ValueError):
        getattr(app.goal_scheduler, method)(now=now)
    assert app.db._conn.execute("SELECT COUNT(*) FROM goal_schedules").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["off", "high_pressure", "invalid_sample"])
def test_real_app_scheduler_consumes_off_and_resource_admission_before_creating_tasks(app, mode):
    from evoagent.core.resource_admission import ResourceGovernor
    goal = ready(app)
    if mode == "off":
        app.proactivity.set_level("off")
    else:
        sample = {"cpu": 0.99, "memory": 0.1, "model_pool": 0.1} if mode == "high_pressure" else {}
        app.proactivity.resource_governor = ResourceGovernor(lambda: sample)
    report = app.goal_scheduler.tick(now=time.time())
    assert report["reason"] == "proactivity_guard"
    assert report["admission"]["allowed"] is False
    assert app.db.list_tasks_by_goal(goal["goal_id"]) == []
    if mode == "off":
        assert report["admission"]["safety_notice"]
        app.proactivity.set_level("normal")
        resumed = app.goal_scheduler.tick(now=time.time())
        assert len(resumed["advances"]) == 1


@pytest.mark.parametrize("now", [True, float("inf"), "not-a-time"])
def test_api_does_not_coerce_invalid_time_before_scheduler_validation(app, now):
    from types import SimpleNamespace
    from evoagent.api.goals_api import goal_scheduler_tick
    ready(app)
    responses = []
    ctx = SimpleNamespace(app=app, body={"now": now},
        auth=SimpleNamespace(role="admin"), json=lambda status, payload: responses.append((status, payload)))
    goal_scheduler_tick(ctx)
    assert responses[0][0] == 400
    assert app.db._conn.execute("SELECT COUNT(*) FROM goal_schedules").fetchone()[0] == 0
