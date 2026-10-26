"""资源准入与核心任务优先（ALN-15-D，G-08/S-10）。

统一能力：向主动帮助/后台分析提供「资源压力下让出资源、恢复后回来」
的准入决策；核心（用户前台）任务在明确阈值内不被后台挤占。

职责边界：
- 本模块只产出**决策**（admit/defer/pause/resume），不直接改任务
  状态——暂停经 controller 既有 pause 通道（决策边界 SUSPENDED，
  检查点保留），恢复经 resume 通道；停止不损坏核心状态由既有
  状态机+检查点保证，这里不绕行。
- 采样器由宿主注入（真实 CPU/内存/模型池用量）；未注入或采样失败
  保持既有fail-open准入，但显式标记sample_valid=False；主动侧可据此拒绝启动。

阈值语义（验收：主任务满足明确阈值）：
- 压力值 = 各资源利用率（0-1）的最大值；
- 压力 ≥ high_water：后台（scheduled/derived/opportunity）暂停/准入
  拒绝；前台（user）不受影响；
- 压力 ≤ low_water：恢复放行；low_water < high_water 迟滞防抖。
"""

from __future__ import annotations

import time
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

#: 非前台来源（让位对象）
BACKGROUND_ORIGINS = ("scheduled", "derived", "opportunity")


class AdmissionConfigError(ValueError):
    """准入策略配置非法。"""


@dataclass(frozen=True)
class PressureSnapshot:
    cpu: float = 0.0
    memory: float = 0.0
    model_pool: float = 0.0
    sampled_at: float = 0.0
    sample_valid: bool = True

    @property
    def pressure(self) -> float:
        return min(1.0, max(0.0, self.cpu, self.memory, self.model_pool))

    def to_dict(self) -> Dict[str, Any]:
        return {"cpu": self.cpu, "memory": self.memory,
                "model_pool": self.model_pool,
                "pressure": self.pressure, "sampled_at": self.sampled_at,
                "sample_valid": self.sample_valid}


@dataclass(frozen=True)
class ResourcePolicy:
    """明确阈值：high/low 迟滞 + 前台豁免。"""

    high_water: float = 0.90
    low_water: float = 0.70
    #: 前台任务在超过该值时仍被准入拒绝（绝对上限；默认接近饱和）
    foreground_ceiling: float = 0.98

    def __post_init__(self) -> None:
        for name, value in (("high_water", self.high_water),
                            ("low_water", self.low_water),
                            ("foreground_ceiling", self.foreground_ceiling)):
            if not 0.0 < value <= 1.0:
                raise AdmissionConfigError(f"{name} 必须在 (0,1]: {value}")
        if not self.low_water < self.high_water <= self.foreground_ceiling:
            raise AdmissionConfigError(
                "阈值必须满足 low_water < high_water <= foreground_ceiling")


@dataclass
class AdmissionDecision:
    decision: str           # admit | defer | pause | resume
    origin: str
    pressure: float
    reason: str = ""
    snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"decision": self.decision, "origin": self.origin,
                "pressure": self.pressure, "reason": self.reason,
                "snapshot": self.snapshot}


class ResourceGovernor:
    """资源准入决策器（采样器注入；决策器本身无副作用）。"""

    def __init__(self,
                 sampler: Callable[[], Dict[str, float]],
                 policy: Optional[ResourcePolicy] = None,
                 *, now: Optional[Callable[[], float]] = None):
        if not callable(sampler):
            raise AdmissionConfigError("sampler 必须可调用")
        self.policy = policy or ResourcePolicy()
        self._sampler = sampler
        self._now = now or (lambda: time.time())
        #: 由宿主登记的因压力暂停的任务（恢复建议的输入，不代替 controller）
        self.paused_tasks: Dict[str, Dict[str, Any]] = {}

    def sample(self) -> PressureSnapshot:
        try:
            raw = self._sampler() or {}
            keys = ("cpu", "memory", "model_pool")
            supplied = [raw[key] for key in keys if key in raw]
            valid = bool(supplied) and all(
                type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
                for value in supplied)
            snapshot = PressureSnapshot(
                cpu=float(raw.get("cpu", 0.0)),
                memory=float(raw.get("memory", 0.0)),
                model_pool=float(raw.get("model_pool", 0.0)),
                sampled_at=self._now(), sample_valid=valid)
        except Exception:
            return PressureSnapshot(sampled_at=self._now(), sample_valid=False)  # legacy fail-open
        return snapshot

    def admit(self, origin: str) -> AdmissionDecision:
        """任务启动准入：高压拒后台、极端压力拒所有、前台豁免。"""
        snapshot = self.sample()
        pressure = snapshot.pressure
        if pressure >= self.policy.foreground_ceiling:
            return AdmissionDecision(
                "defer", origin, pressure,
                "资源压力达到绝对上限，全部任务暂缓", snapshot.to_dict())
        if origin in BACKGROUND_ORIGINS and pressure >= self.policy.high_water:
            return AdmissionDecision(
                "defer", origin, pressure,
                "资源高压，后台任务让位于核心任务", snapshot.to_dict())
        return AdmissionDecision("admit", origin, pressure, "", snapshot.to_dict())

    def should_pause(self, task_id: str, origin: str) -> Optional[AdmissionDecision]:
        """运行中低优先任务是否应让位（仅后台来源；前台不自动暂停）。"""
        if origin not in BACKGROUND_ORIGINS:
            return None
        snapshot = self.sample()
        if snapshot.pressure >= self.policy.high_water:
            decision = AdmissionDecision(
                "pause", origin, snapshot.pressure,
                "资源高压，后台任务让位（经既有 pause 通道，检查点保留）",
                snapshot.to_dict())
            self.paused_tasks[task_id] = decision.to_dict()
            return decision
        return None

    def resume_candidates(self) -> Dict[str, AdmissionDecision]:
        """压力回落到 low_water 以下时，给出可恢复任务的恢复决策。"""
        snapshot = self.sample()
        decisions: Dict[str, AdmissionDecision] = {}
        # Failure to measure is not evidence of recovery. Keep the paused set
        # until a valid low-water sample can justify resuming these tasks.
        if not snapshot.sample_valid:
            return decisions
        if snapshot.pressure <= self.policy.low_water:
            for task_id in list(self.paused_tasks):
                decisions[task_id] = AdmissionDecision(
                    "resume", "background", snapshot.pressure,
                    "资源已回落，后台任务可恢复", snapshot.to_dict())
            self.paused_tasks.clear()
        return decisions
