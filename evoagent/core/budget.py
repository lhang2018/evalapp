"""硬预算控制器（Architecture 12.1 / PRD 56）。

预算由程序化控制器执行，不依赖模型自觉遵守。
预算耗尽时抛 BudgetExceeded，由运行时控制器决定停止/降级/终止。
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Optional


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


class BudgetController:
    """单任务作用域的硬预算。多任务/进化预算池在 EvolutionController 中按天再计。"""

    def __init__(self, limits: Optional[BudgetLimits] = None, overrides: Optional[Dict] = None):
        self.limits = limits or BudgetLimits()
        if overrides:
            for k, v in overrides.items():
                if hasattr(self.limits, k):
                    setattr(self.limits, k, v)
        self.usage = BudgetUsage()
        self._started = time.monotonic()

    # ---- 校验（执行前调用）----
    def check_model_call(self) -> None:
        self._tick_seconds()
        if self.usage.model_calls + 1 > self.limits.max_model_calls:
            raise BudgetExceeded("model_calls", f"max={self.limits.max_model_calls}")

    def check_tool_call(self) -> None:
        self._tick_seconds()
        if self.usage.tool_calls + 1 > self.limits.max_tool_calls:
            raise BudgetExceeded("tool_calls", f"max={self.limits.max_tool_calls}")

    def check_alive(self) -> None:
        self._tick_seconds()
        if self.usage.seconds > self.limits.max_seconds:
            raise BudgetExceeded("seconds", f"max={self.limits.max_seconds}s")
        if self.usage.cost > self.limits.max_cost:
            raise BudgetExceeded("cost", f"max={self.limits.max_cost}")

    # ---- 计量（执行后调用）----
    def record_model_call(self, cost: float = 0.0) -> None:
        self.usage.model_calls += 1
        self.usage.cost += max(0.0, cost)

    def record_tool_call(self, cost: float = 0.0) -> None:
        self.usage.tool_calls += 1
        self.usage.cost += max(0.0, cost)
        self._tick_seconds()

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def _tick_seconds(self) -> None:
        self.usage.seconds = self.elapsed()

    def snapshot(self) -> Dict:
        self._tick_seconds()
        return {
            "limits": self.limits.__dict__,
            "usage": self.usage.__dict__,
        }
