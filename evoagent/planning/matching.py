"""工作流匹配与执行模式选择（ALN-07-W）。

服务是**纯读**的：只读上下文快照做五项检查（输入/权限/依赖/版本/预算），
不做授权判定、不扣预算、不改任何持久状态。预算不足时给出可查原因，
不得静默降级为「预算充足」。

模式规则（与《总体架构》§6 一致）：

1. 有候选**五项全过** → ``stable_workflow``；多个候选时取成本最低者，
   同成本按 (workflow_id, version) 字典序，保证与候选顺序无关；
2. 无全过候选，但存在**只差一项**且该项在候选声明的 ``dynamic_tolerates`` 内
   → ``hybrid_agent``，主流程取该候选，需动态判断的节点写入理由；
3. 其余 → ``dynamic_planning``，理由列出最接近候选缺了什么。

缺失任一检查都不允许被当作「隐式默认」，必须写进理由。
"""
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .contracts import (CHECK_NAMES, CandidateVerdict, CheckResult, MatchContext,
                        ModeSelection, WorkflowCandidate)


class WorkflowMatcher:
    """按五项检查评估候选工作流，并显式选择执行模式。"""

    def __init__(self, candidates: Iterable[WorkflowCandidate] = ()):
        self._candidates: Tuple[WorkflowCandidate, ...] = tuple(candidates)
        keys = [candidate.key() for candidate in self._candidates]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate workflow identity")

    @property
    def candidates(self) -> Tuple[WorkflowCandidate, ...]:
        return self._candidates

    # ---- 五项检查 ----

    @staticmethod
    def _check_inputs(candidate: WorkflowCandidate,
                      context: MatchContext) -> CheckResult:
        missing = tuple(sorted(set(candidate.required_inputs)
                               - set(context.available_inputs)))
        return CheckResult(
            "inputs", not missing, missing,
            "缺少输入: " + ", ".join(missing) if missing else "输入齐备")

    @staticmethod
    def _check_permissions(candidate: WorkflowCandidate,
                           context: MatchContext) -> CheckResult:
        missing = tuple(sorted(set(candidate.required_permissions)
                               - set(context.granted_permissions)))
        return CheckResult(
            "permissions", not missing, missing,
            "缺少权限: " + ", ".join(missing) if missing else "权限齐备")

    @staticmethod
    def _check_dependencies(candidate: WorkflowCandidate,
                            context: MatchContext) -> CheckResult:
        missing = tuple(sorted(set(candidate.dependencies)
                               - set(context.available_dependencies)))
        return CheckResult(
            "dependencies", not missing, missing,
            "缺少依赖: " + ", ".join(missing) if missing else "依赖齐备")

    @staticmethod
    def _check_versions(candidate: WorkflowCandidate,
                        context: MatchContext) -> CheckResult:
        supported = tuple(candidate.supported_model_versions)
        if not supported:
            return CheckResult("versions", True, (), "未声明模型版本约束")
        if not context.model_version:
            return CheckResult("versions", False, supported,
                               "模型版本未知，无法确认适用性")
        if context.model_version in supported:
            return CheckResult("versions", True, (),
                               f"模型版本 {context.model_version} 受支持")
        return CheckResult("versions", False, supported,
                           f"模型版本 {context.model_version} 不在支持范围 "
                           f"{', '.join(supported)}")

    @staticmethod
    def _check_budget(candidate: WorkflowCandidate,
                      context: MatchContext) -> CheckResult:
        need = int(candidate.cost_estimate or 0)
        if not context.budget_known:
            return CheckResult("budget", False, (),
                               f"预算不可知，无法确认 {need} 的额度")
        if need <= 0:
            return CheckResult("budget", True, (), "未声明成本")
        remaining = int(context.remaining_budget)
        if need > remaining:
            return CheckResult("budget", False, (),
                               f"预算不足：需要 {need}，可用 {remaining}")
        return CheckResult("budget", True, (), f"预算可用：{need} <= {remaining}")

    # ---- 判定与选择 ----

    def evaluate(self, candidate: WorkflowCandidate,
                 context: MatchContext) -> CandidateVerdict:
        checks = (
            self._check_inputs(candidate, context),
            self._check_permissions(candidate, context),
            self._check_dependencies(candidate, context),
            self._check_versions(candidate, context),
            self._check_budget(candidate, context),
        )
        failed = [c for c in checks if not c.ok]
        missing: List[str] = []
        for check in failed:
            if check.missing:
                missing.extend(f"{check.name}:{item}" for item in check.missing)
            else:
                missing.append(f"{check.name}:{check.detail}")
        dynamic_gap = ""
        # Only missing inputs can be obtained by declared dynamic nodes.
        # Permissions, compatibility, dependencies and budget remain hard gates.
        if (len(failed) == 1 and failed[0].name == "inputs"
                and "inputs" in candidate.dynamic_tolerates
                and candidate.dynamic_nodes):
            dynamic_gap = failed[0].name
        return CandidateVerdict(
            workflow_id=candidate.workflow_id, version=candidate.version,
            applicable=not failed, checks=checks, missing=tuple(missing),
            cost_estimate=int(candidate.cost_estimate or 0),
            dynamic_nodes=tuple(candidate.dynamic_nodes),
            dynamic_gap=dynamic_gap)

    def select(self, context: MatchContext) -> ModeSelection:
        """选出执行模式并给出理由；不隐式默认。"""
        verdicts = tuple(sorted(
            (self.evaluate(candidate, context) for candidate in self._candidates),
            key=lambda v: (v.workflow_id, v.version)))
        by_key = {candidate.key(): candidate for candidate in self._candidates}

        applicable = [v for v in verdicts if v.applicable]
        if applicable:
            best = min(applicable,
                       key=lambda v: (v.cost_estimate, v.workflow_id, v.version))
            candidate = by_key[(best.workflow_id, best.version)]
            rationale = (
                f"稳定工作流 {best.workflow_id}@{best.version} 五项检查全部通过"
                f"（成本 {best.cost_estimate}）"
                + (f"；{candidate.description}" if candidate.description else ""))
            return ModeSelection(
                mode="stable_workflow", workflow_id=best.workflow_id,
                version=best.version, rationale=rationale,
                checks=best.checks, verdicts=verdicts)

        partial = [v for v in verdicts if v.dynamic_gap]
        if partial:
            best = min(partial,
                       key=lambda v: (v.cost_estimate, v.workflow_id, v.version))
            candidate = by_key[(best.workflow_id, best.version)]
            nodes = ", ".join(candidate.dynamic_nodes) or "未声明节点"
            rationale = (
                f"混合 Agent 工作流 {best.workflow_id}@{best.version}：主流程可用，"
                f"仅缺 {best.dynamic_gap}（允许运行时动态补齐），"
                f"需动态判断节点: {nodes}")
            return ModeSelection(
                mode="hybrid_agent", workflow_id=best.workflow_id,
                version=best.version, rationale=rationale,
                checks=tuple(c for c in best.checks if not c.ok),
                verdicts=verdicts, dynamic_nodes=tuple(candidate.dynamic_nodes))

        nearest = None
        if verdicts:
            nearest = min(verdicts,
                          key=lambda v: (len(v.missing), v.workflow_id, v.version))
        if nearest is None:
            rationale = "无候选工作流：执行模式为动态规划（工具白名单/步数/时限/预算内探索）"
        else:
            detail = "; ".join(nearest.missing) or "无可用候选"
            rationale = (f"无稳定工作流可用（最接近 {nearest.workflow_id}@"
                         f"{nearest.version} 缺 {detail}）：执行模式为动态规划")
        return ModeSelection(mode="dynamic_planning", rationale=rationale,
                             verdicts=verdicts)


def selection_from_dict(raw: Dict[str, Any]) -> ModeSelection:
    """从可序列化字典还原选择结果（供计划序列化往返）。"""
    checks = tuple(CheckResult(
        name=str(c.get("name") or ""), ok=bool(c.get("ok")),
        missing=tuple(str(m) for m in (c.get("missing") or ())),
        detail=str(c.get("detail") or "")) for c in (raw.get("checks") or ()))
    verdicts = tuple(CandidateVerdict(
        workflow_id=str(v.get("workflow_id") or ""),
        version=str(v.get("version") or ""),
        applicable=bool(v.get("applicable")),
        checks=tuple(CheckResult(
            name=str(c.get("name") or ""), ok=bool(c.get("ok")),
            missing=tuple(str(m) for m in (c.get("missing") or ())),
            detail=str(c.get("detail") or "")) for c in (v.get("checks") or ())),
        missing=tuple(str(m) for m in (v.get("missing") or ())),
        cost_estimate=int(v.get("cost_estimate") or 0),
        dynamic_nodes=tuple(str(n) for n in (v.get("dynamic_nodes") or ())),
        dynamic_gap=str(v.get("dynamic_gap") or ""))
        for v in (raw.get("verdicts") or ()))
    return ModeSelection(
        mode=str(raw.get("mode") or "dynamic_planning"),
        workflow_id=str(raw.get("workflow_id") or ""),
        version=str(raw.get("version") or ""),
        rationale=str(raw.get("rationale") or ""),
        checks=checks, verdicts=verdicts,
        dynamic_nodes=tuple(str(n) for n in (raw.get("dynamic_nodes") or ())))
