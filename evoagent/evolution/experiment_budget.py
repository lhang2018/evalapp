"""Lifetime candidate budgets shared by interrupted evaluation attempts."""
from copy import deepcopy
import math
import time

from ..core.budget import BudgetController, BudgetLimits, BudgetUsage


def _ledger_usage(ledger, scope):
    # Day and month rows contain the same charges. Sum only exact-scope day
    # rows, across all dates, including legacy candidates without snapshots.
    prefix = scope + ":day:"
    db = getattr(ledger, "db", None)
    if db is not None:
        with db._lock:
            rows = db._conn.execute(
                "SELECT model_calls,tool_calls,cost,seconds FROM budget_ledger "
                "WHERE substr(ledger_key,1,?)=?", (len(prefix), prefix)).fetchall()
    else:
        with ledger._lock:
            rows = [dict(value) for key, value in ledger._mem.items() if key.startswith(prefix)]
    return {key: sum(row[key] for row in rows)
            for key in ("model_calls", "tool_calls", "cost", "seconds")}


def make_candidate_budget(controller, candidate):
    if controller.budget is None:
        return None
    cfg = controller.budget_config
    limits = (BudgetLimits(max_model_calls=cfg.candidate_max_model_calls,
                           max_tool_calls=cfg.candidate_max_tool_calls,
                           max_cost=cfg.candidate_max_cost,
                           max_seconds=cfg.candidate_max_seconds)
              if cfg is not None else BudgetLimits())
    state = candidate.experiment_budget or {}
    usage = deepcopy(state.get("usage") or {})
    ledger = _ledger_usage(controller.budget, "candidate:" + candidate.candidate_id)
    for key, value in ledger.items():
        usage[key] = max(usage.get(key, 0), value)
    if state.get("active"):
        # An unfinished attempt has an unknown crash time. Conservatively count
        # wall time since its last checkpoint rather than grant free runtime.
        usage["seconds"] = usage.get("seconds", 0) + max(0, time.time() - state["updated_at"])
    for key in ("model_calls", "tool_calls", "cost", "seconds", "unknown_model_costs"):
        value = usage.get(key, 0)
        if not math.isfinite(value) or value < 0:
            raise ValueError("候选预算历史用量无效: " + key)
    budget = BudgetController(limits, initial_usage=BudgetUsage.from_dict(usage),
        ledger=controller.budget, ledger_scope="candidate:" + candidate.candidate_id,
        extra_scopes=["evolution"])
    budget._violations = dict(state.get("violations") or {})

    def checkpoint(snapshot):
        candidate.experiment_budget = {**deepcopy(snapshot), "active": True, "updated_at": time.time()}
        controller._persist(candidate)

    budget.on_change = checkpoint
    budget.persist_usage()  # Save the restored starting point before execution.
    return budget


def finish_candidate_budget(controller, candidate, budget):
    candidate.experiment_budget = {**deepcopy(budget.snapshot()), "active": False,
                                   "updated_at": time.time()}
    controller._persist(candidate)
