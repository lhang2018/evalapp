"""ALN-15-P: provider/expert pools atomized over the durable reservation ledger."""
import threading

import pytest

from evoagent.core.budget import (BudgetController, BudgetExceeded,
                                  BudgetLedger, BudgetLimits)
from evoagent.core.persistence import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "pool.db"))
    yield d
    d.close()


def controller(db, *, calls=25, pool_calls=2, pool_cost=3.0, scope="provider:mock"):
    ledger = BudgetLedger(db)
    budget = BudgetController(BudgetLimits(max_model_calls=calls), ledger=ledger)
    budget.bind_pool(scope, max_model_calls=pool_calls, max_cost=pool_cost)
    return budget


def test_hold_then_settle_releases_capacity(db):
    a = controller(db)
    a.check_model_call()
    a.record_model_call(0.0, "m")
    a.settle_model_cost(0.2, "m")
    service = BudgetLedger(db).pool_reservations()
    balance = service.balance("provider:mock", "day")
    assert balance["spent"] == {"cost_micros": 200000, "model_calls": 1}
    assert balance["held"] == {"cost_micros": 0, "model_calls": 0}
    assert a._pool_holds == {}


def test_concurrent_holds_respect_capacity_across_controllers(db):
    second = controller(db)
    first = controller(db)
    first.check_model_call()
    second.check_model_call()  # cap=2: both holds legitimately atomic
    assert first._pool_holds and second._pool_holds
    with pytest.raises(BudgetExceeded):
        controller(db).check_model_call()


def test_settled_usage_frees_the_slot(db):
    a = controller(db)
    a.check_model_call()
    a.record_model_call(0.0, "m")
    a.settle_model_cost(0.1, "m")
    b = controller(db)
    b.check_model_call()
    b.record_model_call(0.0, "m")
    b.settle_model_cost(0.1, "m")
    with pytest.raises(BudgetExceeded):
        controller(db).check_model_call()


def test_unknown_cost_stays_held_and_is_not_assumed_free(db):
    u = controller(db, pool_calls=1)
    u.check_model_call()
    u.record_model_call(0.0, "m")
    u.mark_unknown_model_cost("m")
    service = BudgetLedger(db).pool_reservations()
    balance = service.balance("provider:mock", "day")
    assert balance["held"]["model_calls"] == 1
    assert balance["unknown_count"] == 1
    assert balance["spent"] == {"cost_micros": 0, "model_calls": 0}
    with pytest.raises(BudgetExceeded):
        controller(db, pool_calls=1).check_model_call()


def test_unknown_settlement_without_a_hold_is_a_noop(db):
    u = controller(db, pool_calls=5)
    u.check_model_call()
    u.record_model_call(0.0, "m")
    u.settle_model_cost(0.1, "m")
    service = BudgetLedger(db).pool_reservations()
    before = service.balance("provider:mock", "day")
    # Simulate a controller that lost its hold reference before resolving.
    orphan = controller(db, pool_calls=5)
    before_orphan = service.balance("provider:mock", "day")
    orphan.mark_unknown_model_cost("m")
    after_orphan = service.balance("provider:mock", "day")
    assert after_orphan["held"] == before_orphan["held"]


def expire(db, reservation_id):
    """Make a reservation's deadline pass (column + JSON must stay in sync)."""
    import json
    import time as _time
    with db._lock:
        raw = db._conn.execute(
            "SELECT data FROM budget_reservations WHERE reservation_id=?",
            (reservation_id,)).fetchone()[0]
        record = json.loads(raw)
        perished = _time.time() - 5
        record["expires_at"] = perished
        db._conn.execute(
            "UPDATE budget_reservations SET data=?, expires_at=? "
            "WHERE reservation_id=?",
            (json.dumps(record, sort_keys=True, ensure_ascii=False,
                        allow_nan=False), perished, reservation_id))
        db._conn.commit()


def test_expired_undispatched_hold_is_refunded_and_replaced(db):
    a = controller(db, pool_calls=10)
    a.check_model_call()
    first = a._pool_holds["provider:mock"]["reservation_id"]
    expire(db, first)
    a.check_model_call()  # expired, never dispatched -> refund, new hold
    replaced = a._pool_holds["provider:mock"]["reservation_id"]
    assert replaced != first
    assert a._pool_service().get(first)["state"] == "refunded"


def test_supersede_keeps_dispatched_hold_for_reconciliation(db):
    a = controller(db, pool_calls=10)
    a.check_model_call()
    a.record_model_call(0.0, "m")  # dispatched, unresolved
    first = a._pool_holds["provider:mock"]["reservation_id"]
    a.check_model_call()  # superseding does NOT refund dispatched work
    replaced = a._pool_holds["provider:mock"]["reservation_id"]
    assert replaced != first
    stale = a._pool_service().get(first)
    assert stale["state"] == "reserved" and stale["dispatched"] is True


def test_repeated_check_reuses_the_same_hold(db):
    """旧 check 语义是幂等检查：重复检查不得每次新建再退款。"""
    a = controller(db, pool_calls=10)
    a.check_model_call()
    first = a._pool_holds["provider:mock"]["reservation_id"]
    a.check_model_call()
    second = a._pool_holds["provider:mock"]["reservation_id"]
    assert first == second
    service = a._pool_service()
    assert service.get(first)["state"] == "reserved"
    a.record_model_call(0.0, "m")
    a.settle_model_cost(0.1, "m")
    assert service.get(first)["state"] == "settled"
    balance = service.balance("provider:mock", "day")
    assert balance["spent"]["model_calls"] == 1


def test_failed_pool_check_rolls_back_earlier_holds(db):
    ledger = BudgetLedger(db)
    budget = BudgetController(BudgetLimits(max_model_calls=25), ledger=ledger)
    budget.bind_pool("provider:a", max_model_calls=2, max_cost=3.0)
    # cap=0 keeps the legacy (non-atomic) check, which refuses immediately.
    budget.bind_pool("provider:b", max_model_calls=0, max_cost=1.0)
    with pytest.raises(BudgetExceeded):
        budget.check_model_call()
    assert budget._pool_holds == {}
    held = BudgetLedger(db).pool_reservations().balance(
        "provider:a", "day")["held"]["model_calls"]
    assert held == 0


def test_pool_without_positive_cost_bound_falls_back_to_legacy_check(db):
    ledger = BudgetLedger(db)
    budget = BudgetController(BudgetLimits(max_model_calls=1), ledger=ledger)
    budget.bind_pool("provider:free", max_model_calls=5, max_cost=None)
    budget.check_model_call()
    budget.record_model_call(0.0, "m")
    # Legacy scope check still enforces the controller call cap.
    with pytest.raises(BudgetExceeded):
        budget.check_model_call()


def test_memory_ledger_keeps_legacy_pool_semantics():
    ledger = BudgetLedger()
    budget = BudgetController(BudgetLimits(max_model_calls=1), ledger=ledger)
    budget.bind_pool("provider:mock", max_model_calls=1, max_cost=1.0)
    budget.check_model_call()
    budget.record_model_call(0.0, "m")
    with pytest.raises(BudgetExceeded):
        budget.check_model_call()


def test_legacy_scope_usage_still_recorded(db):
    a = controller(db)
    b = controller(db)
    a.check_model_call(); b.check_model_call()
    a.record_model_call(0.0, "m"); a.settle_model_cost(0.2, "m")
    b.record_model_call(0.0, "m"); b.settle_model_cost(0.1, "m")
    usage = BudgetLedger(db).usage("provider:mock", "day")
    assert usage["model_calls"] == 2
    assert abs(usage["cost"] - 0.3) < 1e-9


def test_threads_race_for_the_last_pool_slot(db):
    cap_holder = controller(db)
    cap_holder.check_model_call()
    ledger = BudgetLedger(db)
    acquired, blocked = [], []
    barrier = threading.Barrier(3)

    def attempt(i):
        barrier.wait()
        budget = BudgetController(
            BudgetLimits(max_model_calls=25), ledger=BudgetLedger(db))
        budget.bind_pool("provider:mock", max_model_calls=2, max_cost=3.0)
        try:
            budget.check_model_call()
            acquired.append(budget)
        except BudgetExceeded:
            blocked.append(i)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Exactly one of the racers grabs the remaining slot; the rest are refused.
    assert len(acquired) == 1, (len(acquired), len(blocked))
    assert len(blocked) >= 1
    service = BudgetLedger(db).pool_reservations()
    assert service.balance("provider:mock", "day")["held"]["model_calls"] == 2
    assert service.balance("provider:mock", "day")["spent"]["model_calls"] == 0


def test_prepare_is_idempotent_for_same_limits(db):
    service = BudgetLedger(db).pool_reservations()
    one = service.prepare("provider:mock", max_model_calls=3, max_cost=5.0)
    second = service.prepare("provider:mock", max_model_calls=3, max_cost=5.0)
    assert one["account_id"] == second["account_id"]
    with pytest.raises(ValueError):
        service.prepare("provider:mock", max_model_calls=4, max_cost=5.0)


def test_pool_reservations_reject_non_pool_scopes(db):
    service = BudgetLedger(db).pool_reservations()
    with pytest.raises(ValueError):
        service.prepare("runtime", max_model_calls=3, max_cost=5.0)
