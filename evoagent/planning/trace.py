"""轨迹编译：输入绑定与恢复分支候选抽取（ALN-07-T）。

把 `ExecutionPlan` + 当前步骤状态编译成**纯读**的执行轨迹对象：

- 步骤序列（每步状态、依赖、输入绑定引用——只记来源不内联大对象）；
- 失败/跳过步骤的恢复分支候选，按依赖可达性分类：
  ``retryable``（重跑即可）、``blocked``（依赖链上有未恢复失败）、
  ``ready_after``（指明需先恢复哪些）；
- 修订历史与已完成证据原样进入轨迹，供崩溃恢复与评测消费。

设计约束（与 planning/matching.py 同口径）：
- 纯读：不修改计划、不执行任何动作、不触发恢复；
- 确定性：编译结果与遍历顺序无关（输入绑定引用排序、候选排序固定）；
- 显式：不可能恢复的候选明确标 ``infeasible`` 及原因，不静默省略。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .planner import ExecutionPlan

#: 步骤终态：不可能再变化
_TERMINAL_STATES = ("done", "skipped")


@dataclass(frozen=True)
class StepBinding:
    """一步的输入绑定引用（只记来源，不内联值本体）。"""

    step_id: str
    dep_ids: Tuple[str, ...] = ()
    tool_hint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "dep_ids": list(self.dep_ids),
                "tool_hint": self.tool_hint}


@dataclass(frozen=True)
class RecoveryCandidate:
    """一个失败/跳过步骤的恢复分支候选。"""

    step_id: str
    #: retryable | blocked | ready_after | infeasible
    kind: str
    step_state: str
    #: kind=ready_after 时：需先恢复的步骤
    requires: Tuple[str, ...] = ()
    #: kind=infeasible 时的原因
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "kind": self.kind,
                "step_state": self.step_state,
                "requires": list(self.requires), "reason": self.reason}


@dataclass(frozen=True)
class PlanTrace:
    """编译后的执行轨迹（不可变快照）。"""

    plan_id: str
    goal: str
    original_goal: str
    execution_mode: str
    selection_rationale: str
    steps: Tuple[StepBinding, ...] = ()
    states: Dict[str, str] = field(default_factory=dict)
    completed_work: Tuple[str, ...] = ()
    change_reasons: Tuple[str, ...] = ()
    revisions: Tuple[Dict[str, Any], ...] = ()
    acceptance_criteria: Tuple[Dict[str, Any], ...] = ()
    #: 失败/跳过步骤的恢复候选（按 step_id 排序）
    recovery_candidates: Tuple[RecoveryCandidate, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id, "goal": self.goal,
            "original_goal": self.original_goal,
            "execution_mode": self.execution_mode,
            "selection_rationale": self.selection_rationale,
            "steps": [s.to_dict() for s in self.steps],
            "states": dict(self.states),
            "completed_work": list(self.completed_work),
            "change_reasons": list(self.change_reasons),
            "revisions": [dict(r) for r in self.revisions],
            "acceptance_criteria": [dict(a) if isinstance(a, dict) else a
                                    for a in self.acceptance_criteria],
            "recovery_candidates": [c.to_dict()
                                    for c in self.recovery_candidates],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PlanTrace":
        steps = tuple(StepBinding(
            step_id=str(s.get("step_id") or ""),
            dep_ids=tuple(str(x) for x in (s.get("dep_ids") or ())),
            tool_hint=s.get("tool_hint")) for s in (d.get("steps") or [])
            if isinstance(s, dict))
        cands = tuple(RecoveryCandidate(
            step_id=str(c.get("step_id") or ""),
            kind=str(c.get("kind") or ""),
            step_state=str(c.get("step_state") or ""),
            requires=tuple(str(x) for x in (c.get("requires") or ())),
            reason=str(c.get("reason") or "")) for c in
            (d.get("recovery_candidates") or ()) if isinstance(c, dict))
        return cls(
            plan_id=str(d.get("plan_id") or ""),
            goal=str(d.get("goal") or ""),
            original_goal=str(d.get("original_goal") or ""),
            execution_mode=str(d.get("execution_mode") or ""),
            selection_rationale=str(d.get("selection_rationale") or ""),
            steps=steps,
            states={str(k): str(v) for k, v in (d.get("states") or {}).items()},
            completed_work=tuple(str(x) for x in (d.get("completed_work") or ())),
            change_reasons=tuple(str(x) for x in (d.get("change_reasons") or ())),
            revisions=tuple(dict(r) for r in (d.get("revisions") or [])
                            if isinstance(r, dict)),
            acceptance_criteria=tuple(
                dict(a) if isinstance(a, dict) else a
                for a in (d.get("acceptance_criteria") or ())),
            recovery_candidates=cands)


def compile_trace(plan: ExecutionPlan) -> PlanTrace:
    """从计划编译执行轨迹；纯读，不改计划。"""
    steps = tuple(StepBinding(
        step_id=s.step_id,
        dep_ids=tuple(sorted(s.deps)),
        tool_hint=s.tool_hint) for s in plan.steps)

    # 恢复分支候选：只对 failed/skipped 步骤生成
    by_id = {s.step_id: s for s in plan.steps}
    done = set(plan.completed_work)
    candidates: List[RecoveryCandidate] = []
    for s in plan.steps:
        if s.state not in ("failed", "skipped"):
            continue
        if s.step_id in done:
            # 修订后已完成又被标失败属异常状态：不可能也不需要恢复
            candidates.append(RecoveryCandidate(
                step_id=s.step_id, kind="infeasible", step_state=s.state,
                reason="步骤已完成（completed_work），不允许重放成功副作用"))
            continue
        # 依赖分类
        failed_deps = tuple(sorted(
            d for d in s.deps
            if (by_id[d].state == "failed" and d not in done)
            or by_id[d].state == "skipped"))
        pending_deps = tuple(sorted(
            d for d in s.deps
            if by_id[d].state not in _TERMINAL_STATES and d not in failed_deps))
        if failed_deps:
            candidates.append(RecoveryCandidate(
                step_id=s.step_id, kind="ready_after", step_state=s.state,
                requires=failed_deps,
                reason="依赖失败/跳过步骤恢复后可重试"))
        elif pending_deps:
            candidates.append(RecoveryCandidate(
                step_id=s.step_id, kind="blocked", step_state=s.state,
                requires=pending_deps,
                reason="仍有未完成依赖，不能立即重试"))
        else:
            candidates.append(RecoveryCandidate(
                step_id=s.step_id, kind="retryable", step_state=s.state,
                reason="依赖齐备，重跑即可"))
    candidates.sort(key=lambda c: c.step_id)

    return PlanTrace(
        plan_id=plan.plan_id, goal=plan.goal,
        original_goal=plan.original_goal or plan.goal,
        execution_mode=plan.execution_mode,
        selection_rationale=plan.selection_rationale,
        steps=steps,
        states={s.step_id: s.state for s in plan.steps},
        completed_work=tuple(plan.completed_work),
        change_reasons=tuple(plan.change_reasons),
        revisions=tuple(dict(r) for r in plan.revisions),
        acceptance_criteria=tuple(plan.acceptance_criteria),
        recovery_candidates=tuple(candidates))
