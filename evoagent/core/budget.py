"""硬预算控制器（Architecture 12.1 / PRD 55-58）。

预算由程序化控制器执行，不依赖模型自觉遵守。支持层级：

- 单任务（模型调用/工具调用/成本/时长/并发）
- 单次模型调用成本
- 单工具（调用次数/成本，可按工具覆盖）
- 单工作流（作用域预算）
- 单候选实验（评测执行器）
- 每日/每月进化池
- 模型提供方池
- 领域专家池

预算耗尽时抛 BudgetExceeded，由运行时控制器决定停止/降级/终止。
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .task import TaskStatus


class BudgetExceeded(Exception):
    def __init__(self, dimension: str, message: str, scope: Optional[str] = None):
        super().__init__(f"预算耗尽[{dimension}]: {message}")
        self.dimension = dimension
        self.scope = scope


@dataclass
class BudgetUsage:
    model_calls: int = 0
    tool_calls: int = 0
    cost: float = 0.0
    seconds: float = 0.0
    #: 成本未知的模型调用次数（价格档案缺失）。**不并入 cost**：
    #: 未知不是免费，按 0 元记账会系统性低估支出。
    unknown_model_costs: int = 0
    # 按工具/模型的归因，用于单工具预算与成本报告（PRD 55/58）
    tool_usage: Dict[str, Dict[str, float]] = field(default_factory=dict)
    model_usage: Dict[str, Dict[str, float]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BudgetUsage":
        return cls(
            model_calls=int(d.get("model_calls", 0) or 0),
            tool_calls=int(d.get("tool_calls", 0) or 0),
            cost=float(d.get("cost", 0.0) or 0.0),
            seconds=float(d.get("seconds", 0.0) or 0.0),
            unknown_model_costs=int(d.get("unknown_model_costs", 0) or 0),
            tool_usage=dict(d.get("tool_usage") or {}),
            model_usage=dict(d.get("model_usage") or {}))


@dataclass
class BudgetLimits:
    max_model_calls: int = 25
    max_tool_calls: int = 50
    max_cost: float = 1.0
    max_seconds: int = 300
    max_concurrent: int = 2
    # 单次模型调用预算（PRD 55）：超出后不再发起下一次调用
    max_cost_per_model_call: float = 0.25
    # 单工具预算（PRD 55）：默认上限 + 按工具覆盖
    max_tool_calls_per_tool: int = 10
    max_tool_cost_per_tool: float = 0.25
    tool_limits: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def tool_limit(self, tool_id: str, key: str, default: float) -> float:
        return float((self.tool_limits.get(tool_id) or {}).get(key, default))


@dataclass
class PoolLimit:
    """跨任务预算池限制（提供方/专家/进化日或月）。"""
    scope: str
    period: str = "day"
    max_model_calls: Optional[int] = None
    max_cost: Optional[float] = None


class BudgetLedger:
    """跨任务预算账本：按 scope + 日/月累计（PRD 55-58 / Arch 12）。

    任务级硬预算仍由 BudgetController 执行；账本负责运行时/进化/提供方/
    专家的日与月预算池，防止单个任务合法但整体失控。
    """

    def __init__(self, database: Optional[Any] = None):
        self.db = database
        self._mem: Dict[str, Dict[str, float]] = {}
        self._lock = threading.RLock()

    def reservation_store(self, *, clock=None):
        """Opt-in durable aggregate accounts; does not change legacy pool checks.

        Callers must reserve/claim dispatch/settle explicitly. A reservation is
        resource capacity, never permission to execute a model or tool action.
        """
        from .reservations import BudgetReservations
        return BudgetReservations(self, clock=clock)

    @staticmethod
    def _keys(scope: str, at: Optional[float] = None):
        ts = at if at is not None else time.time()
        dt = datetime.fromtimestamp(ts)
        return (f"{scope}:day:{dt:%Y-%m-%d}", f"{scope}:month:{dt:%Y-%m}")

    def record(self, scope: str, model_calls: int = 0, tool_calls: int = 0,
               cost: float = 0.0, seconds: float = 0.0) -> None:
        day_key, month_key = self._keys(scope)
        with self._lock:
            for key in (day_key, month_key):
                cur = self._mem.setdefault(
                    key, {"model_calls": 0, "tool_calls": 0,
                          "cost": 0.0, "seconds": 0.0})
                cur["model_calls"] += int(model_calls)
                cur["tool_calls"] += int(tool_calls)
                cur["cost"] += float(cost)
                cur["seconds"] += float(seconds)
                if self.db is not None:
                    self.db.record_budget(key, model_calls, tool_calls,
                                          cost, seconds)

    def usage(self, scope: str, period: str = "day",
              at: Optional[float] = None) -> Dict[str, float]:
        day_key, month_key = self._keys(scope, at)
        key = month_key if period == "month" else day_key
        if self.db is not None:
            return self.db.budget_usage(key)
        with self._lock:
            return dict(self._mem.get(
                key, {"model_calls": 0, "tool_calls": 0,
                      "cost": 0.0, "seconds": 0.0}))

    def report(self, scopes=("runtime", "evolution")) -> Dict[str, Any]:
        """按 scope 输出日/月用量，供 CLI 与管理端核对预算池（PRD 55-58）。"""
        out: Dict[str, Any] = {}
        for scope in scopes:
            out[scope] = {
                "day": self.usage(scope, "day"),
                "month": self.usage(scope, "month"),
            }
        return out

    def check(self, scope: str, *, max_model_calls: Optional[int] = None,
              max_cost: Optional[float] = None,
              period: str = "day") -> None:
        u = self.usage(scope, period)
        if max_model_calls is not None and u["model_calls"] >= max_model_calls:
            raise BudgetExceeded("daily_model_calls",
                                 f"{scope} {period} 已用 {u['model_calls']}"
                                 f"/{max_model_calls}", scope=scope)
        if max_cost is not None and u["cost"] >= max_cost:
            raise BudgetExceeded("daily_cost",
                                 f"{scope} {period} 已用 {u['cost']}"
                                 f"/{max_cost}", scope=scope)


class BudgetController:
    """单任务作用域的硬预算。多任务/进化预算池由 BudgetLedger 再计。"""

    def __init__(self, limits: Optional[BudgetLimits] = None,
                 overrides: Optional[Dict] = None,
                 initial_usage: Optional[BudgetUsage] = None,
                 ledger: Optional[BudgetLedger] = None,
                 ledger_scope: str = "runtime",
                 extra_scopes: Optional[List[str]] = None):
        self.limits = limits or BudgetLimits()
        if overrides:
            for k, v in overrides.items():
                if hasattr(self.limits, k):
                    setattr(self.limits, k, v)
        # 暂停/恢复沿用同一任务的累计用量（PRD 55/56：单任务预算不因重启重置）
        self.usage = BudgetUsage.from_dict(initial_usage.__dict__) \
            if initial_usage else BudgetUsage()
        self._restored_seconds = float(self.usage.seconds)
        self._started = time.monotonic()
        self.ledger = ledger
        self.ledger_scope = ledger_scope
        self._scopes: List[str] = []
        if ledger is not None:
            for scope in [ledger_scope] + list(extra_scopes or []):
                if scope and scope not in self._scopes:
                    self._scopes.append(scope)
        self.pools: List[PoolLimit] = []
        self._violations: Dict[str, str] = {}
        self._lock = threading.RLock()
        self.on_change = None
        self.authority_check = None
        self._scope_budgets = {}
        self._restored_scopes = {}

    def persist_usage(self):
        if self.on_change:
            self.on_change({**self.snapshot(), "scopes": {
                **self._restored_scopes,
                **{name: scope.snapshot() for name, scope in self._scope_budgets.items()}}})

    # ---- 预算池（提供方/专家/进化）----
    def bind_pool(self, scope: str, *, max_model_calls: Optional[int] = None,
                  max_cost: Optional[float] = None,
                  period: str = "day") -> None:
        """绑定一个账本预算池；同一 scope 只绑定一次。"""
        if self.ledger is None:
            return
        if scope not in self._scopes:
            self._scopes.append(scope)
        if any(p.scope == scope and p.period == period for p in self.pools):
            return
        self.pools.append(PoolLimit(scope=scope, period=period,
                                    max_model_calls=max_model_calls,
                                    max_cost=max_cost))

    def _check_pools(self) -> None:
        for p in self.pools:
            self.ledger.check(p.scope, max_model_calls=p.max_model_calls,
                              max_cost=p.max_cost, period=p.period)

    def _record_scopes(self, **kw: Any) -> None:
        if self.ledger is not None:
            for scope in self._scopes:
                self.ledger.record(scope, **kw)
        self.persist_usage()

    # ---- 校验（执行前调用）----
    def check_model_call(self, model_id: Optional[str] = None) -> None:
        with self._lock:
            if self.authority_check:
                self.authority_check()
            self._tick_seconds()
            self._raise_violation()
            if self.usage.model_calls + 1 > self.limits.max_model_calls:
                raise BudgetExceeded("model_calls",
                                     f"max={self.limits.max_model_calls}")
            self._check_pools()

    def check_tool_call(self, tool_id: Optional[str] = None) -> None:
        with self._lock:
            if self.authority_check:
                self.authority_check()
            self._tick_seconds()
            self._raise_violation()
            if self.usage.tool_calls + 1 > self.limits.max_tool_calls:
                raise BudgetExceeded("tool_calls",
                                     f"max={self.limits.max_tool_calls}")
            if tool_id:
                used = self.usage.tool_usage.get(tool_id, {})
                limit = int(self.limits.tool_limit(
                    tool_id, "max_calls", self.limits.max_tool_calls_per_tool))
                if int(used.get("calls", 0)) + 1 > limit:
                    raise BudgetExceeded(
                        f"tool_calls:{tool_id}",
                        f"{tool_id} max={limit}", scope=tool_id)

    def check_alive(self) -> None:
        with self._lock:
            if self.authority_check:
                self.authority_check()
            self._tick_seconds()
            self._raise_violation()
            if self.usage.seconds > self.limits.max_seconds:
                raise BudgetExceeded("seconds", f"max={self.limits.max_seconds}s")
            if self.usage.cost > self.limits.max_cost:
                raise BudgetExceeded("cost", f"max={self.limits.max_cost}")

    # ---- 计量（执行后调用）----
    def record_model_call(self, cost: float = 0.0,
                          model_id: Optional[str] = None) -> None:
        cost = max(0.0, float(cost))
        with self._lock:
            self.usage.model_calls += 1
            self.usage.cost += cost
            self._attribute(self.usage.model_usage, model_id, cost)
            self._record_scopes(model_calls=1, cost=cost)
            if cost > self.limits.max_cost_per_model_call:
                self._violations["model_call_cost"] = (
                    f"单次模型调用成本 {cost} > "
                    f"{self.limits.max_cost_per_model_call}")

    def settle_model_cost(self, cost: float, model_id: Optional[str] = None) -> None:
        """Add the result cost to an already reserved model call, without counting twice."""
        cost = max(0.0, float(cost))
        with self._lock:
            self.usage.cost += cost
            if model_id:
                usage = self.usage.model_usage.setdefault(model_id, {"calls": 0, "cost": 0.0})
                usage["cost"] += cost
            self._record_scopes(cost=cost)
            if cost > self.limits.max_cost_per_model_call:
                self._violations["model_call_cost"] = f"单次模型调用成本 {cost} > {self.limits.max_cost_per_model_call}"

    def mark_unknown_model_cost(self, model_id: Optional[str] = None) -> None:
        """标记一次调用的成本未知；不计入 cost，也不视为免费。

        价格档案缺失时，提供方用量可能可用但无法折算金额。此时既不能
        按 0 元结算（低估支出），也不能丢弃这条调用（无法对账），
        因此单独计数并随用量持久化。
        """
        with self._lock:
            self.usage.unknown_model_costs += 1
            if model_id:
                bucket = self.usage.model_usage.setdefault(
                    model_id, {"calls": 0, "cost": 0.0})
                bucket["unknown_costs"] = int(
                    bucket.get("unknown_costs", 0)) + 1
            self.persist_usage()

    def record_tool_call(self, tool_id: Optional[str] = None,
                         cost: float = 0.0) -> None:
        cost = max(0.0, float(cost))
        with self._lock:
            self.usage.tool_calls += 1
            self.usage.cost += cost
            self._attribute(self.usage.tool_usage, tool_id, cost)
            self._tick_seconds()
            self._record_scopes(tool_calls=1, cost=cost)
            if tool_id:
                limit = float(self.limits.tool_limit(
                    tool_id, "max_cost", self.limits.max_tool_cost_per_tool))
                spent = float(self.usage.tool_usage.get(
                    tool_id, {}).get("cost", 0.0))
                if spent > limit:
                    self._violations[f"tool_cost:{tool_id}"] = (
                        f"{tool_id} 成本 {spent} > {limit}")

    def record_usage(self, *, model_calls: int = 0, tool_calls: int = 0,
                     cost: float = 0.0, seconds: float = 0.0) -> None:
        """归因外部已发生的用量（如评测执行器），不参与单次调用上限判定。"""
        cost = max(0.0, float(cost))
        with self._lock:
            self.usage.model_calls += int(model_calls)
            self.usage.tool_calls += int(tool_calls)
            self.usage.cost += cost
            self._restored_seconds += max(0.0, float(seconds))
            self._record_scopes(model_calls=int(model_calls),
                                tool_calls=int(tool_calls), cost=cost,
                                seconds=max(0.0, float(seconds)))

    def violations(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._violations)

    def exceeded(self) -> Optional[str]:
        """返回已越界的维度/原因；未越界返回 None（用于聚合型预算）。"""
        with self._lock:
            self._tick_seconds()
            if self._violations:
                return next(iter(self._violations.values()))
            if self.usage.model_calls > self.limits.max_model_calls:
                return f"model_calls {self.usage.model_calls}" \
                       f" > {self.limits.max_model_calls}"
            if self.usage.tool_calls > self.limits.max_tool_calls:
                return f"tool_calls {self.usage.tool_calls}" \
                       f" > {self.limits.max_tool_calls}"
            if self.usage.cost > self.limits.max_cost:
                return f"cost {self.usage.cost} > {self.limits.max_cost}"
            if self.usage.seconds > self.limits.max_seconds:
                return f"seconds {self.usage.seconds} > {self.limits.max_seconds}"
            return None

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def _tick_seconds(self) -> None:
        self.usage.seconds = self._restored_seconds + self.elapsed()

    def _raise_violation(self) -> None:
        if self._violations:
            dim, msg = next(iter(self._violations.items()))
            raise BudgetExceeded(dim, msg)

    @staticmethod
    def _attribute(target: Dict[str, Dict[str, float]],
                   key: Optional[str], cost: float) -> None:
        if not key:
            return
        cur = target.setdefault(key, {"calls": 0, "cost": 0.0})
        cur["calls"] = int(cur.get("calls", 0)) + 1
        cur["cost"] = float(cur.get("cost", 0.0)) + cost

    # ---- 作用域预算（工作流/子任务）----
    def for_scope(self, name: str, limits: Optional[BudgetLimits] = None,
                  initial_usage: Optional[BudgetUsage] = None
                  ) -> "ScopedBudget":
        restored = self._restored_scopes.get(name, {}).get("usage")
        scope = ScopedBudget(self, name, limits or BudgetLimits(),
                             BudgetUsage.from_dict(restored) if restored is not None else initial_usage)
        self._scope_budgets[name] = scope
        return scope

    def snapshot(self) -> Dict:
        with self._lock:
            self._tick_seconds()
            return {
                "limits": self.limits.__dict__,
                "usage": self.usage.__dict__,
                "violations": dict(self._violations),
            }


class ScopedBudget:
    """父预算下的子作用域预算（Arch 12.2：子任务/工作流分配）。

    同时受父预算与本地限制约束；用量记入父账本与作用域账本。
    """

    def __init__(self, parent: BudgetController, name: str,
                 limits: BudgetLimits, initial_usage: Optional[BudgetUsage] = None):
        self.parent = parent
        self.name = name
        self.limits = limits
        self.usage = initial_usage or BudgetUsage()
        self._restored_seconds = self.usage.seconds
        self._started = time.monotonic()
        self._violations: Dict[str, str] = {}

    def _tick(self) -> None:
        self.usage.seconds = self._restored_seconds + time.monotonic() - self._started

    def _raise_violation(self) -> None:
        if self._violations:
            dim, msg = next(iter(self._violations.items()))
            raise BudgetExceeded(dim, msg, scope=self.name)

    def check_model_call(self, model_id: Optional[str] = None) -> None:
        self.parent.check_model_call(model_id)
        self._tick()
        self._raise_violation()
        if self.usage.model_calls + 1 > self.limits.max_model_calls:
            raise BudgetExceeded("model_calls",
                                 f"{self.name} max={self.limits.max_model_calls}",
                                 scope=self.name)

    def check_tool_call(self, tool_id: Optional[str] = None) -> None:
        self.parent.check_tool_call(tool_id)
        self._tick()
        self._raise_violation()
        if self.usage.tool_calls + 1 > self.limits.max_tool_calls:
            raise BudgetExceeded("tool_calls",
                                 f"{self.name} max={self.limits.max_tool_calls}",
                                 scope=self.name)
        if tool_id:
            used = self.usage.tool_usage.get(tool_id, {})
            limit = int(self.limits.tool_limit(
                tool_id, "max_calls", self.limits.max_tool_calls_per_tool))
            if int(used.get("calls", 0)) + 1 > limit:
                raise BudgetExceeded(
                    f"tool_calls:{tool_id}", f"{self.name}/{tool_id} max={limit}",
                    scope=self.name)

    def check_alive(self) -> None:
        self.parent.check_alive()
        self._tick()
        self._raise_violation()
        if self.usage.seconds > self.limits.max_seconds:
            raise BudgetExceeded("seconds",
                                 f"{self.name} max={self.limits.max_seconds}s",
                                 scope=self.name)
        if self.usage.cost > self.limits.max_cost:
            raise BudgetExceeded("cost",
                                 f"{self.name} max={self.limits.max_cost}",
                                 scope=self.name)

    def record_model_call(self, cost: float = 0.0,
                          model_id: Optional[str] = None) -> None:
        with self.parent._lock:
            cost = max(0.0, float(cost))
            self.parent.record_model_call(cost, model_id)
            self.usage.model_calls += 1
            self.usage.cost += cost
            BudgetController._attribute(self.usage.model_usage, model_id, cost)
            if self.parent.ledger is not None:
                self.parent.ledger.record(self.name, model_calls=1, cost=cost)
            if cost > self.limits.max_cost_per_model_call:
                self._violations["model_call_cost"] = (
                    f"{self.name} 单次调用成本 {cost} > "
                    f"{self.limits.max_cost_per_model_call}")
            self.parent.persist_usage()

    def settle_model_cost(self, cost: float, model_id: Optional[str] = None) -> None:
        with self.parent._lock:
            cost = max(0.0, float(cost))
            self.parent.settle_model_cost(cost, model_id)
            self.usage.cost += cost
            if model_id:
                self.usage.model_usage.setdefault(model_id, {"calls": 0, "cost": 0.0})["cost"] += cost
            if self.parent.ledger:
                self.parent.ledger.record(self.name, cost=cost)
            if cost > self.limits.max_cost_per_model_call:
                self._violations["model_call_cost"] = f"{self.name} 单次模型调用成本 {cost} > {self.limits.max_cost_per_model_call}"
            self.parent.persist_usage()

    def mark_unknown_model_cost(self, model_id: Optional[str] = None) -> None:
        with self.parent._lock:
            self.parent.mark_unknown_model_cost(model_id)
            self.usage.unknown_model_costs += 1
            if model_id:
                bucket = self.usage.model_usage.setdefault(
                    model_id, {"calls": 0, "cost": 0.0})
                bucket["unknown_costs"] = int(
                    bucket.get("unknown_costs", 0)) + 1
            self.parent.persist_usage()

    def record_tool_call(self, tool_id: Optional[str] = None,
                         cost: float = 0.0) -> None:
        with self.parent._lock:
            cost = max(0.0, float(cost))
            self.parent.record_tool_call(tool_id, cost)
            self.usage.tool_calls += 1
            self.usage.cost += cost
            BudgetController._attribute(self.usage.tool_usage, tool_id, cost)
            self._tick()
            if self.parent.ledger is not None:
                self.parent.ledger.record(self.name, tool_calls=1, cost=cost)
            if tool_id:
                limit = float(self.limits.tool_limit(
                    tool_id, "max_cost", self.limits.max_tool_cost_per_tool))
                spent = float(self.usage.tool_usage.get(
                    tool_id, {}).get("cost", 0.0))
                if spent > limit:
                    self._violations[f"tool_cost:{tool_id}"] = (
                        f"{self.name}/{tool_id} 成本 {spent} > {limit}")
            self.parent.persist_usage()

    def snapshot(self) -> Dict:
        self._tick()
        return {
            "scope": self.name,
            "limits": self.limits.__dict__,
            "usage": self.usage.__dict__,
            "violations": dict(self._violations),
        }


class CostReporter:
    """成本报告（PRD 58）：按任务/领域/模型/版本/候选实验聚合。

    数据来自任务账本（result_evidence.budget）与评测报告，不把计划成本
    当作实际成本。
    """

    def __init__(self, db: Any, evolution: Optional[Any] = None):
        self.db = db
        self.evolution = evolution

    def report(self, limit: int = 500) -> Dict[str, Any]:
        tasks = self.db.list_tasks(limit=limit)
        totals = {"tasks": len(tasks), "model_calls": 0, "tool_calls": 0,
                  "cost": 0.0, "unknown_model_costs": 0,
                  "successful_tasks": 0, "failed_tasks": 0,
                  "successful_cost": 0.0, "failed_cost": 0.0}
        by_task: List[Dict[str, Any]] = []
        by_domain: Dict[str, Dict[str, Any]] = {}
        by_model: Dict[str, Dict[str, Any]] = {}
        by_version: Dict[str, Dict[str, Any]] = {}
        for t in tasks:
            usage = _usage_of(t)
            cost = float(usage.get("cost", 0.0) or 0.0)
            model_calls = int(usage.get("model_calls", 0) or 0)
            tool_calls = int(usage.get("tool_calls", 0) or 0)
            unknown_costs = int(usage.get("unknown_model_costs", 0) or 0)
            model = ((t.runtime_manifest or {}).get("model_route") or {}
                     ).get("model_id") or "unrouted"
            version = (t.version_manifest or {}).get("version") or "unreleased"
            ok = t.task_status == TaskStatus.SUCCEEDED
            failed = t.task_status == TaskStatus.FAILED
            totals["model_calls"] += model_calls
            totals["tool_calls"] += tool_calls
            totals["cost"] += cost
            totals["unknown_model_costs"] += unknown_costs
            if ok:
                totals["successful_tasks"] += 1
                totals["successful_cost"] += cost
            if failed:
                totals["failed_tasks"] += 1
                totals["failed_cost"] += cost
            by_task.append({
                "task_id": t.task_id, "intent": t.intent, "domain": t.domain,
                "model": model, "version": version,
                "status": t.task_status.value, "cost": round(cost, 6),
                "model_calls": model_calls, "tool_calls": tool_calls,
                "unknown_model_costs": unknown_costs,
                "success": ok, "failure_reason": t.failure_reason})
            for bucket, key in ((by_domain, t.domain or "general"),
                                (by_model, model), (by_version, version)):
                _accumulate(bucket, key, cost, model_calls, tool_calls, ok)
        succeeded = totals["successful_tasks"]
        totals["cost_per_success"] = round(
            totals["successful_cost"] / succeeded, 6) if succeeded else None
        totals["cost_per_success_including_waste"] = round(
            totals["cost"] / succeeded, 6) if succeeded else None
        return {
            "totals": totals,
            "by_task": by_task,
            "by_domain": _finalize(totals, by_domain),
            "by_model": _finalize(totals, by_model),
            "by_version": _finalize(totals, by_version),
            "evolution": self._evolution_report(),
        }

    def _evolution_report(self) -> Dict[str, Any]:
        reports = self.db.list_eval_reports(limit=1000)
        candidates: List[Dict[str, Any]] = []
        total_experiment_cost = 0.0
        for r in reports:
            data = r.get("data") or {}
            cand = data.get("candidate") or {}
            base = data.get("baseline") or {}
            holdout = data.get("holdout") or {}
            experiment_cost = sum(
                float(x.get("cost", 0.0) or 0.0)
                for x in (cand, base, holdout))
            total_experiment_cost += experiment_cost
            base_rate = base.get("success_rate")
            cand_rate = cand.get("success_rate")
            candidates.append({
                "report_id": r.get("report_id"),
                "candidate_id": r.get("candidate_id"),
                "dataset_id": r.get("dataset_id"),
                "experiment_cost": round(experiment_cost, 6),
                "candidate_cost": round(float(cand.get("cost", 0.0) or 0.0), 6),
                "cost_delta": round(float(cand.get("cost", 0.0) or 0.0)
                                    - float(base.get("cost", 0.0) or 0.0), 6),
                "quality_gain": (round(cand_rate - base_rate, 6)
                                 if cand_rate is not None
                                 and base_rate is not None else None),
                "success_rate": cand_rate,
            })
        return {"total_experiment_cost": round(total_experiment_cost, 6),
                "experiments": len(candidates),
                "candidates": candidates}


def _usage_of(task: Any) -> Dict[str, Any]:
    for ev in reversed(task.result_evidence or []):
        if isinstance(ev, dict) and isinstance(ev.get("budget"), dict):
            return ev["budget"].get("usage") or {}
    return {}


def _accumulate(bucket: Dict[str, Dict[str, Any]], key: str, cost: float,
                model_calls: int, tool_calls: int, ok: bool) -> None:
    cur = bucket.setdefault(key, {"tasks": 0, "cost": 0.0,
                                  "model_calls": 0, "tool_calls": 0,
                                  "successful_tasks": 0,
                                  "successful_cost": 0.0})
    cur["tasks"] += 1
    cur["cost"] += cost
    cur["model_calls"] += model_calls
    cur["tool_calls"] += tool_calls
    if ok:
        cur["successful_tasks"] += 1
        cur["successful_cost"] += cost


def _finalize(totals: Dict[str, Any],
              buckets: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    for v in buckets.values():
        succeeded = v["successful_tasks"]
        v["cost"] = round(v["cost"], 6)
        v["successful_cost"] = round(v["successful_cost"], 6)
        v["cost_per_success"] = round(
            v["successful_cost"] / succeeded, 6) if succeeded else None
    return buckets
