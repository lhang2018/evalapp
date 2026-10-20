"""进化控制器（PRD 12-13 / 46-50 / Architecture 10）。

职责：问题优先级排序、影响范围分析、根因假设管理、优化方法选择、
候选任务生成、实验分配、评测调度、收益分析、风险分级、发布资格、回滚触发。
进化控制器本身也在版本管理和评测范围内（Arch 22.4）。
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.events import EventBus
from ..tools.registry import ToolRegistry
from .candidates import (ALLOWED, CandidateStatus, EvolutionCandidate,
                         IllegalCandidateTransition)
from .evaluator import EvalDataset, Evaluator, EvalReport, ReleaseGate

# PRD 80 风险分级 L0-L4
RISK_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}

# PRD 81 发布规则
PUBLISH_RULES: Dict[str, str] = {
    "L0": "auto_preapproved",      # 预先批准范围内自动执行
    "L1": "auto_gate",             # 可通过门禁自动发布
    "L2": "authorized_release",    # 满足领域回归/风险/回滚后按授权发布
    "L3": "strict_review",         # 更严格的审批与测试
    "L4": "manual_only",           # 不允许普通进化流程自行发布
}


@dataclass
class ProblemTicket:
    ticket_id: str
    description: str
    severity: str = "medium"        # low | medium | high | critical
    source_task_id: Optional[str] = None
    evidence: List[str] = field(default_factory=list)
    detected_at: float = field(default_factory=time.time)


class EvolutionController:
    def __init__(self, evaluator: Optional[Evaluator] = None,
                 gate: Optional[ReleaseGate] = None,
                 budget: Optional[Any] = None,
                 event_bus: Optional[EventBus] = None):
        self.evaluator = evaluator
        self.gate = gate or ReleaseGate()
        self.budget = budget
        self.bus = event_bus
        self.candidates: Dict[str, EvolutionCandidate] = {}
        self.tickets: deque = deque()
        self._seq = 0

    # ---- 问题登记与排序 ----
    def register_problem(self, ticket: ProblemTicket) -> None:
        self.tickets.append(ticket)

    def _next_candidate_id(self) -> str:
        self._seq += 1
        return f"cand_{self._seq:03d}"

    # ---- 候选生命周期 ----
    def propose_candidate(self, baseline_version: str, target_component: str,
                          target_problem: str, hypothesis: str,
                          change_scope: str = "local",
                          evidence: Optional[List[str]] = None) -> EvolutionCandidate:
        cand = EvolutionCandidate(
            candidate_id=self._next_candidate_id(),
            baseline_version=baseline_version,
            target_component=target_component,
            target_problem=target_problem,
            hypothesis=hypothesis,
            change_scope=change_scope,
            root_cause_evidence=list(evidence or []),
            # PRD 50：候选必须有明确回滚版本
            rollback_version=baseline_version,
        )
        cand.transition(CandidateStatus.TRIAGED, "影响范围与优先级已评估",
                        {"severity": "auto"})
        cand.transition(CandidateStatus.HYPOTHESIZED, "形成改进假设",
                        {"hypothesis": hypothesis})
        cand.transition(CandidateStatus.CANDIDATE_CREATED, "创建候选版本",
                        {"scope": change_scope})
        self.candidates[cand.candidate_id] = cand
        if self.bus:
            self.bus.emit(event_type="evolution_candidate", component_id="evolution",
                          payload={"candidate_id": cand.candidate_id,
                                   "target": target_component})
        return cand

    def candidate_from_hypothesis(self, hypothesis: Any, baseline_version: str,
                                  change_scope: Optional[str] = None,
                                  risk_level: Optional[str] = None
                                  ) -> EvolutionCandidate:
        """诊断假设升级为进化候选（PRD 45）。

        诊断本身不发布；候选仍需走实验、门禁与发布规则。
        """
        scope = change_scope or "local"
        risk = risk_level or risk_for_component(hypothesis.component)
        evidence = [f"hypothesis:{hypothesis.hypothesis_id}"]
        evidence.extend(hypothesis.supporting_evidence)
        cand = self.propose_candidate(
            baseline_version=baseline_version,
            target_component=hypothesis.component,
            target_problem=hypothesis.statement,
            hypothesis=f"[{hypothesis.category.value}] {hypothesis.statement}",
            change_scope=scope,
            evidence=evidence)
        cand.risk_level = risk
        if self.bus:
            self.bus.emit(event_type="evolution_candidate_risk",
                          component_id="evolution",
                          payload={"candidate_id": cand.candidate_id,
                                   "risk_level": risk,
                                   "publish_rule": cand.publish_suggestion})
        return cand

    def run_evaluation(self, cand: EvolutionCandidate,
                       dataset: EvalDataset,
                       baseline_report: Optional[EvalReport] = None) -> Dict[str, Any]:
        cand.transition(CandidateStatus.TESTING, "进入隔离实验环境测试",
                        {"dataset": dataset.dataset_id})
        cand.transition(CandidateStatus.EVALUATING, "对比基线与候选")
        report = self.evaluator.run(dataset, baseline=False,
                                    metadata={"candidate": cand.candidate_id})
        cand.quality_result = report.summary()
        cand.cost_delta = report.cost
        cand.latency_delta = report.avg_latency
        if baseline_report is not None:
            gate = self.gate.judge(baseline_report, report)
            cand.publish_suggestion = "release_ready" if gate["passed"] else "rejected"
            if not gate["passed"]:
                cand.transition(CandidateStatus.REJECTED,
                                "门禁未通过: " + "; ".join(gate["reasons"]))
            else:
                cand.transition(CandidateStatus.APPROVAL_PENDING, "门禁通过待批准")
                self.candidates[cand.candidate_id] = cand
            return {**gate, "report": report.summary()}
        self.candidates[cand.candidate_id] = cand
        return {"passed": None, "report": report.summary()}

    def approve(self, cand: EvolutionCandidate, approver: str = "auto") -> Dict[str, Any]:
        rule = PUBLISH_RULES.get(cand.risk_level, "strict_review")
        if cand.status == CandidateStatus.APPROVAL_PENDING:
            cand.transition(CandidateStatus.RELEASE_READY, f"批准({approver}); 风险{cand.risk_level} 规则 {rule}")
        return {"candidate_id": cand.candidate_id, "status": cand.status.value,
                "publish_rule": rule}

    def mark_deploying(self, cand: EvolutionCandidate) -> None:
        cand.transition(CandidateStatus.DEPLOYING, "签名制品生成，受控开始部署")

    def mark_monitoring(self, cand: EvolutionCandidate) -> None:
        cand.transition(CandidateStatus.MONITORING, "进入发布后监测（PRD 82）")

    def promote(self, cand: EvolutionCandidate, metrics: Dict[str, Any]) -> None:
        cand.transition(CandidateStatus.PROMOTED, "监测达标晋级",
                        {"metrics": metrics})
        cand.final_result = "PROMOTED"

    def rollback(self, cand: EvolutionCandidate, reason: str) -> None:
        cand.transition(CandidateStatus.ROLLED_BACK, "指标越过阈值或安全事件: " + reason)
        cand.final_result = f"ROLLED_BACK -> {cand.rollback_version}"

    # ---- 影响范围优先顺序（Arch 10.6）----
    def improvement_priority(self, scope: str) -> int:
        order = ["task_local", "workflow", "tool_or_adapter", "expert", "shared", "core"]
        return order.index(scope) if scope in order else len(order)

    def status_of(self, candidate_id: str) -> Optional[CandidateStatus]:
        c = self.candidates.get(candidate_id)
        return c.status if c else None


def risk_for_component(component: str) -> str:
    """PRD 81：改动范围 → 风险等级。核心/安全域只能人工发布。"""
    c = (component or "").lower()
    if (c.startswith("core/") or "safety" in c or "state_machine" in c
            or "state-machine" in c or c in ("permissions/system",
                                             "harness/budget")):
        return "L4"
    if c.startswith("shared/"):
        return "L3"
    if c.startswith(("expert/", "workflow/", "tool/")):
        return "L2"
    return "L1"
