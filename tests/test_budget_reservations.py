"""ALN-15: durable allocation, unknown charges and multi-process races."""
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import threading
import time

import pytest

from evoagent.core.budget import BudgetExceeded, BudgetLedger
from evoagent.core.persistence import Database


AUDIT = {"actor": "runtime:test", "reason": "bounded task allocation"}


@pytest.fixture
def store(tmp_path):
    db = Database(str(tmp_path / "budget.db"))
    clock = [1700000000.0]
    service = BudgetLedger(db).reservation_store(clock=lambda: clock[0])
    service.test_clock = clock
    yield service
    db.close()


def account(store, name="goal:root", limits=None, parent=None, kind="goal"):
    return store.create_account(name, limits or {"cost_micros": 100, "model_calls": 10},
        kind=kind, reference=name, parent_id=parent, currency="USD", **AUDIT)


def reserve(store, key="call:1", name="goal:root", cost=60, calls=1, ttl=60):
    return store.reserve(name, key, {"cost_micros": cost, "model_calls": calls},
                         ttl_seconds=ttl, **AUDIT)


def dispatch(store, reservation):
    return store.mark_dispatched(reservation["reservation_id"], **AUDIT)


def settle(store, reservation, cost=40, calls=1, evidence="invoice:1"):
    return store.settle(reservation["reservation_id"],
        {"cost_micros": cost, "model_calls": calls}, evidence_ref=evidence, **AUDIT)


def test_capacity_is_held_before_any_usage_is_recorded(store):
    account(store)
    pending = reserve(store)
    assert pending["state"] == "reserved"
    assert store.balance("goal:root")["held"]["cost_micros"] == 60
    with pytest.raises(BudgetExceeded):
        reserve(store, "call:2")
    assert store.balance("goal:root")["spent"]["cost_micros"] == 0


def test_threads_on_one_database_allow_only_one_reservation(store):
    account(store)
    barrier = threading.Barrier(8)
    def attempt(i):
        barrier.wait()
        try:
            return reserve(store, f"call:{i}")["reservation_id"]
        except BudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(x is not None for x in results) == 1


def _process_reserve(path, barrier, results, key):
    db = Database(path)
    try:
        service = BudgetLedger(db).reservation_store()
        barrier.wait(timeout=20)
        try:
            reserve(service, key)
            results.put("reserved")
        except BudgetExceeded:
            results.put("denied")
    finally:
        db.close()


def test_processes_share_the_same_capacity(store):
    account(store)
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_process_reserve,
                 args=(store.db.path, barrier, results, f"process:{i}")) for i in range(2)]
    for proc in processes:
        proc.start()
    try:
        for proc in processes:
            proc.join(timeout=30)
            assert proc.exitcode == 0
        assert sorted(results.get(timeout=5) for _ in processes) == ["denied", "reserved"]
    finally:
        for proc in processes:
            if proc.is_alive():
                proc.terminate()
                proc.join()
        results.close()


def test_replay_cannot_replace_amounts_or_resurrect_closed_reservation(store):
    account(store)
    pending = reserve(store)
    assert reserve(store)["reservation_id"] == pending["reservation_id"]
    with pytest.raises(ValueError):
        reserve(store, cost=61)
    store.refund(pending["reservation_id"], **AUDIT)
    assert reserve(store)["state"] == "refunded"
    assert store.balance("goal:root")["held"]["cost_micros"] == 0


def test_sibling_accounts_cannot_spend_parent_twice(store):
    account(store)
    account(store, "task:a", parent="goal:root", kind="task")
    account(store, "experiment:b", parent="goal:root", kind="experiment")
    reserve(store, name="task:a")
    with pytest.raises(BudgetExceeded):
        reserve(store, name="experiment:b")
    assert store.balance("goal:root")["held"]["cost_micros"] == 60


def test_child_limits_currency_and_identity_are_immutable(store):
    original = account(store)
    assert account(store) == original
    with pytest.raises(ValueError):
        account(store, limits={"cost_micros": 101, "model_calls": 10})
    with pytest.raises(ValueError):
        account(store, "child", limits={"cost_micros": 101, "model_calls": 10}, parent="goal:root")
    with pytest.raises(ValueError):
        store.create_account("child", {"cost_micros": 50, "model_calls": 1},
            kind="task", reference="t", parent_id="goal:root", currency="CNY", **AUDIT)
    with pytest.raises(ValueError):
        store.create_account("child", {"model_calls": 1}, kind="task", reference="t",
                             parent_id="goal:root", currency="USD", **AUDIT)


def test_settlement_releases_unused_amount_and_records_legacy_usage_once(store):
    account(store)
    pending = dispatch(store, reserve(store))
    paid = settle(store, pending)
    assert paid["state"] == "settled"
    assert settle(store, pending) == paid
    balance = store.balance("goal:root")
    assert balance["held"] == {"cost_micros": 0, "model_calls": 0}
    assert balance["spent"] == {"cost_micros": 40, "model_calls": 1}
    assert balance["available"]["cost_micros"] == 60
    usage = store.ledger.usage("reservation:USD:goal:root", at=pending["created_at"])
    assert usage["model_calls"] == 1
    assert usage["cost"] == pytest.approx(40 / 1_000_000)
    with pytest.raises(ValueError):
        settle(store, pending, cost=41)


def test_actual_overrun_is_recorded_honestly_and_stops_new_spending(store):
    account(store)
    pending = dispatch(store, reserve(store))
    settle(store, pending, cost=120)
    balance = store.balance("goal:root")
    assert balance["available"]["cost_micros"] == -20
    assert balance["spent"]["cost_micros"] == 120
    with pytest.raises(BudgetExceeded):
        reserve(store, "new", cost=1)


def test_dispatch_is_claimed_once_and_prevents_unproven_refund(store):
    account(store)
    pending = reserve(store)
    with pytest.raises(ValueError):
        settle(store, pending)
    dispatch(store, pending)
    with pytest.raises(ValueError):
        dispatch(store, pending)
    with pytest.raises(ValueError):
        store.refund(pending["reservation_id"], **AUDIT)


def test_unknown_keeps_capacity_and_blocks_parent_until_reconciled(store):
    account(store)
    account(store, "task:a", parent="goal:root", kind="task")
    pending = dispatch(store, reserve(store, name="task:a"))
    store.mark_unknown(pending["reservation_id"], evidence_ref="timeout:1", **AUDIT)
    balance = store.balance("goal:root")
    assert balance["unknown_count"] == 1
    assert balance["held"]["cost_micros"] == 60
    with pytest.raises(BudgetExceeded):
        reserve(store, "new", cost=1)
    with pytest.raises(ValueError):
        store.refund(pending["reservation_id"], **AUDIT)
    with pytest.raises(ValueError):
        settle(store, pending)
    reconciled = store.reconcile(pending["reservation_id"],
        {"cost_micros": 20, "model_calls": 1}, evidence_ref="bill:resolved", **AUDIT)
    assert reconciled["state"] == "settled"
    assert store.balance("goal:root")["unknown_count"] == 0
    assert reserve(store, "new", cost=80)["state"] == "reserved"


def test_expiry_recovery_refunds_only_undispatched_and_never_replays(store):
    account(store)
    unused = reserve(store, "unused", cost=30)
    sent = dispatch(store, reserve(store, "sent", cost=30))
    store.test_clock[0] += 61
    recovered = store.recover_expired(**AUDIT)
    assert {r["state"] for r in recovered} == {"refunded", "unknown"}
    assert store.get(unused["reservation_id"])["state"] == "refunded"
    assert store.get(sent["reservation_id"])["state"] == "unknown"
    assert store.recover_expired(**AUDIT) == []
    with pytest.raises(ValueError):
        dispatch(store, unused)


def test_expired_unclaimed_dispatch_is_rejected(store):
    account(store)
    pending = reserve(store)
    store.test_clock[0] += 60
    with pytest.raises(ValueError):
        dispatch(store, pending)


def test_reconcile_zero_requires_complete_actuals_and_evidence(store):
    account(store)
    pending = dispatch(store, reserve(store))
    store.mark_unknown(pending["reservation_id"], evidence_ref="timeout", **AUDIT)
    for actual, evidence in [({"model_calls": 0}, "bill"),
                             ({"cost_micros": 0, "model_calls": 0}, "")]:
        with pytest.raises(ValueError):
            store.reconcile(pending["reservation_id"], actual, evidence_ref=evidence, **AUDIT)
    result = store.reconcile(pending["reservation_id"],
        {"cost_micros": 0, "model_calls": 0}, evidence_ref="provider:no-charge", **AUDIT)
    assert result["state"] == "refunded"
    assert store.balance("goal:root")["available"]["cost_micros"] == 100


def test_restart_preserves_holds_references_and_audit(store):
    account(store)
    account(store, "task:a", parent="goal:root", kind="task")
    pending = dispatch(store, reserve(store, name="task:a"))
    db = Database(store.db.path)
    try:
        restarted = BudgetLedger(db).reservation_store(clock=lambda: store.test_clock[0])
        assert restarted.get(pending["reservation_id"])["dispatched"]
        assert restarted.balance("goal:root")["held"]["cost_micros"] == 60
        store.test_clock[0] += 61
        restarted.recover_expired(**AUDIT)
        history = restarted.history(pending["reservation_id"])
        assert [e["event"] for e in history] == ["reserved", "dispatched", "unknown"]
        assert all(e["actor"] and e["reason"] for e in history)
        assert restarted.get_account("task:a")["parent_id"] == "goal:root"
        assert restarted.get_account("task:a")["reference"] == "task:a"
    finally:
        db.close()


def test_settlement_failure_rolls_back_state_audit_and_legacy_charge(store):
    account(store)
    pending = dispatch(store, reserve(store))
    store.db._conn.execute("CREATE TRIGGER reject_budget BEFORE INSERT ON budget_ledger "
                           "BEGIN SELECT RAISE(ABORT, 'injected disk failure'); END")
    store.db._conn.commit()
    with pytest.raises(Exception, match="injected disk failure"):
        settle(store, pending)
    assert store.get(pending["reservation_id"])["state"] == "reserved"
    assert len(store.history(pending["reservation_id"])) == 2
    store.db._conn.execute("DROP TRIGGER reject_budget")
    store.db._conn.commit()
    settle(store, pending)
    assert store.balance("goal:root")["spent"]["cost_micros"] == 40


@pytest.mark.parametrize("bad", [True, -1, 1.2, float("nan"), float("inf"), "3", 2**63])
def test_invalid_units_cannot_allocate_or_mutate_accounts(store, bad):
    with pytest.raises(ValueError):
        account(store, limits={"cost_micros": bad, "model_calls": 2})
    account(store)
    with pytest.raises(ValueError):
        reserve(store, cost=bad)
    assert store.balance("goal:root")["held"]["cost_micros"] == 0


@pytest.mark.parametrize("ttl", [0, -1, True, float("nan"), float("inf")])
def test_invalid_lease_is_rejected(store, ttl):
    account(store)
    with pytest.raises(ValueError):
        reserve(store, ttl=ttl)


def test_unknown_dimensions_empty_allocations_and_missing_audit_are_rejected(store):
    account(store)
    for amounts in [{"cpu": 1}, {}, {"cost_micros": 0, "model_calls": 0},
                    {"cost_micros": 0, "model_calls": 1}]:
        with pytest.raises(ValueError):
            store.reserve("goal:root", "bad", amounts, ttl_seconds=60, **AUDIT)
    with pytest.raises(ValueError):
        store.reserve("goal:root", "bad", {"cost_micros": 1, "model_calls": 1},
                      ttl_seconds=60, actor="", reason="why")


def test_persistent_database_is_required():
    with pytest.raises(ValueError):
        BudgetLedger().reservation_store()


def test_parent_settlement_does_not_double_count_child_or_reset_old_pools(store):
    store.ledger.record("runtime", model_calls=2, cost=0.5)
    account(store)
    account(store, "task:a", parent="goal:root", kind="task")
    pending = dispatch(store, reserve(store, name="task:a"))
    settle(store, pending)
    assert store.balance("goal:root")["spent"]["cost_micros"] == 40
    assert store.balance("task:a")["spent"]["cost_micros"] == 40
    assert store.ledger.usage("runtime")["cost"] == 0.5


def test_lost_runtime_lease_cannot_write_reservations(store):
    from evoagent.core.leases import CURRENT_LEASE, LeaseLost, LeaseToken
    account(store)
    token = CURRENT_LEASE.set(LeaseToken(store.db, "missing-task", "run", "old-worker", 1))
    try:
        with pytest.raises(LeaseLost):
            reserve(store)
    finally:
        CURRENT_LEASE.reset(token)
    assert store.balance("goal:root")["held"]["cost_micros"] == 0


def test_unknown_or_overrun_blocks_dispatch_of_an_earlier_hold(store):
    account(store)
    first = dispatch(store, reserve(store, "first", cost=30))
    second = reserve(store, "second", cost=30)
    store.mark_unknown(first["reservation_id"], evidence_ref="timeout", **AUDIT)
    with pytest.raises(BudgetExceeded):
        dispatch(store, second)
    store.reconcile(first["reservation_id"], {"cost_micros": 120, "model_calls": 1},
                    evidence_ref="overrun:bill", **AUDIT)
    with pytest.raises(BudgetExceeded):
        dispatch(store, second)


def test_expired_dispatched_hold_blocks_without_waiting_for_recovery_job(store):
    account(store)
    dispatch(store, reserve(store, cost=10))
    store.test_clock[0] += 61
    assert store.balance("goal:root")["reconciliation_required"]
    with pytest.raises(BudgetExceeded):
        reserve(store, "new", cost=1)


def test_concurrent_settlement_posts_one_charge(store):
    account(store)
    pending = dispatch(store, reserve(store))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: settle(store, pending), range(8)))
    assert all(r == results[0] for r in results)
    assert store.ledger.usage("reservation:USD:goal:root", at=pending["created_at"])["model_calls"] == 1
    assert [e["event"] for e in store.history(pending["reservation_id"])] == ["reserved", "dispatched", "settle"]


def _crash_after_dispatch(path):
    import os
    db = Database(path)
    service = BudgetLedger(db).reservation_store()
    dispatch(service, reserve(service, "crash", cost=10, ttl=1))
    os._exit(23)  # No close/finally: prove the pre-call durable marker survived.


def test_killed_process_cannot_make_dispatched_charge_look_unused(store):
    account(store)
    ctx = multiprocessing.get_context("spawn")
    worker = ctx.Process(target=_crash_after_dispatch, args=(store.db.path,))
    worker.start()
    worker.join(timeout=30)
    try:
        assert worker.exitcode == 23
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join()
    store.test_clock[0] = time.time() + 2
    recovered = store.recover_expired(**AUDIT)
    assert len(recovered) == 1 and recovered[0]["state"] == "unknown"
    assert store.balance("goal:root")["held"]["cost_micros"] == 10


def test_nested_transaction_is_not_committed_or_rolled_back(store):
    account(store)
    store.db._conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError, match="nest"):
            reserve(store)
        assert store.db._conn.in_transaction
    finally:
        store.db._conn.rollback()


def test_goal_task_experiment_refs_share_capacity_across_three_levels(store):
    account(store)
    account(store, "task:a", parent="goal:root", kind="task")
    account(store, "experiment:b", parent="task:a", kind="experiment")
    pending = reserve(store, name="experiment:b")
    assert [a["kind"] for a in pending["account_refs"]] == ["experiment", "task", "goal"]
    assert store.balance("goal:root")["held"]["cost_micros"] == 60
    assert store.balance("task:a")["held"]["cost_micros"] == 60


@pytest.mark.parametrize("dimensions", [{"tool_calls": 3}, {"tokens": 100}])
def test_nonmonetary_capacity_is_accounted_without_fabricating_money(store, dimensions):
    root = store.create_account("pool", dimensions, kind="pool", reference="bounded-work",
                                currency="CNY", **AUDIT)
    r = store.reserve(root["account_id"], "r", dimensions, ttl_seconds=10, **AUDIT)
    dispatch(store, r)
    paid = store.settle(r["reservation_id"], dimensions, evidence_ref="usage:1", **AUDIT)
    assert paid["currency"] == "CNY"
    assert store.balance("pool")["spent"] == dimensions
    assert store.ledger.usage("reservation:CNY:pool", at=r["created_at"])["cost"] == 0
