"""ALN-15-R: actual controller/model/tool consumers and server-owned binding."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import uuid
import multiprocessing

import pytest

from evoagent.app import App
from evoagent.core.budget import BudgetController, BudgetExceeded, BudgetLimits
from evoagent.core.runtime_budget import model_call, identity, experiment_scope
from evoagent.core.task import Task, TaskStatus
from evoagent.models.base import Action, ModelProfile
from evoagent.models.metering import MeteredModel
from evoagent.tools.contract import ToolContract, ToolResult


class Provider:
    def __init__(self, *, free=False, unknown=False, report_usage=True, fail=False):
        self.calls = 0
        self.last_usage = None
        self.report_usage, self.fail = report_usage, fail
        self._profile = ModelProfile("runtime-test", pricing_known=not unknown,
            price_per_1k_input=0 if free else 1, price_per_1k_output=0 if free else 1)

    def profile(self):
        return self._profile

    def estimate_cost(self, *_):
        return 0 if self._profile.price_per_1k_input == 0 else .002

    def _call(self):
        self.calls += 1
        self.last_usage = {"input_tokens": 1, "output_tokens": 1} if self.report_usage else None
        if self.fail:
            raise TimeoutError("request may have been billed")

    def generate(self, messages):
        self._call()
        if "意图分类" in messages[0].content:
            return json.dumps({"intent": "general", "domain": "general", "confidence": 1})
        return json.dumps({"steps": [{"step_id": "s1", "description": "answer", "deps": []}]})

    def suggest_action(self, messages, available_tools):
        self._call()
        return Action(kind="finish", answer="verified answer")


@pytest.fixture
def app(tmp_path):
    application = App(data_dir=str(tmp_path))
    application.permissions.high_risk_mode = "allow"
    yield application
    application.db.close()


def install(app, provider):
    metered = MeteredModel(provider, app.bus)
    app.model_registry.register(provider.profile())
    app.router.default_adapter = metered
    app.router.default_model_id = provider.profile().model_id
    app.router.setup(lambda _: metered)
    app.classifier.model = metered
    app.planner.model = metered
    app.react.model = metered
    return metered


def goal(app, *, parent=None, calls=6, cost=1):
    deadline, expiry = time.time() + 3600, time.time() + 1800
    if parent:
        outer = app.goals.get(parent)
        grant = json.loads(app.db._conn.execute("SELECT data FROM goal_grants WHERE authorization_ref=?",
                           (outer["authorization_ref"],)).fetchone()[0])
        deadline = min(deadline, outer["deadline"])
        expiry = min(expiry, grant["expires_at"])
    g = app.goals.create("bounded goal", actor="user", deadline=deadline,
        success_criteria=["answer"], parent_goal_id=parent)
    g = app.goals.authorize(g["goal_id"],
        {"tools": ["clock"], "permissions": [], "max_risk": "critical"},
        actor="admin", reason="explicit authority", level="execution",
        expected_revision=g["revision"], expires_at=expiry,
        budget_policy={"max_model_calls": calls, "max_cost": cost})
    return app.goals.transition(g["goal_id"], "ready", actor="user", reason="start",
                                expected_revision=g["revision"])


def bound(app, goal_id="", extras=None):
    task = Task(user_goal="bounded request", goal_id=goal_id)
    if goal_id:
        app.goals.bind_task(task)
    task.current_run_id = "run_" + uuid.uuid4().hex
    task.task_status = TaskStatus.RUNNING
    task.runtime_manifest.update(extras or {})
    app.db.save_task(task)
    return task, app.controller._make_budget(task)


def records(app, task):
    rows = app.db._conn.execute("SELECT data FROM budget_reservations ORDER BY rowid").fetchall()
    return [json.loads(r[0]) for r in rows if json.loads(r[0]).get("context", {}).get("task_id") == task.task_id]


def call(budget, adapter):
    from evoagent.models.base import ChatMessage
    return model_call(budget, adapter, "generate", [ChatMessage("user", "bounded")])


def test_real_controller_classification_planning_and_react_each_allocate_once(app):
    provider = Provider()
    install(app, provider)
    task = app.controller.submit_task("answer a general question", wait=True)
    assert task.task_status == TaskStatus.SUCCEEDED
    log = records(app, task)
    assert [r["context"]["phase"] for r in log] == ["classification", "planning", "react"]
    assert provider.calls == 3
    assert all(r["state"] == "settled" for r in log)
    assert all(r["actual"]["cost_micros"] == 2000 for r in log)
    account = task.runtime_manifest["aggregate_budget"]["account_id"]
    assert app.ledger.reservation_store().balance(account)["spent"]["model_calls"] == 3
    assert app.ledger.usage("runtime")["model_calls"] == 3
    assert app.ledger.usage("runtime")["cost"] == pytest.approx(.006)


def test_explicit_hint_does_not_allocate_a_phantom_classifier_call(app):
    provider = Provider(free=True)
    install(app, provider)
    task = app.controller.submit_task("answer", intent="general", wait=True)
    assert provider.calls == 2
    assert [r["context"]["phase"] for r in records(app, task)] == ["planning", "react"]


def test_two_real_tasks_share_one_goal_allocation(app):
    provider = Provider(free=True)
    install(app, provider)
    g = goal(app, calls=1)
    barrier = threading.Barrier(2)
    def run(_):
        barrier.wait()
        return app.controller.submit_task("answer", goal_id=g["goal_id"], intent="general", wait=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = list(pool.map(run, range(2)))
    assert provider.calls == 1
    assert all(t.task_status == TaskStatus.FAILED for t in tasks)
    account = identity("goal", g["goal_id"])
    assert app.ledger.reservation_store().balance(account)["spent"]["model_calls"] == 1


def test_sibling_goal_and_separate_day_pool_cannot_be_bypassed(app):
    provider = install(app, Provider(free=True))
    root = goal(app, calls=1)
    a = goal(app, parent=root["goal_id"], calls=1)
    b = goal(app, parent=root["goal_id"], calls=1)
    _, first = bound(app, a["goal_id"])
    _, second = bound(app, b["goal_id"])
    call(first, provider)
    with pytest.raises(BudgetExceeded):
        call(second, provider)
    assert second.runtime_binding.data["goal_ids"] == [root["goal_id"], b["goal_id"]]


def test_different_goals_compete_for_atomic_runtime_day_capacity(app):
    app.config.budget.runtime_daily_model_calls = 1
    provider = install(app, Provider(free=True))
    a, b = goal(app), goal(app)
    _, first = bound(app, a["goal_id"])
    _, second = bound(app, b["goal_id"])
    call(first, provider)
    with pytest.raises(BudgetExceeded):
        call(second, provider)


def test_forged_manifest_does_not_choose_accounts_or_experiments(app):
    fake = {"aggregate_budget": {"account_id": "unlimited"}, "candidate_id": "forged"}
    task, budget = bound(app, extras=fake)
    assert budget.runtime_binding.data["account_id"] == identity("task", task.task_id)
    assert not any(r.startswith("experiment:") for r in budget.runtime_binding.data["additional_accounts"])


def test_unknown_pricing_without_ceiling_is_rejected_before_provider(app):
    provider = Provider(unknown=True)
    metered = install(app, provider)
    _, budget = bound(app)
    with pytest.raises(BudgetExceeded, match="explicit reservation ceiling"):
        call(budget, metered)
    assert provider.calls == 0
    assert budget.usage.model_calls == 0


def test_unknown_price_with_ceiling_keeps_hold_and_stops_followup(app):
    app.config.budget.unknown_model_reservation_cost = .1
    provider = Provider(unknown=True)
    metered = install(app, provider)
    task, budget = bound(app)
    with pytest.raises(BudgetExceeded):
        call(budget, metered)
    log = records(app, task)
    assert log[0]["state"] == "unknown"
    assert log[0]["amounts"]["cost_micros"] == 100000
    assert budget.usage.unknown_model_costs == 1
    with pytest.raises(BudgetExceeded):
        call(budget, metered)
    assert provider.calls == 1


@pytest.mark.parametrize("fail", [False, True])
def test_missing_provider_usage_or_timeout_never_settles_estimate_as_actual(app, fail):
    provider = Provider(report_usage=False, fail=fail)
    metered = install(app, provider)
    task, budget = bound(app)
    with pytest.raises((BudgetExceeded, TimeoutError)):
        call(budget, metered)
    assert records(app, task)[0]["state"] == "unknown"


def test_budget_denial_is_not_swallowed_by_classifier_fallback(app):
    provider = Provider(unknown=True)
    install(app, provider)
    task = app.controller.submit_task("answer", wait=True)
    assert task.task_status == TaskStatus.FAILED
    assert provider.calls == 0
    assert "pricing" in task.failure_reason
    assert "控制器异常" not in task.failure_reason
    assert any(e.event_type == "task_budget_exhausted" for e in app.bus.query(task_id=task.task_id))


def test_classification_unknown_cost_remains_visible_without_verdict_evidence(app):
    from evoagent.core.budget import CostReporter
    from evoagent.evolution.monitor import collect_metrics
    app.config.budget.unknown_model_reservation_cost = .1
    provider = Provider(unknown=True)
    install(app, provider)
    task = app.controller.submit_task("answer", wait=True)
    assert task.task_status == TaskStatus.FAILED
    assert provider.calls == 1
    assert not task.result_evidence
    report = CostReporter(app.db).report()
    assert report["totals"]["model_calls"] == 1
    assert report["totals"]["unknown_model_costs"] == 1
    assert report["totals"]["cost_complete"] is False
    assert collect_metrics(app.db, 0, time.time() + 10)["unknown_cost_samples"] == 1


def test_durable_usage_overrides_older_verdict_snapshot(app):
    from evoagent.core.budget import CostReporter
    provider = install(app, Provider())
    task, budget = bound(app)
    task.result_evidence = [{"budget": {"usage": {"cost": 0, "model_calls": 0}}}]
    app.db.save_task(task)
    call(budget, provider)
    report = CostReporter(app.db).report()
    assert report["totals"]["cost"] == pytest.approx(.002)
    assert report["totals"]["model_calls"] == 1


def test_revocation_between_hold_and_dispatch_refunds_without_calling_provider(app):
    metered = install(app, Provider(free=True))
    g = goal(app)
    task, budget = bound(app, g["goal_id"])
    def revoke(inflight):
        if inflight:
            current = app.goals.get(g["goal_id"])
            app.goals.revoke(g["goal_id"], actor="user", reason="stop", expected_revision=current["revision"])
    with pytest.raises(PermissionError):
        model_call(budget, metered, "generate", [], on_pre_dispatch=revoke)
    assert metered.inner.calls == 0
    assert records(app, task)[0]["state"] == "refunded"


def test_restart_reuses_same_accounts_and_fences_unfinished_previous_run(app):
    metered = install(app, Provider())
    task, budget = bound(app)
    hold = budget.runtime_binding.begin(budget, "model", "runtime-test", 10000, phase="react")
    hold.dispatch()
    task.current_run_id = "replacement-run"
    app.db.save_task(task)
    restored = app.controller._make_budget(task)
    assert restored.runtime_binding.data["account_id"] == budget.runtime_binding.data["account_id"]
    with pytest.raises(BudgetExceeded):
        call(restored, metered)
    assert app.ledger.reservation_store().get(hold.id)["state"] == "unknown"
    assert metered.inner.calls == 0


def test_unused_previous_run_hold_is_reclaimed_before_new_call(app):
    metered = install(app, Provider(free=True))
    task, budget = bound(app)
    hold = budget.runtime_binding.begin(budget, "model", "runtime-test", 10000, phase="react")
    task.current_run_id = "replacement-run"
    app.db.save_task(task)
    call(app.controller._make_budget(task), metered)
    assert app.ledger.reservation_store().get(hold.id)["state"] == "refunded"


def test_tool_call_is_allocated_and_correlated_to_effect(app):
    task, budget = bound(app)
    app.tools.register(ToolContract("clock", side_effect_level="local", retry_policy=0),
                       lambda _: ToolResult(ok=True, data={"done": True}, submitted=True, executed=True, contract_valid=True))
    result = app.tools.execute("clock", {}, budget=budget, task_id=task.task_id, run_id=task.current_run_id)
    assert result.ok
    r = records(app, task)[0]
    assert r["context"]["effect_id"] == result.evidence["effect_id"]
    assert r["state"] == "settled" and r["actual"]["tool_calls"] == 1
    assert app.ledger.usage("runtime")["tool_calls"] == 1


def test_fixed_price_tool_settles_money_without_incrementing_calls_twice(app):
    task, budget = bound(app)
    app.tools.register(ToolContract("clock", requires_network=True,
        cost_model={"cost_per_call": .01, "currency": "USD"}), lambda _: ToolResult(ok=True, data="done", submitted=True, executed=True, contract_valid=True))
    result = app.tools.execute("clock", {}, budget=budget, task_id=task.task_id)
    assert result.ok
    assert budget.usage.tool_calls == 1 and budget.usage.cost == pytest.approx(.01)
    assert records(app, task)[0]["actual"]["cost_micros"] == 10000
    assert app.ledger.usage("runtime")["cost"] == pytest.approx(.01)


def test_network_tool_unknown_price_stops_before_runner(app):
    task, budget = bound(app)
    invoked = []
    app.tools.register(ToolContract("clock", requires_network=True),
        lambda _: invoked.append(1) or ToolResult(ok=True, data="done", submitted=True, executed=True, contract_valid=True))
    result = app.tools.execute("clock", {}, budget=budget, task_id=task.task_id)
    assert not result.ok and result.error_class == "budget_exceeded"
    assert not invoked


def test_unknown_tool_billing_retains_actual_effect_result_and_pending_fee(app):
    app.config.budget.unknown_tool_reservation_cost = .1
    task, budget = bound(app)
    app.tools.register(ToolContract("clock", requires_network=True, side_effect_level="local"),
        lambda _: ToolResult(ok=True, data="actual effect", submitted=True, executed=True, contract_valid=True))
    result = app.tools.execute("clock", {}, budget=budget, task_id=task.task_id)
    assert result.ok and not result.side_effect_uncertain
    assert result.evidence["cost_reconciliation_required"]
    assert budget.usage.unknown_tool_costs == 1
    assert records(app, task)[0]["state"] == "unknown"
    assert app.tools.effects.get(result.evidence["effect_id"])["status"] == "confirmed"


def test_experiment_context_is_server_owned_and_shared_between_case_tasks(app):
    metered = install(app, Provider(free=True))
    experiment = BudgetController(BudgetLimits(max_model_calls=1), ledger=app.ledger,
                                  ledger_scope="candidate:trusted")
    with experiment_scope(experiment):
        _, first = bound(app)
        _, second = bound(app)
    call(first, metered)
    with pytest.raises(BudgetExceeded):
        call(second, metered)
    experiment_id = identity("experiment", "candidate:trusted")
    assert experiment_id in first.runtime_binding.data["additional_accounts"]
    assert experiment_id in second.runtime_binding.data["additional_accounts"]


def test_goal_reauthorization_does_not_reset_spent_or_expand_via_new_task(app):
    metered = install(app, Provider(free=True))
    g = goal(app, calls=4)
    _, first = bound(app, g["goal_id"])
    call(first, metered)
    current = app.goals.get(g["goal_id"])
    app.goals.authorize(g["goal_id"], {"tools": ["clock"], "permissions": [], "max_risk": "critical"},
        level="execution", actor="admin", reason="narrow total", expected_revision=current["revision"],
        expires_at=time.time() + 600, budget_policy={"max_model_calls": 1})
    _, second = bound(app, g["goal_id"])
    with pytest.raises(BudgetExceeded):
        call(second, metered)
    assert metered.inner.calls == 1


def test_multiple_root_allocation_rolls_back_all_roots_on_denial(app):
    store = app.ledger.reservation_store()
    for name, maximum in [("a", 10), ("b", 0)]:
        store.create_account(name, {"model_calls": maximum}, kind="pool", reference=name,
                             currency="USD", actor="host", reason="test")
    with pytest.raises(BudgetExceeded):
        store.reserve("a", "one", {"model_calls": 1}, additional_accounts=["b"],
                      ttl_seconds=1, actor="host", reason="test")
    assert store.balance("a")["held"]["model_calls"] == 0


def test_host_day_pool_migration_does_not_reset_existing_usage(app):
    app.config.budget.runtime_daily_model_calls = 1
    app.ledger.record("runtime", model_calls=1)
    metered = install(app, Provider(free=True))
    _, budget = bound(app)
    with pytest.raises(BudgetExceeded):
        call(budget, metered)
    assert metered.inner.calls == 0
    assert app.ledger.usage("runtime")["model_calls"] == 1


def test_old_goal_with_unmigrated_spending_cannot_receive_fresh_total(app):
    g = goal(app)
    old = Task(user_goal="legacy", goal_id=g["goal_id"])
    app.goals.bind_task(old)
    app.db.save_execution_budget(old.task_id, "old-run",
        {"limits": {}, "usage": {"model_calls": 1, "tool_calls": 0, "cost": 0}})
    with pytest.raises(BudgetExceeded, match="migration"):
        bound(app, g["goal_id"])


def test_legacy_task_imports_spending_once_without_billing_day_pool_again(app):
    from evoagent.core.budget import BudgetUsage
    old = Task(user_goal="legacy", task_status=TaskStatus.RUNNING, current_run_id="old-run")
    app.db.save_task(old)
    app.db.save_execution_budget(old.task_id, "old-run", {"limits": {}, "usage":
        {"model_calls": 2, "tool_calls": 0, "cost": .01}})
    app.ledger.record("runtime", model_calls=2, cost=.01)
    first = app.controller._make_budget(old)
    restored = app.controller._make_budget(old)
    assert first.runtime_binding.data == restored.runtime_binding.data
    account = restored.runtime_binding.data["account_id"]
    store = app.ledger.reservation_store()
    assert store.balance(account)["spent"]["model_calls"] == 2
    pool = restored.runtime_binding.data["additional_accounts"][0]
    assert store.balance(pool)["spent"]["model_calls"] == 0
    assert store.balance(pool)["limits"]["model_calls"] == app.config.budget.runtime_daily_model_calls - 2
    assert app.ledger.usage("runtime")["cost"] == .01


def test_trusted_known_free_calls_can_use_zero_money_limit(app):
    app.config.budget.task_max_cost = 0
    app.config.budget.runtime_daily_cost = 0
    metered = install(app, Provider(free=True))
    task, budget = bound(app)
    call(budget, metered)
    r = records(app, task)[0]
    assert r["known_free"] and r["actual"]["cost_micros"] == 0
    assert r["actual"]["model_calls"] == 1


def test_provider_overrun_is_never_capped_to_hold_or_hidden(app):
    provider = Provider()
    provider.profile().adapter_config["reservation_max_cost"] = .001
    metered = install(app, provider)
    g = goal(app, cost=.001)
    task, budget = bound(app, g["goal_id"])
    call(budget, metered)
    r = records(app, task)[0]
    assert r["amounts"]["cost_micros"] == 1000
    assert r["actual"]["cost_micros"] == 2000
    assert r["overrun"]["cost_micros"] == 1000
    with pytest.raises(BudgetExceeded):
        call(budget, metered)
    assert provider.calls == 1


def test_host_limit_reduction_is_rechecked_at_dispatch(app):
    metered = install(app, Provider(free=True))
    task, budget = bound(app)
    def narrow(inflight):
        if inflight:
            app.config.budget.runtime_daily_model_calls = 0
    with pytest.raises(BudgetExceeded):
        model_call(budget, metered, "generate", [], on_pre_dispatch=narrow)
    assert metered.inner.calls == 0
    assert records(app, task)[0]["state"] == "refunded"


def test_explicit_billing_is_not_overwritten_by_local_free_declaration(app):
    task, budget = bound(app)
    app.tools.register(ToolContract("clock"), lambda _: ToolResult(ok=True, executed=True,
        submitted=True, contract_valid=True, evidence={"billing": {"known": True,
            "cost": .01, "currency": "USD", "receipt_ref": "actual:receipt"}}))
    result = app.tools.execute("clock", {}, budget=budget, task_id=task.task_id)
    assert result.ok
    assert records(app, task)[0]["actual"]["cost_micros"] == 10000
    assert budget.usage.cost == pytest.approx(.01)


def test_multi_root_currency_mismatch_rejects_before_allocation(app):
    store = app.ledger.reservation_store()
    for name, currency in [("usd", "USD"), ("cny", "CNY")]:
        store.create_account(name, {"model_calls": 3}, kind="pool", reference=name,
                             currency=currency, actor="host", reason="test")
    with pytest.raises(ValueError, match="currency"):
        store.reserve("usd", "one", {"model_calls": 1}, additional_accounts=["cny"],
                      ttl_seconds=1, actor="host", reason="test")
    assert store.balance("usd")["held"]["model_calls"] == 0


def test_traffic_recovery_reclaims_unissued_expired_holds_without_manual_scan(app):
    metered = install(app, Provider(free=True))
    task, budget = bound(app)
    hold = budget.runtime_binding.begin(budget, "model", "runtime-test", 1, phase="react")
    now = time.time()
    budget.runtime_binding.store.clock = lambda: now + 1000
    call(budget, metered)
    assert app.ledger.reservation_store().get(hold.id)["state"] == "refunded"
    assert any(e.event_type == "budget_reservations_recovered" for e in app.bus.query())


def test_controller_startup_automatically_recovers_expired_hold(app):
    task, budget = bound(app)
    hold = budget.runtime_binding.begin(budget, "model", "runtime-test", 1, phase="react")
    store = budget.runtime_binding.store
    expired = time.time() - 1000
    with app.db._lock:
        data = store.get(hold.id)
        data["expires_at"] = expired
        app.db._conn.execute("UPDATE budget_reservations SET expires_at=?,data=? WHERE reservation_id=?",
                             (expired, json.dumps(data), hold.id))
        app.db._conn.commit()
    reopened = App(data_dir=str(app.config.data_dir))
    try:
        assert reopened.ledger.reservation_store().get(hold.id)["state"] == "refunded"
        assert any(e.event_type == "budget_reservations_recovered" for e in reopened.bus.query())
    finally:
        reopened.db.close()


def test_new_call_crossing_midnight_uses_new_day_but_keeps_task_total(app, monkeypatch):
    metered = install(app, Provider(free=True))
    task, budget = bound(app)
    previous_pool = budget.runtime_binding.data["additional_accounts"][0]
    call(budget, metered)
    import evoagent.core.runtime_budget as runtime
    original = runtime.time.strftime
    monkeypatch.setattr(runtime.time, "strftime", lambda fmt, *args: "2099-01-02"
                        if fmt == "%Y-%m-%d" else original(fmt, *args))
    call(budget, metered)
    first, second = records(app, task)
    assert previous_pool in {r["account_id"] for r in first["account_refs"]}
    assert "runtime:day:2099-01-02" in {r["account_id"] for r in second["account_refs"]}
    assert previous_pool not in {r["account_id"] for r in second["account_refs"]}
    assert budget.runtime_binding.store.balance(budget.runtime_binding.data["account_id"])["spent"]["model_calls"] == 2


def test_unknown_tool_cost_reaches_report_and_online_monitor(app):
    from evoagent.core.budget import CostReporter
    from evoagent.evolution.monitor import collect_metrics
    task, budget = bound(app)
    budget.mark_unknown_tool_cost("clock")
    task.task_status = TaskStatus.SUCCEEDED
    task.result_evidence = [{"budget": budget.snapshot()}]
    app.db.save_task(task)
    report = CostReporter(app.db).report()
    assert report["totals"]["unknown_tool_costs"] == 1
    assert report["totals"]["cost_complete"] is False
    metrics = collect_metrics(app.db, 0, time.time() + 10)
    assert metrics["unknown_cost_samples"] == 1


def test_unreconciled_legacy_fees_cannot_become_zero_day_baseline(app):
    legacy = Task(user_goal="legacy")
    app.db.save_task(legacy)
    app.db.save_execution_budget(legacy.task_id, "old-run", {"usage": {"model_calls": 1,
        "unknown_model_costs": 1, "tool_calls": 0, "cost": 0}})
    with pytest.raises(BudgetExceeded, match="historical unknown"):
        bound(app)


def _goal_worker(data_dir, goal_id, marker, gate, outcomes):
    from pathlib import Path
    app = App(data_dir=data_dir)
    class Counting(Provider):
        def _call(self):
            with Path(marker).open("a", encoding="utf-8") as out:
                out.write("called\n")
            super()._call()
    install(app, Counting(free=True))
    try:
        gate.wait(timeout=20)
        task = app.controller.submit_task("answer", goal_id=goal_id, intent="general", wait=True)
        outcomes.put(task.task_status.value)
    finally:
        app.db.close()


def test_real_processes_cannot_dispatch_twice_against_one_goal(app, tmp_path):
    g = goal(app, calls=1)
    ctx = multiprocessing.get_context("spawn")
    gate, outcomes = ctx.Barrier(2), ctx.Queue()
    marker = tmp_path / "provider-calls.txt"
    workers = [ctx.Process(target=_goal_worker,
        args=(str(tmp_path), g["goal_id"], str(marker), gate, outcomes)) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(timeout=30)
            assert worker.exitcode == 0
        assert [outcomes.get(timeout=3) for _ in workers] == ["FAILED", "FAILED"]
        assert marker.read_text(encoding="utf-8").splitlines() == ["called"]
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()
        outcomes.close()
