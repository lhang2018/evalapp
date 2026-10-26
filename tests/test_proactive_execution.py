"""ALN-25-C：主动任务准入执行与真实资源非干扰测试。

覆盖：defer/关闭/无高价值机会 → 零新增任务与外部动作；未知成本不按 0 处理；
预留成功→dispatch→submit；submit 回执未知保留预留、未派发失败释放；真实宿主 CPU 压力下
主动让出且核心任务状态不被改写。
"""

import time

import pytest

from evoagent.goals.proactive_execution import (ProactiveExecutionError,
                                                ProactiveExecutionService)
from evoagent.goals.proactivity import ProactivityGovernor, level_policy


class FakeReservations:
    def __init__(self, fail_reserve=None, fail_dispatch=None):
        self.reservations = {}
        self.refunds = []
        self.dispatched = []
        self.fail_reserve = fail_reserve
        self.fail_dispatch = fail_dispatch
        self.counter = 0

    def reserve(self, account_id, key, amounts, *, ttl_seconds, actor, reason):
        self.counter += 1
        if self.fail_reserve:
            raise self.fail_reserve
        reservation = {"reservation_id": f"res_{self.counter}", "amounts": dict(amounts),
                       "state": "reserved", "dispatched": False}
        self.reservations[reservation["reservation_id"]] = reservation
        return reservation

    def mark_dispatched(self, reservation_id, *, actor, reason):
        if self.fail_dispatch:
            raise self.fail_dispatch
        self.dispatched.append(reservation_id)
        self.reservations[reservation_id]["dispatched"] = True
        return self.get(reservation_id)

    def get(self, reservation_id):
        return self.reservations[reservation_id]

    def mark_unknown(self, reservation_id, **kwargs):
        self.reservations[reservation_id]["state"] = "unknown"
        return self.get(reservation_id)

    def refund(self, reservation_id, *, actor, reason):
        if self.get(reservation_id)["dispatched"]:
            raise ValueError("dispatched work requires reconciliation")
        self.refunds.append({"reservation_id": reservation_id, "reason": reason})
        self.reservations[reservation_id]["state"] = "refunded"
        return self.get(reservation_id)


def governor(level="normal"):
    from evoagent.core.resource_admission import ResourceGovernor
    return ProactivityGovernor(level=level, resource_governor=ResourceGovernor(
        lambda: {"cpu": 0.1, "memory": 0.1, "model_pool": 0.1}))


def service(governor_=None, *, reservations=None, submit=None):
    return ProactiveExecutionService(governor_ or governor(),
                                     reservations=reservations, submit=submit)


def test_no_high_value_opportunity_starts_nothing():
    calls = []
    svc = service(submit=lambda **kwargs: calls.append(kwargs) or {"task_id": "t1"})
    result = svc.attempt(actor="user", reason="主动帮助", opportunity_value=0)
    assert result["started"] is False
    assert result["reason"] == "no_high_value_opportunity"
    assert calls == [] and result["zero_new_non_essential_tasks"] is True


def test_off_level_blocks_with_notice_and_no_side_effects():
    calls = []
    svc = service(governor("off"), submit=lambda **kwargs: calls.append(kwargs))
    result = svc.attempt(actor="user", reason="主动帮助", opportunity_value=1.0)
    assert result["started"] is False and calls == []
    assert "off" in result["reason"]
    assert result["safety_notice"]
    assert result["zero_new_non_essential_tasks"] is True


def test_defer_from_resource_pressure_starts_nothing():
    from types import SimpleNamespace

    class Pressure:
        def admit(self, origin):
            return SimpleNamespace(decision="defer", reason="cpu_high",
                                   snapshot={"sample_valid": True})

    calls = []
    gov = ProactivityGovernor(level="normal", resource_governor=Pressure())
    svc = service(gov, submit=lambda **kwargs: calls.append(kwargs))
    result = svc.attempt(actor="user", reason="主动帮助", opportunity_value=1.0)
    assert result["started"] is False and calls == []
    assert "资源" in result["reason"] or "让位" in result["reason"]


def test_unknown_cost_is_refused_not_treated_as_zero():
    reservations = FakeReservations()
    calls = []
    svc = service(reservations=reservations,
                  submit=lambda **kwargs: calls.append(kwargs))
    result = svc.attempt(actor="user", reason="主动帮助",
                         opportunity_value=1.0, estimated_cost_known=False)
    assert result["started"] is False and result["reason"] == "cost_unknown"
    assert calls == [] and reservations.counter == 0


def test_success_reserves_dispatches_then_submits():
    reservations = FakeReservations()
    calls = []
    svc = service(reservations=reservations,
                  submit=lambda **kwargs: calls.append(kwargs) or {"task_id": "task_1"})
    result = svc.attempt(actor="user", reason="主动帮助", opportunity_value=2.0,
                         estimated_cost_known=True,
                         amounts={"model_calls": 1}, idempotency_key="k1",
                         opportunity_id="opp_1")
    assert result["started"] is True
    assert result["task_id"] == "task_1"
    assert reservations.dispatched == ["res_1"]
    assert calls and calls[0]["idempotency_key"] == "k1"
    assert result["core_state_touched"] is False


def test_submit_failure_keeps_unknown_reservation():
    reservations = FakeReservations()

    def submit(**kwargs):
        raise RuntimeError("existing channel rejected")

    svc = service(reservations=reservations, submit=submit)
    result = svc.attempt(actor="user", reason="主动帮助", opportunity_value=2.0,
                         amounts={"model_calls": 1}, estimated_cost_known=True,
                         idempotency_key="failure-1")
    assert result["started"] is False
    assert result["refunded"] is False
    assert reservations.get("res_1")["state"] == "unknown"
    assert reservations.refunds == []
    assert result["zero_new_non_essential_tasks"] is False


def test_reservation_or_dispatch_failure_starts_nothing():
    calls = []
    reserved = FakeReservations(fail_reserve=RuntimeError("budget_exceeded"))
    svc = service(reservations=reserved,
                  submit=lambda **kwargs: calls.append(kwargs))
    result = svc.attempt(actor="user", reason="主动帮助", opportunity_value=1.0,
                         amounts={"model_calls": 1}, estimated_cost_known=True,
                         idempotency_key="reserve-1")
    assert result["started"] is False and calls == []
    assert "reservation_failed" in result["reason"]

    dispatch_failed = FakeReservations(fail_dispatch=RuntimeError("lease_conflict"))
    svc2 = service(reservations=dispatch_failed,
                   submit=lambda **kwargs: calls.append(kwargs))
    result2 = svc2.attempt(actor="user", reason="主动帮助", opportunity_value=1.0,
                           amounts={"model_calls": 1}, estimated_cost_known=True,
                           idempotency_key="dispatch-1")
    assert result2["started"] is False and calls == []
    assert result2["refunded"] is True and dispatch_failed.refunds


def test_missing_submit_entry_does_not_acquire_reservation():
    reservations = FakeReservations()
    svc = service(reservations=reservations, submit=None)
    result = svc.attempt(actor="user", reason="主动帮助", opportunity_value=1.0,
                         amounts={"model_calls": 1}, estimated_cost_known=True)
    assert result["started"] is False
    assert result["refunded"] is False
    assert reservations.counter == 0


def test_invalid_arguments_rejected():
    svc = service()
    with pytest.raises(ProactiveExecutionError):
        svc.attempt(actor="", reason="x", opportunity_value=1.0)
    with pytest.raises(ProactiveExecutionError):
        svc.attempt(actor="u", reason="", opportunity_value=1.0)
    with pytest.raises(ProactiveExecutionError):
        svc.attempt(actor="u", reason="x", opportunity_value="bad")


def test_real_host_pressure_yields_and_core_state_untouched():
    """真实宿主 CPU 压力下：主动侧不启动，核心任务记录不被改写。"""
    import os
    import platform
    import subprocess
    import sys

    import psutil
    from evoagent.app import App
    from evoagent.core.resource_admission import ResourceGovernor
    from evoagent.core.task import Task, TaskStatus

    app = App(data_dir=str(__import__("tempfile").mkdtemp(prefix="aln25c_")))
    core = Task(user_goal="真实核心任务", task_status=TaskStatus.RUNNING)
    app.db.save_task(core)
    before_status = app.db.get_task(core.task_id).task_status

    def sampler():
        return {"cpu": psutil.cpu_percent(interval=0.5) / 100.0,
                "memory": psutil.virtual_memory().percent / 100.0,
                "model_pool": 0.0}

    resource = ResourceGovernor(sampler)
    proactivity = ProactivityGovernor(level="normal", resource_governor=resource)
    calls = []
    svc = ProactiveExecutionService(proactivity,
                                    submit=lambda **kwargs: calls.append(kwargs) or {"task_id": "proactive"})
    workers = max(2, os.cpu_count() or 4)
    procs = []
    try:
        for _ in range(workers):
            procs.append(subprocess.Popen(
                [sys.executable, "-c", "while True: pass"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1.5)
        result = svc.attempt(actor="user", reason="压力下主动帮助",
                             opportunity_value=1.0, estimated_cost_known=True)
    finally:
        for proc in procs:
            proc.kill()
        for proc in procs:
            proc.wait(timeout=5)
    after_status = app.db.get_task(core.task_id).task_status
    app.db.close()
    assert platform.system()
    assert result["started"] is False
    assert calls == []
    assert before_status == TaskStatus.RUNNING
    assert after_status == before_status
    assert result["core_state_touched"] is False

def _ready_goal(app):
    import time as _time
    governor = app.goal_governor
    try:
        governor.policy("bounded/v1")
    except PermissionError:
        governor.install_policy("bounded/v1", {}, actor="admin", reason="25-C")
    deadline = _time.time() + 3600
    goal = app.goals.create("主动建议目标", actor="user",
                            success_criteria=["verified improvement"],
                            goal_type="long_term", deadline=deadline)
    goal = app.goals.authorize(
        goal["goal_id"], {"tools": [], "permissions": [], "max_risk": "low"},
        actor="admin", reason="25-C grant", level="continuous",
        expected_revision=goal["revision"], expires_at=deadline - 100,
        no_progress_policy_ref="bounded/v1")
    return app.goals.transition(goal["goal_id"], "ready", actor="user",
                                reason="start", expected_revision=goal["revision"])


def test_app_opportunity_task_requires_authorized_goal(tmp_path):
    from evoagent.app import App

    app = App(data_dir=str(tmp_path))
    try:
        _configure_host(app)
        result = app.proactive_execution.attempt(
            actor="user", reason="主动帮助", opportunity_value=1.0,
            estimated_cost_known=True, amounts={"model_calls": 1}, idempotency_key="unauthorized",
            submit_payload={"goal": "检查项目风险"})
        assert result["started"] is False
        assert "submit_failed" in result["reason"]
    finally:
        app.db.close()


def test_app_wires_attempt_to_authorized_goal_opportunity_task(tmp_path):
    from evoagent.app import App

    app = App(data_dir=str(tmp_path))
    try:
        _configure_host(app)
        goal = _ready_goal(app)
        result = app.proactive_execution.attempt(
            actor="user", reason="主动帮助", opportunity_value=1.0,
            estimated_cost_known=True, amounts={"model_calls": 1}, idempotency_key="authorized",
            submit_payload={"goal": "检查项目风险", "goal_id": goal["goal_id"]})
        assert result["started"] is True, result
        task = app.db.get_task(result["task_id"])
        assert task is not None
        assert getattr(task, "origin", "") == "opportunity"
        # Durable Goal commands prevent duplicate submission after a restart.
        replay = app._submit_proactive_task(actor="user", reason="retry",
            idempotency_key="authorized", opportunity_id="",
            payload={"goal": "检查项目风险", "goal_id": goal["goal_id"]})
        assert replay["task_id"] == task.task_id
        before = len(app.db.list_tasks(limit=2000))
        app.proactivity.set_level("off")
        denied = app.proactive_execution.attempt(
            actor="user", reason="主动帮助", opportunity_value=1.0,
            submit_payload={"goal": "检查项目风险 2", "goal_id": goal["goal_id"]})
        assert denied["started"] is False
        assert len(app.db.list_tasks(limit=2000)) == before
        assert denied["safety_notice"]
    finally:
        app.db.close()


def _configure_host(app):
    from evoagent.core.resource_admission import ResourceGovernor
    app.proactivity.resource_governor = ResourceGovernor(
        lambda: {"cpu": 0.1, "memory": 0.1, "model_pool": 0.1})
    store = app.ledger.reservation_store()
    store.create_account("proactive", {"model_calls": 10}, kind="pool", reference="proactive",
                         currency="USD", actor="host", reason="25-C component test")
    app.proactive_execution.reservations = store
    app.proactive_execution.account_id = "proactive"


def test_app_sampled_pressure_denial_preserves_persisted_core_task(tmp_path):
    from evoagent.app import App
    from evoagent.core.resource_admission import ResourceGovernor
    from evoagent.core.task import Task, TaskStatus
    app = App(data_dir=str(tmp_path))
    try:
        _configure_host(app)
        core = Task(user_goal="核心任务", task_status=TaskStatus.RUNNING)
        app.db.save_task(core)
        before = len(app.db.list_tasks(limit=2000))
        app.proactivity.resource_governor = ResourceGovernor(
            lambda: {"cpu": 0.99, "memory": 0.2, "model_pool": 0.1})
        result = app.proactive_execution.attempt(actor="host", reason="test",
            opportunity_value=1, estimated_cost_known=True,
            amounts={"model_calls": 1}, idempotency_key="pressure")
        assert result["started"] is False
        assert len(app.db.list_tasks(limit=2000)) == before
        assert app.db.get_task(core.task_id).task_status == TaskStatus.RUNNING
        assert app.proactive_execution.reservations.balance("proactive")["held"] == {"model_calls": 0}
    finally:
        app.db.close()
