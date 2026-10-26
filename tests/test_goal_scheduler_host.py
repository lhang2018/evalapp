"""ALN-13-S：长期目标调度宿主接线测试。

覆盖：App 宿主装配与可观测状态；单轮推进按持久到期执行；同槽位/重启不重复
派发；门控拒绝（授权/预算等）在报告与状态中可见；API 启停/tick 与 CLI 观测。
"""

import json
import time
from types import SimpleNamespace

import pytest

from evoagent.app import App
from evoagent.goals.scheduler import GoalScheduler


@pytest.fixture
def app(tmp_path):
    application = App(data_dir=str(tmp_path))
    yield application
    application.goal_scheduler.stop()
    application.db.close()


def ready(app, *, title="answer a general question", ref="bounded/v1"):
    governor = app.goal_governor
    try:
        governor.policy(ref)
    except PermissionError:
        governor.install_policy(ref, {}, actor="admin",
                                reason="scheduler host thresholds")
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


def test_app_wires_host_scheduler_with_status(app):
    scheduler = app.goal_scheduler
    assert isinstance(scheduler, GoalScheduler)
    status = scheduler.status()
    assert status["running"] is False
    assert status["ticks"] == 0
    assert status["last_report"] is None
    assert set(status) >= {"owner_id", "running", "stats", "load", "policy"}
    assert status["stats"]["total"] >= 0


def test_tick_advances_due_goal_and_status_observable(app):
    goal = ready(app)
    scheduler = app.goal_scheduler
    report = scheduler.tick(now=time.time())
    assert report["ok"] is True and report["reason"] is None
    assert len(report["advances"]) == 1
    assert report["advances"][0]["goal_id"] == goal["goal_id"]
    assert report["advances"][0]["tasks"]
    assert len(goal_tasks(app, goal["goal_id"])) == 1
    status = scheduler.status()
    assert status["ticks"] == 1
    assert status["last_tick_at"] == report["at"]
    assert status["last_report"]["tick_id"] == report["tick_id"]
    assert status["stats"]["next_due_at"] is not None


def test_restart_same_slot_does_not_duplicate_dispatch(tmp_path):
    moment = time.time()
    app1 = App(data_dir=str(tmp_path))
    try:
        goal = ready(app1)
        first = app1.goal_scheduler.tick(now=moment)
        assert len(first["advances"]) == 1
        assert len(goal_tasks(app1, goal["goal_id"])) == 1
        goal_id = goal["goal_id"]
    finally:
        app1.goal_scheduler.stop()
        app1.db.close()

    app2 = App(data_dir=str(tmp_path))
    try:
        scheduler = app2.goal_scheduler
        second = scheduler.tick(now=moment)
        assert second["advances"] == []
        assert len(goal_tasks(app2, goal_id)) == 1
        # 人为把到期时间拨回同槽位，验证 command_id 幂等不会重复派发
        app2.db._conn.execute(
            "UPDATE goal_schedules SET next_due_at=? WHERE goal_id=?",
            (moment, goal_id))
        app2.db._conn.commit()
        replay = scheduler.tick(now=moment)
        assert len(goal_tasks(app2, goal_id)) == 1, replay
    finally:
        app2.goal_scheduler.stop()
        app2.db.close()


def test_gate_refusal_is_visible_in_report_and_status(app):
    ready(app)
    scheduler = app.goal_scheduler

    def refuse(*args, **kwargs):
        raise PermissionError("预算不足，暂停主动推进")

    scheduler.governor = SimpleNamespace(
        db=app.db, goals=app.goals,
        reconcile=lambda goal_id: [],
        advance=refuse)
    report = scheduler.tick(now=time.time())
    assert report["ok"] is True
    assert report["stopped"], report
    assert "预算不足" in report["stopped"][0]["reason"]
    status = scheduler.status()
    assert status["last_report"]["stopped"][0]["goal_id"] == report["stopped"][0]["goal_id"]


def test_api_status_tick_start_stop(tmp_path):
    import threading
    import urllib.error
    import urllib.request
    from evoagent.api.server import ApiServer

    application = App(data_dir=str(tmp_path))
    application.config.api_token = "op"
    application.config.api_admin_token = "root"
    ready(application)
    server = ApiServer(application, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.port}"

    def call(method, path, payload=None, token="root"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(base + path, data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    try:
        status, payload = call("GET", "/goals/scheduler/status", token="op")
        assert status == 200 and payload["running"] is False
        status, payload = call("POST", "/goals/scheduler/tick",
                               {"now": time.time()})
        assert status == 200 and payload["report"]["advances"]
        status, _ = call("POST", "/goals/scheduler/tick", {"now": time.time()},
                         token="op")
        assert status == 403
        status, payload = call("POST", "/goals/scheduler/start",
                               {"poll_seconds": 5})
        assert status == 200 and payload["running"] is True
        status, payload = call("POST", "/goals/scheduler/stop",
                               {"timeout": 5})
        assert status == 200 and payload["running"] is False
    finally:
        application.goal_scheduler.stop()
        server.shutdown()
        application.db.close()


def test_cli_reports_tick_and_status(app, monkeypatch, capsys):
    from evoagent import cli
    ready(app)
    monkeypatch.setattr(cli, "_app", lambda: app)
    cli.cmd_goal_scheduler_tick()
    tick = json.loads(capsys.readouterr().out)
    assert tick["advances"], tick
    cli.cmd_goal_scheduler_status()
    status = json.loads(capsys.readouterr().out)
    assert status["ticks"] >= 1
    assert status["last_report"]["tick_id"] == tick["tick_id"]
