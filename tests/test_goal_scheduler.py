"""ALN-13-P：长期目标周期推进与负载守护测试。

覆盖卡片验收：停止/预算/授权持续有效、无进展停止接入、真实负载不饿死主任务；
以及调度状态的持久化、多进程租约接管与 fencing、同槽位重放不重复派发。
"""

import json
import time

import pytest

from evoagent.app import App
from evoagent.core.leases import LeaseLost
from evoagent.core.persistence import Database
from evoagent.core.task import Task, TaskStatus
from evoagent.goals.governance import GoalGovernor
from evoagent.goals.scheduler import GoalScheduler, SchedulerPolicy


@pytest.fixture
def app(tmp_path):
    application = App(data_dir=str(tmp_path))
    yield application
    application.db.close()


def ready(app, *, title="answer a general question", ref="bounded/v1", rules=None):
    governor = app.goal_governor
    if ref != "manual-review/v1":
        try:
            governor.policy(ref)
        except PermissionError:
            governor.install_policy(ref, rules or {}, actor="admin",
                                    reason="scheduler thresholds")
    deadline = time.time() + 3600
    goal = app.goals.create(title, actor="user",
                            success_criteria=["verified improvement"],
                            goal_type="long_term", deadline=deadline)
    goal = app.goals.authorize(
        goal["goal_id"],
        {"tools": [], "permissions": [], "max_risk": "low"},
        actor="admin", reason="explicit continuous grant", level="continuous",
        expected_revision=goal["revision"], expires_at=deadline - 100,
        no_progress_policy_ref=ref)
    return app.goals.transition(goal["goal_id"], "ready", actor="user",
                                reason="start",
                                expected_revision=goal["revision"])


def goal_tasks(app, goal_id):
    return app.db.list_tasks_by_goal(goal_id)


def test_policy_validation_rejects_bad_values():
    with pytest.raises(ValueError):
        SchedulerPolicy.parse({"max_steps_per_goal": 33})
    with pytest.raises(ValueError):
        SchedulerPolicy.parse({"interval_seconds": 0})
    with pytest.raises(ValueError):
        SchedulerPolicy.parse({"unknown_field": 1})
    with pytest.raises(ValueError):
        SchedulerPolicy.parse({"max_goals_per_tick": True})


def test_tick_advances_due_goal_and_persists_schedule(app):
    goal = ready(app)
    scheduler = GoalScheduler(app.goal_governor, bus=app.bus)
    report = scheduler.tick(now=time.time())
    assert report["ok"] is True
    assert report["reason"] is None
    assert report["load"]["busy"] == 0
    assert len(report["advances"]) == 1
    assert report["advances"][0]["goal_id"] == goal["goal_id"]
    assert report["advances"][0]["tasks"]
    assert len(goal_tasks(app, goal["goal_id"])) == 1
    row = app.db._conn.execute(
        "SELECT * FROM goal_schedules WHERE goal_id=?",
        (goal["goal_id"],)).fetchone()
    assert row["next_due_at"] > report["at"]
    assert row["lease_token"] is None
    assert json.loads(row["last_outcome"])["status"] == "advanced"


def test_primary_load_guard_yields_to_user_tasks(app):
    goal = ready(app)
    # 用户主任务尚未完成 → 长期目标不得抢占运行资源
    app.db.save_task(Task(user_goal="primary job"))
    scheduler = GoalScheduler(app.goal_governor)
    report = scheduler.tick(now=time.time())
    assert report["reason"] == "primary_load_guard"
    assert report["ok"] is True
    assert report["advances"] == []
    assert report["load"]["busy"] == 1
    assert goal_tasks(app, goal["goal_id"]) == []
    assert app.goals.get(goal["goal_id"])["status"] == "ready"


def test_paused_and_blocked_goals_are_not_advanced(app):
    goal = ready(app)
    paused = app.goals.transition(
        goal["goal_id"], "paused", actor="user", reason="hold",
        expected_revision=app.goals.get(goal["goal_id"])["revision"])
    scheduler = GoalScheduler(app.goal_governor)
    report = scheduler.tick(now=time.time())
    assert report["advances"] == []
    assert any("inactive:paused" in item["reason"] for item in report["skipped"])
    assert goal_tasks(app, goal["goal_id"]) == []
    assert app.goals.get(paused["goal_id"])["status"] == "paused"


def test_revoked_authorization_stops_advancement(app):
    goal = ready(app)
    app.goals.revoke(goal["goal_id"], actor="admin", reason="authorization withdrawn",
                     expected_revision=app.goals.get(goal["goal_id"])["revision"])
    scheduler = GoalScheduler(app.goal_governor)
    report = scheduler.tick(now=time.time())
    assert report["advances"] == []
    assert any("inactive:blocked" in item["reason"] for item in report["skipped"])
    assert goal_tasks(app, goal["goal_id"]) == []


def test_no_progress_stop_is_consumed_by_next_tick(app):
    """14-A 的停止条件把目标置 paused 后，下一轮只记录 stopped 不再派发。"""
    goal = ready(app, ref="manual-review/v1")
    # manual-review/v1: max_attempts=1 → 一次 advance 后 reconcile 会把目标置 paused
    scheduler = GoalScheduler(app.goal_governor,
                              policy={"max_steps_per_goal": 20})
    first = scheduler.tick(now=time.time())
    assert first["advances"], first
    assert first["advances"][0]["goal_status"] == "paused"
    second = scheduler.tick(now=time.time() + 120)
    assert second["advances"] == []
    assert any("inactive:paused" in item["reason"] for item in second["skipped"])


def test_same_slot_replay_does_not_dispatch_twice(app):
    goal = ready(app)
    scheduler = GoalScheduler(app.goal_governor)
    at = 2_000_000.0
    first = scheduler.tick(now=at)
    assert len(first["advances"][0]["tasks"]) == 1
    task_id = first["advances"][0]["tasks"][0]
    # 模拟崩溃后同一槽位重放：把下次到期拨回同一时刻
    app.db._conn.execute(
        "UPDATE goal_schedules SET next_due_at=? WHERE goal_id=?", (at, goal["goal_id"]))
    app.db._conn.commit()
    replay = scheduler.tick(now=at)
    assert replay["advances"][0]["command_id"] == first["advances"][0]["command_id"]
    assert replay["advances"][0]["tasks"] == [task_id]
    assert len(goal_tasks(app, goal["goal_id"])) == 1


def test_lease_takeover_and_fencing_on_schedule(app):
    goal = ready(app)
    at = 3_000_000.0
    holder = GoalScheduler(app.goal_governor, clock=lambda: at, owner_id="worker_a",
                           policy={"lease_ttl_seconds": 5})
    holder.sync(now=at)
    claim = holder._claim(goal["goal_id"], at)
    assert claim is not None and claim["attempts"] == 1
    # 另一进程在租约过期前领不到
    other = GoalScheduler(app.goal_governor, clock=lambda: at + 1, owner_id="worker_b",
                          policy={"lease_ttl_seconds": 5})
    assert other._claim(goal["goal_id"], at + 1) is None
    # 租约过期后可以接管，attempts 递增使旧凭据失效
    takeover = other._claim(goal["goal_id"], at + 10)
    assert takeover is not None and takeover["attempts"] == 2
    with pytest.raises(LeaseLost):
        holder._finish(claim, at, {"status": "advanced"}, True)
    other._finish(takeover, at + 10, {"status": "advanced"}, True)
    row = app.db._conn.execute(
        "SELECT * FROM goal_schedules WHERE goal_id=?",
        (goal["goal_id"],)).fetchone()
    assert row["lease_token"] is None and row["consecutive_failures"] == 0


def test_failure_backoff_is_persisted(app, monkeypatch):
    goal = ready(app)
    scheduler = GoalScheduler(app.goal_governor,
                              policy={"failure_backoff_seconds": 100})
    at = 4_000_000.0

    def boom(*args, **kwargs):
        raise RuntimeError("runtime exploded")

    monkeypatch.setattr(app.goal_governor, "advance", boom)
    report = scheduler.tick(now=at)
    assert report["ok"] is False and report["errors"]
    row = app.db._conn.execute(
        "SELECT * FROM goal_schedules WHERE goal_id=?",
        (goal["goal_id"],)).fetchone()
    assert row["consecutive_failures"] == 1
    assert row["next_due_at"] == pytest.approx(at + 100)


def test_schedule_survives_restart(tmp_path):
    app = App(data_dir=str(tmp_path))
    reopened = Database(str(tmp_path / "evoagent.db"))
    try:
        goal = ready(app)
        scheduler = GoalScheduler(app.goal_governor)
        scheduler.tick(now=5_000_000.0)
        # 新进程：新的连接 + 新的 governor/scheduler，调度状态来自库而非内存
        governor = GoalGovernor(reopened, controller=app.controller)
        restarted = GoalScheduler(governor)
        assert restarted.stats()["total"] == 1
        assert restarted.due(now=5_000_000.0 + 10_000)
        # 重启后同一槽位重放仍走 advance 幂等表，不新增任务
        reopened._conn.execute(
            "UPDATE goal_schedules SET next_due_at=? WHERE goal_id=?",
            (5_000_000.0, goal["goal_id"]))
        reopened._conn.commit()
        report = restarted.tick(now=5_000_000.0)
        assert report["advances"], report
        assert report["advances"][0]["command_id"] == (
            "sched:%s:%d" % (goal["goal_id"], int(5_000_000.0 // 60)))
        assert len(reopened.list_tasks_by_goal(goal["goal_id"])) == 1
    finally:
        reopened.close()
        app.db.close()
