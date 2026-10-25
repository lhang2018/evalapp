"""跨专家迁移验证（ALN-27-A）。

用**任务质量**的前后对照证明经验迁移收益：同一专家在注入知识前后跑同一任务
集合，比较通过率变化。**命中率不能代替任务质量**——只给命中率而没有真实任务
结果时，一律判为 unproven。退化必须显式可见。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class TaskOutcome:
    """一次真实任务结果。``passed`` 由任务本身的验收决定，不是检索命中。"""

    task_id: str
    expert: str
    passed: bool

    def validate(self) -> None:
        if not str(self.task_id).strip():
            raise ValueError("task_id 必填")
        if not str(self.expert).strip():
            raise ValueError("expert 必填")


def _rate(passed: int, total: int) -> float:
    return passed / total if total else 0.0


@dataclass(frozen=True)
class ExpertDelta:
    expert: str
    before_passed: int
    before_total: int
    after_passed: int
    after_total: int

    @property
    def before_rate(self) -> float:
        return _rate(self.before_passed, self.before_total)

    @property
    def after_rate(self) -> float:
        return _rate(self.after_passed, self.after_total)

    @property
    def delta(self) -> float:
        return self.after_rate - self.before_rate


@dataclass(frozen=True)
class TransferReport:
    basis: str
    verdict: str
    sufficient: bool
    overall_before: Optional[float]
    overall_after: Optional[float]
    deltas: tuple[ExpertDelta, ...]
    degradations: tuple[str, ...]
    reasons: tuple[str, ...]


def _by_expert(outcomes: Iterable[TaskOutcome]) -> dict[str, list[TaskOutcome]]:
    grouped: dict[str, list[TaskOutcome]] = {}
    for outcome in outcomes:
        outcome.validate()
        grouped.setdefault(outcome.expert, []).append(outcome)
    return grouped


def _unproven(reason: str, *, hit_rate: Optional[float]) -> TransferReport:
    reasons = [reason]
    if hit_rate is not None:
        reasons.append("命中率不能代替任务质量，不能据此声称收益")
    return TransferReport(
        basis="task_quality",
        verdict="unproven",
        sufficient=False,
        overall_before=None,
        overall_after=None,
        deltas=(),
        degradations=(),
        reasons=tuple(reasons),
    )


def evaluate_transfer(
    before: Iterable[TaskOutcome],
    after: Iterable[TaskOutcome],
    *,
    hit_rate: Optional[float] = None,
) -> TransferReport:
    """比较同一专家集合在知识注入前后的任务质量，判定迁移收益。"""
    before_list = list(before)
    after_list = list(after)
    if not before_list or not after_list:
        return _unproven("缺少注入前或注入后的真实任务结果", hit_rate=hit_rate)

    grouped_before = _by_expert(before_list)
    grouped_after = _by_expert(after_list)
    if set(grouped_before) != set(grouped_after):
        return _unproven(
            "前后专家集合不一致，无法把变化归因到知识迁移", hit_rate=hit_rate
        )

    deltas: list[ExpertDelta] = []
    for expert in sorted(grouped_before):
        b = grouped_before[expert]
        a = grouped_after[expert]
        deltas.append(
            ExpertDelta(
                expert=expert,
                before_passed=sum(1 for o in b if o.passed),
                before_total=len(b),
                after_passed=sum(1 for o in a if o.passed),
                after_total=len(a),
            )
        )

    total_before = len(before_list)
    total_after = len(after_list)
    overall_before = _rate(sum(1 for o in before_list if o.passed), total_before)
    overall_after = _rate(sum(1 for o in after_list if o.passed), total_after)
    degradations = tuple(d.expert for d in deltas if d.delta < 0)
    reasons: list[str] = []
    if hit_rate is not None:
        reasons.append("命中率已忽略，结论只基于任务质量")

    if degradations:
        verdict = "degraded"
    elif overall_after > overall_before:
        verdict = "improved"
    else:
        verdict = "unchanged"

    return TransferReport(
        basis="task_quality",
        verdict=verdict,
        sufficient=True,
        overall_before=overall_before,
        overall_after=overall_after,
        deltas=tuple(deltas),
        degradations=degradations,
        reasons=tuple(reasons),
    )
