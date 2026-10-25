"""主动强度、停止与资源非干扰（ALN-25-B，UX-21/F04-03·04/G-08）。

复用已接受切片组装「主动帮助的运行策略层」：
- 四档主动强度（off/low/normal/high）——只影响主动侧的预算与并发，
  永不放宽核心任务的资源；
- 主动侧独立预算池与并发上限（与核心任务分账，BudgetLedger 既有
  语义，scope="proactive"）；
- 无进展停止：主动任务连续 N 次无收益即停（G-06 口径），且**主动
  失败不修改核心状态**（本模块零写任务/账本）；
- 资源非干扰：高压时主动侧让位/暂停经 15-D ResourceGovernor 决策
  （消费既有实现，不重写采样与阈值）；
- 关闭即停：off 档不启动任何新非必要任务，并返回保留的安全提示
  （建议不丢失，状态可查）。

本模块是纯策略/决策层：不 submit_task、不改 Task 状态机、不写
副作用账本；执行仍由 controller 既有通道完成（装配留缺口）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

#: 四档主动程度（UX-21）
PROACTIVITY_LEVELS = ("off", "low", "normal", "high")

#: 各档策略：主动侧每日模型调用上限/并发上限/是否允许主动发起
_LEVEL_POLICY = {
    "off":    {"max_model_calls": 0,  "max_concurrent": 0,  "may_start": False},
    "low":    {"max_model_calls": 5,  "max_concurrent": 1,  "may_start": True},
    "normal": {"max_model_calls": 25, "max_concurrent": 2,  "may_start": True},
    "high":   {"max_model_calls": 60, "max_concurrent": 3,  "may_start": True},
}

#: 无进展停止（G-06）：连续无收益尝试达到阈值即停
NO_PROGRESS_LIMIT = {"low": 2, "normal": 3, "high": 5}


class ProactivityError(ValueError):
    """主动策略配置或状态错误。"""


def level_policy(level: str) -> Dict[str, Any]:
    if level not in PROACTIVITY_LEVELS:
        raise ProactivityError(f"未知主动档位: {level!r}（off/low/normal/high）")
    return dict(_LEVEL_POLICY[level])


@dataclass
class ProactivityDecision:
    """一次「是否允许主动行为」的决策（只读，附完整理由）。"""

    allowed: bool
    level: str
    reasons: List[str]
    safety_notice: Optional[str] = None   # off 档保留的安全提示

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "level": self.level,
                "reasons": list(self.reasons),
                "safety_notice": self.safety_notice}


class ProactivityGovernor:
    """主动侧运行策略：档位 + 独立预算/并发 + 无进展停止 + 资源让位。"""

    def __init__(self, *, level: str = "normal",
                 resource_governor: Optional[Any] = None,
                 ledger: Optional[Any] = None,
                 clock: Optional[Callable[[], float]] = None):
        self.level = level
        self.resource_governor = resource_governor   # 15-D ResourceGovernor
        self.ledger = ledger                         # BudgetLedger（scope=proactive 池）
        self._clock = clock or time.time
        self._no_progress_streak = 0
        self._stopped_for_no_progress = False

    def set_level(self, level: str) -> Dict[str, Any]:
        """档位切换（用户控制 UX-21）。off 即停：不影响已在执行的任务，
        但禁止新主动启动并给出安全提示。"""
        level_policy(level)  # 校验
        previous, self.level = self.level, level
        if level == "off":
            self._stopped_for_no_progress = False
        return {"previous": previous, "level": level,
                "notice": self._off_notice() if level == "off" else None}

    @staticmethod
    def _off_notice() -> str:
        return ("主动帮助已关闭：不再自动分析或发起建议任务；"
                "已保存的机会与建议保留可查，随时可重新开启。")

    # ---- 启动决策（F04-04：关闭不启动新非必要任务） ----

    def may_start(self, *, concurrent_proactive: int = 0) -> ProactivityDecision:
        policy = level_policy(self.level)
        reasons: List[str] = []
        if not policy["may_start"]:
            return ProactivityDecision(
                False, self.level, ["主动帮助已关闭（off 档）"],
                self._off_notice())
        if self._stopped_for_no_progress:
            reasons.append(
                f"连续无收益已达上限，主动侧已停（需用户重置或调档）")
        if concurrent_proactive >= policy["max_concurrent"]:
            reasons.append(
                f"主动并发已达上限 {policy['max_concurrent']}（核心任务不受此限）")
        # 独立预算池（与核心任务分账）
        if self.ledger is not None:
            try:
                self.ledger.check("proactive",
                                  max_model_calls=policy["max_model_calls"])
            except Exception as exc:   # BudgetExceeded 等独立预算拒绝
                reasons.append(f"主动预算池已尽: {exc}")
        # 资源非干扰（F04-03）：高压时主动侧让位——15-D 既有决策器
        if self.resource_governor is not None:
            decision = self.resource_governor.admit("opportunity")
            if decision.decision == "defer":
                reasons.append(f"资源压力让位核心任务: {decision.reason}")
        return ProactivityDecision(
            len(reasons) == 0, self.level, reasons,
            None if len(reasons) == 0 else None)

    # ---- 无进展停止（G-06 口径；主动失败不改核心状态） ----

    def record_attempt(self, *, had_benefit: bool) -> Dict[str, Any]:
        """记录一次主动尝试的收益结果。零写入核心状态。"""
        limit = NO_PROGRESS_LIMIT[self.level]
        if had_benefit:
            self._no_progress_streak = 0
        else:
            self._no_progress_streak += 1
            if self._no_progress_streak >= limit:
                self._stopped_for_no_progress = True
        return {"streak": self._no_progress_streak, "limit": limit,
                "stopped": self._stopped_for_no_progress}

    def reset_no_progress(self) -> None:
        self._no_progress_streak = 0
        self._stopped_for_no_progress = False

    # ---- 主动失败与核心状态隔离（验收：主动失败不修改核心状态） ----

    def handle_failure(self, reason: str) -> Dict[str, Any]:
        """主动侧失败的处置：只记决策层状态与提示，不触碰任务/账本。"""
        self.record_attempt(had_benefit=False)
        return {"isolated": True, "reason": reason,
                "core_state_touched": False,
                "notice": "主动分析失败已隔离；你的任务与数据不受影响。"}

    def status(self) -> Dict[str, Any]:
        policy = level_policy(self.level)
        return {"level": self.level, **policy,
                "no_progress_streak": self._no_progress_streak,
                "no_progress_limit": NO_PROGRESS_LIMIT[self.level],
                "stopped_for_no_progress": self._stopped_for_no_progress}
