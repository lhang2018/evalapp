"""Server-owned allocation bindings and actual call boundaries (ALN-15-R)."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
import time
import uuid

from .budget import BudgetExceeded
from .leases import assert_current_lease
from .reservations import MAX_UNITS

EXPERIMENT_BUDGET = ContextVar("aggregate_experiment_budget", default=None)
CLASSIFICATION_BUDGET = ContextVar("classification_budget", default=None)
AUDIT = {"actor": "runtime:budget", "reason": "authorized execution allocation"}


def micros(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise BudgetExceeded("pricing", "cost must be a finite nonnegative host policy amount")
    amount = int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    if amount > MAX_UNITS:
        raise BudgetExceeded("pricing", "cost exceeds supported integer range")
    return amount


def limits(policy):
    result = {"cost_micros": micros(policy["max_cost"]),
              "model_calls": policy["max_model_calls"], "tool_calls": policy["max_tool_calls"]}
    if any(type(v) is not int or not 0 <= v <= MAX_UNITS for v in result.values()):
        raise BudgetExceeded("limits", "invalid allocation limits")
    return result


def identity(kind, ref):
    # Separate identities; never concatenate caller-controlled IDs into a path.
    return kind + ":" + hashlib.sha256(ref.encode()).hexdigest()


@contextmanager
def experiment_scope(budget):
    token = EXPERIMENT_BUDGET.set(budget)
    try:
        yield
    finally:
        EXPERIMENT_BUDGET.reset(token)


class RuntimeBudgets:
    def __init__(self, ledger, config, *, bus=None):
        self.ledger, self.db, self.config = ledger, ledger.db, config
        self.store = ledger.reservation_store()
        self.bus = bus

    def sweep(self):
        recovered = self.store.recover_expired(**AUDIT)
        if recovered and self.bus is not None:
            self.bus.emit(event_type="budget_reservations_recovered", component_id="runtime_budget",
                severity="warn" if any(r["state"] == "unknown" for r in recovered) else "info",
                payload={"reservations": [{"reservation_id": r["reservation_id"],
                    "state": r["state"]} for r in recovered]})
        return recovered

    def _ensure(self, name, cap, kind, ref, parent=None):
        try:
            account = self.store.get_account(name)
        except KeyError:
            try:
                return self.store.create_account(name, cap, kind=kind, reference=ref,
                    currency="USD", parent_id=parent, **AUDIT)
            except ValueError:
                # Another connection may have provisioned a narrower account.
                account = self.store.get_account(name)
        if (account["kind"] != kind or account["reference"] != ref or
                account["parent_id"] != parent or account["currency"] != "USD"):
            raise BudgetExceeded("binding", "budget identity was rebound")
        return account

    def _goals(self, task):
        from ..goals import GoalStore
        goals = GoalStore(self.db)
        if not task.goal_id:
            if task.origin != "user":
                raise PermissionError("autonomous task lacks a registered Goal")
            return []
        goals.check_task(task.task_id)
        with self.db._lock:
            binding = self.db._conn.execute("SELECT action FROM goal_tasks WHERE task_id=?",
                                           (task.task_id,)).fetchone()
            lineage, grants = goals._grants(task.goal_id, binding["action"])
            return list(reversed(list(zip(lineage, grants))))

    def _pool(self):
        day = time.strftime("%Y-%m-%d")
        name = "runtime:day:" + day
        with self.store._write():
            existing = self.db._conn.execute("SELECT data FROM budget_runtime_pool_baselines WHERE account_id=?",
                                             (name,)).fetchone()
            if existing:
                return name
            legacy = self.db._conn.execute("SELECT e.state FROM execution_budget_state e "
                "LEFT JOIN budget_runtime_bindings b ON b.task_id=e.task_id WHERE b.task_id IS NULL").fetchall()
            if any((json.loads(r[0]).get("usage", {}).get("unknown_model_costs") or
                    json.loads(r[0]).get("usage", {}).get("unknown_tool_costs")) for r in legacy):
                raise BudgetExceeded("legacy_reconciliation_required",
                                     "unmigrated historical unknown charges require reconciliation")
            # Snapshot and create under the same SQLite writer lock. Existing
            # legacy day usage consumes capacity; an upgrade is not a new grant.
            baseline = self.ledger.usage("runtime")
            old = dict(cost_micros=micros(baseline["cost"]),
                       model_calls=baseline["model_calls"], tool_calls=baseline["tool_calls"])
            configured = dict(cost_micros=micros(self.config.runtime_daily_cost),
                              model_calls=self.config.runtime_daily_model_calls, tool_calls=MAX_UNITS)
            cap = {k: max(0, configured[k] - old[k]) for k in configured}
            from .reservations import _units
            _units(cap)
            self.db._conn.execute("INSERT INTO budget_accounts VALUES (?,?,?,?,?,?,?,?,?)",
                (name, None, "pool", name, "USD", json.dumps(cap), time.time(),
                 AUDIT["actor"], "host day allocation after legacy baseline"))
            self.db._conn.execute("INSERT INTO budget_runtime_pool_baselines VALUES (?,?)",
                                 (name, json.dumps(old)))
        return name

    def _legacy_goal_history(self, goal_id):
        with self.db._lock:
            rows = self.db._conn.execute("WITH RECURSIVE family(goal_id) AS ("
                "SELECT ? UNION SELECT g.goal_id FROM goals g JOIN family f ON g.parent_goal_id=f.goal_id) "
                "SELECT e.state FROM tasks t JOIN execution_budget_state e ON e.task_id=t.task_id "
                "LEFT JOIN budget_runtime_bindings b ON b.task_id=t.task_id "
                "WHERE t.goal_id IN (SELECT goal_id FROM family) AND b.task_id IS NULL", (goal_id,)).fetchall()
        for row in rows:
            usage = json.loads(row[0]).get("usage", {})
            if any(usage.get(k) for k in ("model_calls", "tool_calls", "cost", "unknown_model_costs", "unknown_tool_costs")):
                raise BudgetExceeded("legacy_goal_budget", "existing Goal spending requires explicit aggregate migration")

    def bind(self, task, budget):
        self.sweep()
        # Read task identity from the authoritative row, never manifest_extras.
        stored = self.db.get_task(task.task_id)
        if stored is None or stored.goal_id != task.goal_id:
            raise PermissionError("task identity is not registered")
        parents = self._goals(stored)
        with self.db._lock:
            old = self.db._conn.execute("SELECT data FROM budget_runtime_bindings WHERE task_id=?",
                                        (task.task_id,)).fetchone()
        if old:
            data = json.loads(old[0])
            result = RuntimeBinding(self, stored.task_id, data)
            if data.get("legacy_usage"):
                from .budget import BudgetUsage
                result.import_legacy(BudgetUsage.from_dict(data["legacy_usage"]))
            return result
        parent, caps = None, {}
        if parents:
            self._legacy_goal_history(parents[0][0]["goal_id"])
        for goal, grant in parents:
            cap = limits(grant["budget_policy"])
            if parent:
                outer = self.store.get_account(parent)["limits"]
                cap = {k: min(cap[k], outer[k]) for k in cap}
            name = identity("goal", goal["goal_id"])
            account = self._ensure(name, cap, "goal", goal["goal_id"], parent)
            caps[name] = {k: min(cap[k], account["limits"][k]) for k in cap}
            parent = name
        cfg = self.config
        pool_id = self._pool()
        additional = [pool_id]
        experiment = EXPERIMENT_BUDGET.get()
        if experiment is not None:
            ref = getattr(experiment, "_aggregate_experiment_ref", None)
            if ref is None:
                scope = getattr(experiment, "ledger_scope", "runtime")
                ref = scope if scope.startswith("candidate:") else "experiment:" + uuid.uuid4().hex
                experiment._aggregate_experiment_ref = ref
            exp_id = identity("experiment", ref)
            cap = limits(asdict(experiment.limits))
            used = experiment.usage
            if used.unknown_model_costs or used.unknown_tool_costs:
                raise BudgetExceeded("reconciliation_required", "experiment has unresolved historical charges")
            cap = {"cost_micros": max(0, cap["cost_micros"] - micros(used.cost)),
                   "model_calls": max(0, cap["model_calls"] - used.model_calls),
                   "tool_calls": max(0, cap["tool_calls"] - used.tool_calls)}
            self._ensure(exp_id, cap, "experiment", ref)
            additional.append(exp_id)
        task_cap = limits(asdict(budget.limits))
        # The registered generous expert can expand these two task limits later
        # in classification. Provision capacity, then narrow EVERY call using
        # the actual controller limits; provisioning is never permission to spend.
        for key in ("cost_micros", "model_calls"):
            task_cap[key] = min(MAX_UNITS, task_cap[key] * 2)
        if parent:
            outer = self.store.get_account(parent)["limits"]
            task_cap = {k: min(task_cap[k], outer[k]) for k in task_cap}
        task_id = identity("task", stored.task_id)
        self._ensure(task_id, task_cap, "task", stored.task_id, parent)
        data = dict(schema_version=1, account_id=task_id, additional_accounts=additional,
                    goal_ids=[goal["goal_id"] for goal, _ in parents], task_id=stored.task_id,
                    legacy_usage=(asdict(budget.usage) if any(getattr(budget.usage, k)
                        for k in ("model_calls", "tool_calls", "cost", "unknown_model_costs", "unknown_tool_costs")) else None))
        with self.store._write():
            row = self.db._conn.execute("SELECT data FROM budget_runtime_bindings WHERE task_id=?",
                                       (task.task_id,)).fetchone()
            if row:
                data = json.loads(row[0])
            else:
                self.db._conn.execute("INSERT INTO budget_runtime_bindings VALUES (?,?)",
                                     (task.task_id, json.dumps(data, sort_keys=True)))
        result = RuntimeBinding(self, stored.task_id, data)
        # Do not silently give an upgraded legacy task a fresh budget.
        if data.get("legacy_usage"):
            from .budget import BudgetUsage
            result.import_legacy(BudgetUsage.from_dict(data["legacy_usage"]))
        return result


class RuntimeBinding:
    def __init__(self, service, task_id, data):
        self.service, self.task_id, self.data = service, task_id, data
        self.store, self.db = service.store, service.db

    def authority(self):
        assert_current_lease()
        task = self.db.get_task(self.task_id)
        if task is None or task.is_terminal():
            raise PermissionError("task has ended; allocation is not execution permission")
        self.service._goals(task)

    def ceilings(self, budget, additional_accounts=None):
        while getattr(budget, "parent", None) is not None:
            budget = budget.parent
        task = self.db.get_task(self.task_id)
        if task is None:
            raise PermissionError("task no longer exists")
        caps = {self.data["account_id"]: limits(asdict(budget.limits))}
        for name in (self.data["additional_accounts"] if additional_accounts is None else additional_accounts):
            if name.startswith("runtime:day:"):
                with self.db._lock:
                    row = self.db._conn.execute("SELECT data FROM budget_runtime_pool_baselines WHERE account_id=?",
                                               (name,)).fetchone()
                if row is None:
                    raise BudgetExceeded("binding", "missing durable day-pool baseline")
                old = json.loads(row[0])
                cfg = self.service.config
                caps[name] = dict(cost_micros=max(0, micros(cfg.runtime_daily_cost) - old["cost_micros"]),
                    model_calls=max(0, cfg.runtime_daily_model_calls - old["model_calls"]),
                    tool_calls=MAX_UNITS - old["tool_calls"])
        for goal, grant in self.service._goals(task):
            caps[identity("goal", goal["goal_id"])] = limits(grant["budget_policy"])
        return caps

    def begin(self, budget, kind, component, hold, *, phase, effect_id="", known_free=False):
        self.authority()
        self.service.sweep()
        self.recover_previous_runs()
        # A task can cross midnight. Allocate each new action to its current
        # day; an already reserved action keeps its original period and receipt.
        additional = [name for name in self.data["additional_accounts"]
                      if not name.startswith("runtime:day:")]
        additional.append(self.service._pool())
        amount = {"cost_micros": hold, "model_calls": int(kind == "model"),
                  "tool_calls": int(kind == "tool")}
        task = self.db.get_task(self.task_id)
        context = dict(task_id=self.task_id, run_id=task.current_run_id,
                       kind=kind, component=component, phase=phase)
        if effect_id:
            context["effect_id"] = effect_id
        reservation = self.store.reserve(self.data["account_id"], "call:" + uuid.uuid4().hex,
            amount, ttl_seconds=max(1, budget.limits.max_seconds), known_free=known_free,
            additional_accounts=additional, ceilings=self.ceilings(budget, additional),
            context=context, **AUDIT)
        return RuntimeCall(self, budget, reservation)

    def recover_previous_runs(self):
        task = self.db.get_task(self.task_id)
        with self.db._lock:
            rows = self.db._conn.execute("SELECT data FROM budget_reservations "
                "WHERE account_id=? AND state='reserved'", (self.data["account_id"],)).fetchall()
        for row in rows:
            record = json.loads(row[0])
            previous = record.get("context", {}).get("run_id")
            if previous and previous != task.current_run_id:
                if record["dispatched"]:
                    if record.get("known_free"):
                        self.store.settle(record["reservation_id"], record["amounts"],
                            evidence_ref="recovery:declared-free:" + previous, **AUDIT)
                    else:
                        self.store.mark_unknown(record["reservation_id"],
                            evidence_ref="recovery:previous-run:" + previous, **AUDIT)
                else:
                    self.store.refund(record["reservation_id"], **AUDIT)

    def import_legacy(self, usage):
        # A durable historic snapshot is a migration receipt, never a fresh grant.
        amount = {"cost_micros": micros(usage.cost), "model_calls": usage.model_calls,
                  "tool_calls": usage.tool_calls}
        if not any(amount.values()):
            return
        hold = dict(amount)
        unknown = bool(usage.unknown_model_costs or usage.unknown_tool_costs)
        if unknown and not hold["cost_micros"]:
            hold["cost_micros"] = self.store.get_account(self.data["account_id"])["limits"]["cost_micros"]
        reservation = self.store.reserve(self.data["account_id"], "migration:v1", hold,
            ttl_seconds=1, known_free=not bool(hold["cost_micros"]),
            **AUDIT)
        if reservation["state"] == "refunded":
            raise BudgetExceeded("migration", "historical use cannot be refunded as unused")
        if reservation["state"] in {"settled", "unknown"}:
            return
        if not reservation["dispatched"]:
            self.store.mark_dispatched(reservation["reservation_id"], **AUDIT)
        if unknown:
            self.store.mark_unknown(reservation["reservation_id"], evidence_ref="legacy:unknown-cost", **AUDIT)
        else:
            self.store.settle(reservation["reservation_id"], amount,
                              evidence_ref="execution_budget:" + self.task_id, **AUDIT)


class RuntimeCall:
    def __init__(self, binding, budget, record):
        self.binding, self.budget, self.record = binding, budget, record
        self.store = binding.store
        self.dispatched = False

    @property
    def id(self):
        return self.record["reservation_id"]

    def dispatch(self, authority=None):
        self.budget.check_alive()
        def check():
            self.binding.authority()
            if authority:
                authority()
        accounts = [ref["account_id"] for ref in self.record["account_refs"]]
        self.store.mark_dispatched(self.id, ceilings=self.binding.ceilings(self.budget, accounts),
                                   authority_check=check, **AUDIT)
        self.dispatched = True

    def refund(self):
        if not self.dispatched:
            self.store.refund(self.id, **AUDIT)

    def abort(self, evidence):
        from .reservations import ReservationConflict
        record = self.store.get(self.id)
        if record["state"] not in {"reserved", "unknown"}:
            return
        if not record["dispatched"]:
            try:
                self.store.refund(self.id, **AUDIT)
                return
            except ReservationConflict:
                pass  # A timeout raced the worker's durable dispatch marker.
        record = self.store.get(self.id)
        if record["state"] == "reserved":
            self.unknown(evidence)

    def unknown(self, evidence):
        self.store.mark_unknown(self.id, evidence_ref=evidence, **AUDIT)

    def settle(self, cost, evidence):
        actual = dict(self.record["amounts"], cost_micros=micros(cost))
        self.store.settle(self.id, actual, evidence_ref=evidence, **AUDIT)


def binding_for(budget):
    while getattr(budget, "parent", None) is not None:
        budget = budget.parent
    return getattr(budget, "runtime_binding", None)


def model_call(budget, adapter, method, messages, *args, phase="react", on_pre_dispatch=None):
    """The one actual model invocation boundary; also preserves legacy accounting."""
    from ..models.metering import (adapter_model_id, pricing_status, normalize_token_usage,
        cost_from_tokens, settle_adapter_cost)
    try:
        profile = adapter.profile()
    except Exception:
        profile = None
    model_id = adapter_model_id(adapter)
    binding = binding_for(budget) if budget is not None else None
    permit = None
    free = pricing_status(profile) == "free"
    if budget is not None:
        budget.check_model_call(model_id)
    if binding:
        if free:
            hold = 0
        elif pricing_status(profile) == "unknown":
            configured = binding.service.config.unknown_model_reservation_cost
            if configured is None or micros(configured) == 0:
                raise BudgetExceeded("pricing", "unknown model price requires an explicit reservation ceiling")
            hold = micros(configured)
        else:
            current = budget
            bound = budget.limits.max_cost_per_model_call
            while getattr(current, "parent", None) is not None:
                current = current.parent
                bound = min(bound, current.limits.max_cost_per_model_call)
            profile_bound = getattr(profile, "adapter_config", {}).get("reservation_max_cost")
            if profile_bound is not None:
                micros(profile_bound)
                bound = min(bound, profile_bound)
            hold = micros(bound)
            if not hold:
                raise BudgetExceeded("pricing", "paid model requires a positive call ceiling")
        permit = binding.begin(budget, "model", model_id, hold, phase=phase, known_free=free)
    old_usage = getattr(adapter, "last_usage", None)
    old_call_id = getattr(old_usage, "call_id", None)
    sent = False
    try:
        if budget is not None:
            budget.record_model_call(0.0, model_id)
        if on_pre_dispatch:
            on_pre_dispatch(True)
        if permit:
            permit.dispatch()
        elif budget is not None:
            budget.check_alive()
        sent = True
        result = getattr(adapter, method)(messages, *args)
    except BaseException:
        if permit:
            if permit.dispatched:
                if free:
                    permit.settle(0, "model:error:declared-free")
                else:
                    permit.unknown("model:exception-after-dispatch")
            else:
                permit.refund()
        if on_pre_dispatch and not sent:
            on_pre_dispatch(False)
        if budget is not None and sent:
            settle_adapter_cost(budget, adapter, model_id, fallback_cost=0 if free else None)
        raise
    usage = getattr(adapter, "last_usage", None)
    known, cost, evidence = free, 0.0, "model:declared-free"
    if usage is not None and getattr(usage, "call_id", None) != old_call_id:
        if getattr(usage, "cost_source", None) in {"provider", "free"} and usage.cost is not None:
            known, cost, evidence = True, usage.cost, "model_usage:" + usage.call_id
    raw_usage = normalize_token_usage(usage)
    if raw_usage and usage is not old_usage and not free:
        calculated = cost_from_tokens(profile, raw_usage["input_tokens"], raw_usage["output_tokens"])
        if calculated is not None:
            known, cost, evidence = True, calculated, "provider_usage:" + (permit.id if permit else uuid.uuid4().hex)
    if permit:
        if known:
            permit.settle(cost, evidence)
        else:
            permit.unknown("model:missing-verified-cost")
    if budget is not None:
        before_unknown = budget.usage.unknown_model_costs
        try:
            fallback = adapter.estimate_cost(sum(len(m.content or "") for m in messages), len(str(result)))
        except Exception:
            fallback = None
        settle_adapter_cost(budget, adapter, model_id, fallback_cost=0 if free else fallback)
        if permit and not known and budget.usage.unknown_model_costs == before_unknown:
            budget.mark_unknown_model_cost(model_id)
    if permit and not known:
        raise BudgetExceeded("reconciliation_required", "provider cost is unknown; no new paid action")
    return result


def begin_tool(budget, contract, effect_id):
    binding = binding_for(budget) if budget is not None else None
    if binding is None:
        return None
    price = contract.cost_model.get("cost_per_call")
    # Non-network contracts declare local resource work, not supplier billing.
    # CPU/disk/electricity are NOT silently converted to monetary charges here.
    free = (price is None and not contract.requires_network) or (
        type(price) in (int, float) and price == 0)
    if contract.cost_model.get("currency", "USD") != "USD":
        raise BudgetExceeded("currency", "runtime does not implicitly convert supplier currencies")
    if free:
        hold = 0
    elif price is not None:
        hold = micros(price)
    else:
        cap = binding.service.config.unknown_tool_reservation_cost
        if cap is None or not micros(cap):
            raise BudgetExceeded("pricing", "network tool needs a price or an explicit hold ceiling")
        hold = micros(cap)
    permit = binding.begin(budget, "tool", contract.tool_id, hold,
                           phase="tool", effect_id=effect_id or "", known_free=free)
    permit.tool_free = free
    permit.tool_price = price
    return permit


def finish_tool(permit, budget, tool_id, result):
    if permit is None:
        return
    result.evidence["budget_reservation_id"] = permit.id
    billing = result.evidence.get("billing")
    if isinstance(billing, dict) and billing.get("known") is True and (
            billing.get("currency") == "USD" and isinstance(billing.get("receipt_ref"), str)
            and billing["receipt_ref"].strip()):
        cost, evidence = billing.get("cost"), billing["receipt_ref"]
        micros(cost)
    elif permit.tool_free and billing is None:
        cost, evidence = 0, "tool:declared-no-supplier-fee"
    elif result.ok and permit.tool_price is not None and billing is None:
        cost, evidence = permit.tool_price, "tool_contract:fixed-per-call:" + tool_id
    else:
        permit.unknown("tool:missing-verified-billing")
        budget.mark_unknown_tool_cost(tool_id)
        result.evidence["cost_reconciliation_required"] = True
        return
    permit.settle(cost, evidence)
    if cost:
        budget.settle_tool_cost(tool_id, cost)
