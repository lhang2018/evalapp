"""Review probes use the durable reservation protocol, not a permissive fake."""
import pytest

from evoagent.core.budget import BudgetLedger
from evoagent.core.persistence import Database
from evoagent.core.resource_admission import ResourceGovernor
from evoagent.goals.proactivity import ProactivityGovernor
from evoagent.goals.proactive_execution import ProactiveExecutionService


@pytest.fixture
def runtime(tmp_path):
    db = Database(str(tmp_path / "review.db"))
    store = BudgetLedger(db).reservation_store()
    store.create_account("proactive", {"model_calls": 10}, kind="pool",
                         reference="proactive", currency="USD", actor="host", reason="test")
    governor = ProactivityGovernor(resource_governor=ResourceGovernor(
        lambda: {"cpu": 0.1, "memory": 0.1, "model_pool": 0.1}))
    def service(submit):
        return ProactiveExecutionService(governor, reservations=store,
                                         account_id="proactive", submit=submit)
    yield store, service
    db.close()


def attempt(svc, **kwargs):
    return svc.attempt(**dict(actor="host", reason="test", opportunity_value=1,
        estimated_cost_known=True, amounts={"model_calls": 1},
        idempotency_key="opportunity-1", **kwargs))


def test_real_store_missing_submit_does_not_reserve_or_dispatch(runtime):
    store, service = runtime
    result = attempt(service(None))
    assert result["reason"] == "submit_entry_unavailable"
    assert store.balance("proactive")["held"] == {"model_calls": 0}


def test_created_task_then_exception_keeps_unknown_hold_and_blocks_retry(runtime):
    store, service = runtime
    tasks = []
    def submit(**kwargs):
        tasks.append("task-created")
        raise RuntimeError("receipt lost after creation")
    svc = service(submit)
    result = attempt(svc)
    assert result["submission_state"] == "unknown"
    assert result["zero_new_non_essential_tasks"] is False
    assert result["refunded"] is False
    assert store.get(result["reservation_id"])["state"] == "unknown"
    assert store.balance("proactive")["held"] == {"model_calls": 1}
    assert attempt(svc)["started"] is False
    assert tasks == ["task-created"]


@pytest.mark.parametrize("receipt", [None, {}, {"ok": False}, {"task_id": ""},
                                     {"task_id": 3}, {"ok": False, "task_id": "t"}])
def test_invalid_receipt_is_unknown_not_success_or_free(runtime, receipt):
    store, service = runtime
    result = attempt(service(lambda **kw: receipt))
    assert result["started"] is False
    assert result["submission_state"] == "unknown"
    assert store.get(result["reservation_id"])["state"] == "unknown"


def test_real_store_success_retains_usage_hold_and_cannot_double_dispatch(runtime):
    store, service = runtime
    calls = []
    svc = service(lambda **kw: calls.append(kw) or {"task_id": "task-1"})
    first = attempt(svc)
    assert first["started"] is True
    assert first["reconciliation_required"] is True
    state = store.get(first["reservation_id"])
    assert state["dispatched"] is True and state["state"] == "reserved"
    assert attempt(svc)["started"] is False
    assert len(calls) == 1
    assert store.balance("proactive")["held"] == {"model_calls": 1}


@pytest.mark.parametrize("changes", [
    {"opportunity_value": float("nan")}, {"opportunity_value": float("inf")},
    {"opportunity_value": True}, {"concurrent_proactive": 0.5},
    {"concurrent_proactive": False}, {"estimated_cost_known": "false"},
])
def test_malformed_admission_inputs_never_submit(runtime, changes):
    _, service = runtime
    calls = []
    svc = service(lambda **kw: calls.append(kw) or {"task_id": "t"})
    values = dict(actor="host", reason="test", opportunity_value=1,
                  estimated_cost_known=True, idempotency_key="k", amounts={"model_calls": 1})
    values.update(changes)
    with pytest.raises(ValueError):
        svc.attempt(**values)
    assert calls == []


def test_missing_reservations_and_sampler_cannot_silently_bypass_guard():
    calls = []
    svc = ProactiveExecutionService(ProactivityGovernor(),
        submit=lambda **kw: calls.append(kw) or {"task_id": "t"})
    result = attempt(svc)
    assert result["started"] is False
    assert calls == []


def test_valid_sampler_without_budget_is_also_refused(runtime):
    _, service = runtime
    svc = service(lambda **kw: pytest.fail("must not submit without a budget"))
    svc.reservations = None
    assert attempt(svc)["reason"] == "reservation_store_unavailable"


@pytest.mark.parametrize("after_dispatch", [False, True])
def test_dispatch_exception_refunds_only_when_not_claimed(runtime, after_dispatch):
    store, service = runtime
    original = store.mark_dispatched
    def dispatch(*args, **kwargs):
        if after_dispatch:
            original(*args, **kwargs)
        raise RuntimeError("dispatch acknowledgement lost")
    store.mark_dispatched = dispatch
    result = attempt(service(lambda **kw: pytest.fail("no submission on dispatch failure")))
    state = store.get(result["reservation_id"])
    assert result["refunded"] is (not after_dispatch)
    assert result["reconciliation_required"] is after_dispatch
    assert state["state"] == ("reserved" if after_dispatch else "refunded")
    assert state["dispatched"] is after_dispatch


def test_unknown_recording_failure_retains_hold_without_false_refund(runtime):
    store, service = runtime
    def unavailable(*args, **kwargs):
        raise RuntimeError("db write unavailable")
    store.mark_unknown = unavailable
    result = attempt(service(lambda **kw: None))
    assert result["submission_state"] == "unknown"
    assert "db write unavailable" in result["reconciliation_error"]
    assert result["refunded"] is False
    assert store.get(result["reservation_id"])["dispatched"] is True
    assert store.balance("proactive")["held"] == {"model_calls": 1}


def test_default_cost_is_unknown_and_missing_idempotency_is_not_actor_fallback(runtime):
    store, service = runtime
    svc = service(lambda **kw: pytest.fail("must not submit without explicit declarations"))
    assert svc.attempt(actor="host", reason="test", opportunity_value=1)["reason"] == "cost_unknown"
    with pytest.raises(ValueError):
        svc.attempt(actor="host", reason="test", opportunity_value=1,
                    estimated_cost_known=True, amounts={"model_calls": 1})
    assert store.balance("proactive")["held"] == {"model_calls": 0}


def test_empty_dispatch_receipt_does_not_authorize_submission(runtime):
    store, service = runtime
    store.mark_dispatched = lambda *args, **kwargs: None
    result = attempt(service(lambda **kw: pytest.fail("unconfirmed claim must not submit")))
    assert result["started"] is False
    assert result["refunded"] is True
    assert store.balance("proactive")["held"] == {"model_calls": 0}
