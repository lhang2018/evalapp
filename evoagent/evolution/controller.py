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
                 event_bus: Optional[EventBus] = None,
                 store: Optional[Any] = None,
                 releases: Optional[Any] = None,
                 datasets: Optional[Dict[str, EvalDataset]] = None):
        self.evaluator = evaluator
        self.gate = gate or ReleaseGate()
        self.budget = budget
        self.bus = event_bus
        self.store = store
        self.releases = releases
        self.datasets: Dict[str, EvalDataset] = dict(datasets or {})
        self.candidates: Dict[str, EvolutionCandidate] = {}
        self.tickets: deque = deque()
        self._seq = 0
        if store is not None:
            loaded = store.list_candidates(limit=1000)
            for cand in loaded:
                self.candidates[cand.candidate_id] = cand
                try:
                    self._seq = max(self._seq,
                                    int(cand.candidate_id.split("_")[-1]))
                except ValueError:
                    pass

    def attach_evaluator(self, evaluator: Evaluator,
                         datasets: Optional[Dict[str, EvalDataset]] = None) -> None:
        """装配后期注入，避免 RuntimeController ↔ EvolutionController 循环依赖。"""
        self.evaluator = evaluator
        if datasets:
            self.datasets.update(datasets)

    def _persist(self, cand: EvolutionCandidate) -> None:
        self.candidates[cand.candidate_id] = cand
        if self.store is not None:
            self.store.save_candidate(cand)

    def all_candidates(self) -> List[EvolutionCandidate]:
        return sorted(self.candidates.values(),
                      key=lambda c: c.created_at, reverse=True)

    def get_candidate(self, candidate_id: str) -> Optional[EvolutionCandidate]:
        return self.candidates.get(candidate_id)

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
        self._persist(cand)
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
        self._persist(cand)
        if self.bus:
            self.bus.emit(event_type="evolution_candidate_risk",
                          component_id="evolution",
                          payload={"candidate_id": cand.candidate_id,
                                   "risk_level": risk,
                                   "publish_rule": cand.publish_suggestion})
        return cand

    def evaluate_candidate(self, candidate_id: str,
                           dataset_id: Optional[str] = None,
                           baseline: bool = True) -> Dict[str, Any]:
        """完整评测路径：基线 → 候选 → 门禁 → 报告落盘（PRD 51-54）。"""
        cand = self.candidates.get(candidate_id)
        if cand is None:
            raise KeyError(candidate_id)
        if self.evaluator is None:
            raise RuntimeError("评测器未装配：无法运行候选评测")
        ds = self.datasets.get(dataset_id or cand.eval_dataset_id or "")
        if ds is None:
            ds = next(iter(self.datasets.values()), None)
        if ds is None:
            raise ValueError("没有可用评测集")
        cand.eval_dataset_id = ds.dataset_id
        baseline_report = None
        if baseline:
            baseline_report = self.evaluator.run(
                ds, baseline=True, metadata={"kind": "baseline"})
        return self.run_evaluation(cand, ds, baseline_report)

    def run_evaluation(self, cand: EvolutionCandidate,
                       dataset: EvalDataset,
                       baseline_report: Optional[EvalReport] = None) -> Dict[str, Any]:
        if self.evaluator is None:
            raise RuntimeError("评测器未装配：无法运行候选评测")
        cand.transition(CandidateStatus.TESTING, "进入隔离实验环境测试",
                        {"dataset": dataset.dataset_id})
        cand.transition(CandidateStatus.EVALUATING, "对比基线与候选")
        report = self.evaluator.run(dataset, baseline=False,
                                    metadata={"candidate": cand.candidate_id})
        cand.quality_result = report.summary()
        cand.cost_delta = report.cost
        cand.latency_delta = report.avg_latency
        if self.store is not None:
            self.store.save_eval_report(
                f"eval_{cand.candidate_id}_{int(time.time())}",
                dataset.dataset_id, cand.candidate_id,
                {"baseline": baseline_report.summary() if baseline_report else None,
                 "candidate": report.summary()})
        if baseline_report is not None:
            gate = self.gate.judge(baseline_report, report)
            cand.publish_suggestion = "release_ready" if gate["passed"] else "rejected"
            if not gate["passed"]:
                cand.transition(CandidateStatus.REJECTED,
                                "门禁未通过: " + "; ".join(gate["reasons"]))
            else:
                cand.transition(CandidateStatus.APPROVAL_PENDING, "门禁通过待批准")
            self._persist(cand)
            if self.bus:
                self.bus.emit(event_type="evolution_evaluated",
                              component_id="evolution",
                              payload={"candidate_id": cand.candidate_id,
                                       "passed": gate["passed"],
                                       "reasons": gate["reasons"]})
            return {**gate, "report": report.summary()}
        self._persist(cand)
        return {"passed": None, "report": report.summary()}

    def approve(self, cand: EvolutionCandidate, approver: str = "auto") -> Dict[str, Any]:
        rule = PUBLISH_RULES.get(cand.risk_level, "strict_review")
        if cand.status == CandidateStatus.APPROVAL_PENDING:
            cand.transition(CandidateStatus.RELEASE_READY, f"批准({approver}); 风险{cand.risk_level} 规则 {rule}")
            self._persist(cand)
        return {"candidate_id": cand.candidate_id, "status": cand.status.value,
                "publish_rule": rule}

    def deploy_candidate(self, cand: EvolutionCandidate, *,
                         actor: str = "auto",
                         gray_percent: int = 100,
                         gate_passed: Optional[bool] = None) -> Dict[str, Any]:
        """候选发布：必须已批准，且按 L0-L4 规则决定能否由流程发布。"""
        rule = PUBLISH_RULES.get(cand.risk_level, "strict_review")
        if cand.status != CandidateStatus.RELEASE_READY:
            return {"ok": False, "error": f"候选状态 {cand.status.value} 不可发布"}
        if cand.risk_level == "L4":
            return {"ok": False,
                    "error": "L4 核心/安全边界改动不允许普通进化流程发布"}
        if cand.risk_level in ("L2", "L3") and actor == "auto":
            return {"ok": False,
                    "error": f"{cand.risk_level} 需要具名授权发布（rule={rule}）"}
        if self.releases is None:
            return {"ok": False, "error": "发布管理器未装配"}
        gate_ok = (cand.publish_suggestion == "release_ready"
                   if gate_passed is None else bool(gate_passed))
        if not gate_ok:
            return {"ok": False, "error": "评测门禁未通过"}
        manifest = self.releases.build_manifest(
            version=f"candidate-{cand.candidate_id}",
            model_adapter_version=cand.baseline_version,
            workflow_versions=[],
            eval_dataset_version=cand.eval_dataset_id or "",
            eval_report=cand.quality_result,
            rollback_target=self.releases.current_release_id,
            compatible_requirements=cand.compatible_requirements)
        out = self.releases.deploy(manifest.release_id, gate_passed=True,
                                   scope="critical" if cand.risk_level == "L4"
                                   else "normal",
                                   gray_percent=gray_percent)
        if not out.get("ok"):
            return out
        cand.transition(CandidateStatus.DEPLOYING,
                        "签名制品生成，受控开始部署",
                        {"release_id": manifest.release_id,
                         "gray_percent": gray_percent, "actor": actor})
        cand.transition(CandidateStatus.MONITORING,
                        "进入发布后监测（PRD 82）")
        self._persist(cand)
        if self.bus:
            self.bus.emit(event_type="evolution_deployed",
                          component_id="evolution",
                          payload={"candidate_id": cand.candidate_id,
                                   "release_id": manifest.release_id,
                                   "gray_percent": gray_percent})
        return {"ok": True, "candidate_id": cand.candidate_id,
                "release_id": manifest.release_id,
                "status": cand.status.value, "gray_percent": gray_percent}

    def monitor(self, cand: EvolutionCandidate, metrics: Dict[str, Any],
                thresholds: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """发布后监测：达标晋级，越界回滚（PRD 82）。"""
        th = {"min_success_rate": 0.8, "max_error_rate": 0.15,
              "max_cost_ratio": 1.5}
        th.update(thresholds or {})
        reasons: List[str] = []
        if metrics.get("safety_incident"):
            reasons.append("安全事件")
        if float(metrics.get("success_rate", 1.0)) < th["min_success_rate"]:
            reasons.append(f"成功率过低: {metrics.get('success_rate')}")
        if float(metrics.get("error_rate", 0.0)) > th["max_error_rate"]:
            reasons.append(f"错误率过高: {metrics.get('error_rate')}")
        if float(metrics.get("cost_ratio", 1.0)) > th["max_cost_ratio"]:
            reasons.append(f"成本增幅过大: {metrics.get('cost_ratio')}")
        if reasons:
            reason = "; ".join(reasons)
            self.rollback(cand, reason)
            out = self.releases.rollback(reason) if self.releases else {"ok": False}
            return {"decision": "rollback", "reasons": reasons,
                    "candidate": cand.to_dict(), "release": out}
        self.promote(cand, metrics)
        return {"decision": "promoted", "reasons": [],
                "candidate": cand.to_dict()}

    def mark_deploying(self, cand: EvolutionCandidate) -> None:
        cand.transition(CandidateStatus.DEPLOYING, "签名制品生成，受控开始部署")
        self._persist(cand)

    def mark_monitoring(self, cand: EvolutionCandidate) -> None:
        cand.transition(CandidateStatus.MONITORING, "进入发布后监测（PRD 82）")
        self._persist(cand)

    def promote(self, cand: EvolutionCandidate, metrics: Dict[str, Any]) -> None:
        cand.transition(CandidateStatus.PROMOTED, "监测达标晋级",
                        {"metrics": metrics})
        cand.final_result = "PROMOTED"
        self._persist(cand)

    def rollback(self, cand: EvolutionCandidate, reason: str) -> None:
        cand.transition(CandidateStatus.ROLLED_BACK, "指标越过阈值或安全事件: " + reason)
        cand.final_result = f"ROLLED_BACK -> {cand.rollback_version}"
        self._persist(cand)

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
