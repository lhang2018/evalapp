"""ALN-15-P: legacy Goal aggregate migration into the authoritative ledger."""
import json
import time

import pytest

from evoagent.core.budget import BudgetLedger
from evoagent.core.persistence import Database
from evoagent.core.runtime_budget import identity
from evoagent.goals.budget_migration import GoalBudgetMigration


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "goals.db"))
    yield d
    d.close()


@pytest.fixture
def ledger(db):
    return BudgetLedger(db)


def migration(db):
    return GoalBudgetMigration(BudgetLedger(db))


def seed_goal(db, goal_id, parent=None, policy=None, *, author="tester"):
    now = time.time()
    db._conn.execute(
        "INSERT INTO goals VALUES (?,?,?,?,?,?,?)",
        (goal_id, parent, author, "active", 1, now + 86400,
         json.dumps({"title": "g-" + goal_id}, ensure_ascii=False)))
    if policy:
        db._conn.execute(
            "INSERT INTO goal_grants VALUES (?,?,?,?)",
            ("gag-" + goal_id, goal_id,
             json.dumps({"goal_id": goal_id, "level": "autonomous",
                         "expires_at": now + 86400,
                         "budget_policy": policy}, ensure_ascii=False), None))
    db._conn.commit()


def seed_task(db, goal_id, usage, *, task_id=None):
    from evoagent.core.task import Task
    task_id = task_id or ("t-" + goal_id + "-" + str(time.time_ns()))
    task = Task(task_id=task_id, user_goal="demo", goal_id=goal_id)
    db.save_task(task)
    db.save_execution_budget(task_id, "run1", {"usage": usage})
    db._conn.commit()
    return task_id


POLICY_ROOT = {"max_cost": 10.0, "max_model_calls": 20, "max_tool_calls": 30}
POLICY_LEAF = {"max_cost": 5.0, "max_model_calls": 10, "max_tool_calls": 15}


def binding_rows(db):
    return [json.loads(r[0]) for r in db._conn.execute(
        "SELECT data FROM budget_runtime_bindings").fetchall()]


def test_known_usage_is_accounted_exactly(db):
    seed_goal(db, "groot", policy=POLICY_ROOT)
    seed_goal(db, "gleaf", parent="groot", policy=POLICY_LEAF)
    seed_task(db, "gleaf", {"model_calls": 4, "tool_calls": 2, "cost": 1.5})
    store = BudgetLedger(db).reservation_store()
    migration(db).migrate("groot")
    spent = store.balance(identity("goal", "gleaf"))["spent"]
    assert spent == {"cost_micros": 1_500_000, "model_calls": 4, "tool_calls": 2}
    # Parent chain mirrors the same amount through ancestry.
    assert store.balance(identity("goal", "groot"))["spent"] == spent


def test_migrate_is_idempotent_and_skips_zero_usage(db):
    seed_goal(db, "groot", policy=POLICY_ROOT)
    seed_task(db, "groot", {"model_calls": 2, "tool_calls": 0, "cost": 0.4})
    seed_task(db, "groot", {"model_calls": 0, "tool_calls": 0, "cost": 0.0})
    service = BudgetLedger(db).reservation_store()
    first = migration(db).migrate("groot")
    second = migration(db).migrate("groot")
    spent = service.balance(identity("goal", "groot"))["spent"]
    assert spent == {"cost_micros": 400_000, "model_calls": 2, "tool_calls": 0}
    # Replay is a no-op: every legacy task is already bound.
    assert second == {"goal_id": "groot", "migrated": [], "skipped": [],
                      "reconciliation_required": []}
    assert len(first["migrated"]) + len(first["skipped"]) == 2
    assert len(binding_rows(db)) == 2


def test_unknown_usage_is_held_not_zero(db):
    seed_goal(db, "groot", policy=POLICY_ROOT)
    seed_task(db, "groot", {"model_calls": 1, "tool_calls": 1, "cost": 0.0,
                            "unknown_model_costs": 1})
    service = BudgetLedger(db).reservation_store()
    receipt = migration(db).migrate("groot")
    assert receipt["migrated"][0]["state"] == "unknown"
    balance = service.balance(identity("goal", "groot"))
    assert balance["spent"] == {"cost_micros": 0, "model_calls": 0, "tool_calls": 0}
    assert balance["unknown_count"] == 1
    assert balance["held"]["model_calls"] == 1
    assert balance["held"]["tool_calls"] == 1
    # Cost holds the positive account upper bound (never assumed free).
    assert balance["held"]["cost_micros"] == POLICY_ROOT["max_cost"] * 1_000_000


def test_binding_marker_has_no_legacy_usage(db):
    seed_goal(db, "groot", policy=POLICY_ROOT)
    task_id = seed_task(db, "groot", {"model_calls": 1, "tool_calls": 0, "cost": 0.1})
    migration(db).migrate("groot")
    rows = binding_rows(db)
    assert len(rows) == 1
    marker = rows[0]
    assert marker["schema_version"] == 1
    assert marker["task_id"] == task_id
    assert marker["additional_accounts"] == []
    assert marker["goal_ids"] == ["groot"]
    assert marker["account_id"] == identity("goal", "groot")
    # Explicitly missing legacy usage: re-binding must NOT re-bill history.
    assert not marker.get("legacy_usage")


def test_rollback_refunds_only_undispatched_migrations(db):
    seed_goal(db, "groot", policy=POLICY_ROOT)
    seed_task(db, "groot", {"model_calls": 1, "tool_calls": 0, "cost": 0.1})
    service = BudgetLedger(db).reservation_store()
    mig = migration(db)
    mig.migrate("groot")
    # Crash-window artifact: a reserved, undispatched migration reservation.
    crashed = service.reserve(identity("goal", "groot"),
                              "goal-migration:v1|groot|t-crash",
                              {"cost_micros": 100_000, "model_calls": 1, "tool_calls": 0},
                              ttl_seconds=60, known_free=False,
                              actor="budget:goal-migration",
                              reason="crash window artifact")
    report = mig.rollback("groot")
    assert report["reverted"] == [crashed["reservation_id"]]
    # Settled history survives; accounts/binding markers are untouched.
    assert len(report["kept"]) >= 1
    assert len(binding_rows(db)) == 1
    assert service.balance(identity("goal", "groot"))["spent"]["cost_micros"] == 100_000


def test_verify_reports_consistency_and_unknown_work(db):
    seed_goal(db, "groot", policy=POLICY_ROOT)
    seed_task(db, "groot", {"model_calls": 3, "tool_calls": 1, "cost": 0.7})
    seed_task(db, "groot", {"model_calls": 1, "tool_calls": 0, "cost": 0.0,
                            "unknown_tool_costs": 1})
    mig = migration(db)
    mig.migrate("groot")
    report = mig.verify("groot")
    assert report["consistent"] is True
    assert report["expected_spent"] == {
        "cost_micros": 700_000, "model_calls": 3, "tool_calls": 1}
    assert len(report["unknown_tasks"]) == 1


def test_family_and_ancestors_are_covered(db):
    seed_goal(db, "groot", policy=POLICY_ROOT)
    seed_goal(db, "gleaf", parent="groot", policy=POLICY_LEAF)
    t1 = seed_task(db, "gleaf", {"model_calls": 1, "tool_calls": 0, "cost": 0.2})
    t2 = seed_task(db, "groot", {"model_calls": 1, "tool_calls": 0, "cost": 0.2})
    receipt = migration(db).migrate("groot")
    task_ids = {entry["task_id"] for entry in receipt["migrated"]}
    assert task_ids == {t1, t2}
    store = BudgetLedger(db).reservation_store()
    spent = store.balance(identity("goal", "groot"))["spent"]
    assert spent["model_calls"] == 2 and spent["cost_micros"] == 400_000
