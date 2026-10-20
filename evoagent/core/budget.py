"""硬预算控制器（Architecture 12.1 / PRD 56）。

预算由程序化控制器执行，不依赖模型自觉遵守。
预算耗尽时抛 BudgetExceeded，由运行时控制器决定停止/降级/终止。
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


class BudgetExceeded(Exception):
    def __init__(self, dimension: str, message: str):
        super().__init__(f"预算耗尽[{dimension}]: {message}")
        self.dimension = dimension


@dataclass
class BudgetUsage:
    model_calls: int = 0
    tool_calls: int = 0
    cost: float = 0.0
    seconds: float = 0.0


@dataclass
class BudgetLimits:
    max_model_calls: int = 25
    max_tool_calls: int = 50
    max_cost: float = 1.0
    max_seconds: int = 300
    max_concurrent: int = 2


class BudgetLedger:
    """跨任务预算账本：按 scope + 日/月累计（PRD 55-58 / Arch 12）。

    任务级硬预算仍由 BudgetController 执行；账本负责运行时/进化/专家的
    日与月预算池，防止单个任务合法但整体失控。
    """

    def __init__(self, database: Optional[Any] = None):
        self.db = database
        self._mem: Dict[str, Dict[str, float]] = {}
        self._lock = threading.RLock()

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
                                 f"/{max_model_calls}")
        if max_cost is not None and u["cost"] >= max_cost:
            raise BudgetExceeded("daily_cost",
                                 f"{scope} {period} 已用 {u['cost']}"
                                 f"/{max_cost}")


class BudgetController:
    """单任务作用域的硬预算。多任务/进化预算池由 BudgetLedger 再计。"""

    def __init__(self, limits: Optional[BudgetLimits] = None,
                 overrides: Optional[Dict] = None,
                 initial_usage: Optional[BudgetUsage] = None,
                 ledger: Optional[BudgetLedger] = None,
                 ledger_scope: str = "runtime"):
        self.limits = limits or BudgetLimits()
        if overrides:
            for k, v in overrides.items():
                if hasattr(self.limits, k):
                    setattr(self.limits, k, v)
        # 暂停/恢复沿用同一任务的累计用量（PRD 55/56：单任务预算不因重启重置）
        self.usage = BudgetUsage(**(initial_usage.__dict__ if initial_usage
                                    else {}))
        self._restored_seconds = float(self.usage.seconds)
        self._started = time.monotonic()
        self.ledger = ledger
        self.ledger_scope = ledger_scope
        self._lock = threading.RLock()

    # ---- 校验（执行前调用）----
    def check_model_call(self) -> None:
        with self._lock:
            self._tick_seconds()
            if self.usage.model_calls + 1 > self.limits.max_model_calls:
                raise BudgetExceeded("model_calls",
                                     f"max={self.limits.max_model_calls}")

    def check_tool_call(self) -> None:
        with self._lock:
            self._tick_seconds()
            if self.usage.tool_calls + 1 > self.limits.max_tool_calls:
                raise BudgetExceeded("tool_calls",
                                     f"max={self.limits.max_tool_calls}")

    def check_alive(self) -> None:
        with self._lock:
            self._tick_seconds()
            if self.usage.seconds > self.limits.max_seconds:
                raise BudgetExceeded("seconds", f"max={self.limits.max_seconds}s")
            if self.usage.cost > self.limits.max_cost:
                raise BudgetExceeded("cost", f"max={self.limits.max_cost}")

    # ---- 计量（执行后调用）----
    def record_model_call(self, cost: float = 0.0) -> None:
        with self._lock:
            self.usage.model_calls += 1
            self.usage.cost += max(0.0, cost)
            if self.ledger is not None:
                self.ledger.record(self.ledger_scope, model_calls=1,
                                   cost=max(0.0, cost))

    def record_tool_call(self, cost: float = 0.0) -> None:
        with self._lock:
            self.usage.tool_calls += 1
            self.usage.cost += max(0.0, cost)
            self._tick_seconds()
            if self.ledger is not None:
                self.ledger.record(self.ledger_scope, tool_calls=1,
                                   cost=max(0.0, cost))

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def _tick_seconds(self) -> None:
        self.usage.seconds = self._restored_seconds + self.elapsed()

    def snapshot(self) -> Dict:
        with self._lock:
            self._tick_seconds()
            return {
                "limits": self.limits.__dict__,
                "usage": self.usage.__dict__,
            }
