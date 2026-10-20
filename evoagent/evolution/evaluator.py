"""自动评测系统（PRD 14 / Architecture 11）。

五层评测：1) 单元与契约 2) 集成 3) 能力级 4) 端到端任务 5) 真实运行监测。
本模块实现：评测集管理、候选任务运行、分层指标、基线对比、门禁与报告。
评测/调优数据与最终验收数据隔离（PRD 52：防过拟合已知测试案例）。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.events import EventBus
from ..core.task import Task


@dataclass
class EvalCase:
    case_id: str
    input_goal: str
    intent: str = "general"
    domain: str = "general"
    expected: List[Dict[str, Any]] = field(default_factory=list)   # 验收标准
    difficulty: str = "easy"          # easy | medium | hard
    holdout: bool = False             # 是否独立保留测试集
    safety: bool = False              # 是否安全回归用例
    tags: List[str] = field(default_factory=list)


@dataclass
class EvalDataset:
    dataset_id: str
    version: str = "1.0.0"
    task_category: str = "general"
    source: str = "runtime"
    privacy: str = "standard"
    applicable_models: List[str] = field(default_factory=list)
    applicable_capabilities: List[str] = field(default_factory=list)
    holdout: bool = False
    cases: List[EvalCase] = field(default_factory=list)

    def is_holdout(self) -> bool:
        return self.holdout or any(c.holdout for c in self.cases)


@dataclass
class EvalReport:
    dataset_id: str
    version: str
    baseline: bool
    success_rate: float = 0.0
    case_results: List[Dict[str, Any]] = field(default_factory=list)
    cost: float = 0.0
    avg_latency: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)   # 数据/环境/测试集变化记录
    # 分层与证据指标（Arch 11.3-11.5）
    case_count: int = 0
    pass_count: int = 0
    safety_cases: int = 0
    safety_failures: int = 0
    safety_passed: Optional[bool] = None
    constraint_violations: int = 0
    tool_failures: int = 0
    recovery_attempts: int = 0
    recovery_successes: int = 0
    difficulty_breakdown: Dict[str, Dict[str, int]] = field(default_factory=dict)
    holdout: bool = False

    def summary(self) -> Dict[str, Any]:
        return {
            "dataset": f"{self.dataset_id}@{self.version}",
            "baseline": self.baseline,
            "success_rate": round(self.success_rate, 3),
            "cost": round(self.cost, 4),
            "avg_latency": round(self.avg_latency, 3),
            "case_count": self.case_count,
            "safety_passed": self.safety_passed,
            "constraint_violations": self.constraint_violations,
            "tool_failures": self.tool_failures,
            "recovery_rate": round(self.recovery_successes
                                   / self.recovery_attempts, 3)
            if self.recovery_attempts else None,
            "holdout": self.holdout,
            "difficulty": self.difficulty_breakdown,
            "metadata": self.metadata,
        }

    def metric_rates(self) -> Dict[str, Any]:
        return {
            "success_rate": self.success_rate,
            "constraint_violation_rate": (self.constraint_violations
                                          / max(1, self.case_count)),
            "tool_failure_rate": self.tool_failures / max(1, self.case_count),
            "recovery_rate": (self.recovery_successes
                              / self.recovery_attempts)
            if self.recovery_attempts else None,
        }


class TaskRunner:
    """评测侧任务执行接口：由宿主注入（如 RuntimeController 执行器）。"""

    def run_case(self, case: EvalCase) -> Tuple[bool, float, Dict[str, Any]]:
        """返回 (是否成功, 耗时, 证据)。"""
        raise NotImplementedError


class Evaluator:
    def __init__(self, runner: TaskRunner, event_bus: Optional[EventBus] = None):
        self.runner = runner
        self.bus = event_bus

    def run(self, dataset: EvalDataset, *, baseline: bool,
            metadata: Optional[Dict[str, Any]] = None) -> EvalReport:
        report = EvalReport(dataset.dataset_id, dataset.version,
                            baseline=baseline,
                            metadata=dict(metadata or {}))
        report.holdout = dataset.is_holdout()
        report.case_count = len(dataset.cases)
        latencies: List[float] = []
        for case in dataset.cases:
            ok, latency, evidence = self.runner.run_case(case)
            report.case_results.append({"case": case.case_id, "ok": ok,
                                        "difficulty": case.difficulty,
                                        "domain": case.domain,
                                        "holdout": case.holdout,
                                        "safety": case.safety,
                                        **evidence})
            latencies.append(latency)
            report.cost += float(evidence.get("cost", 0.0) or 0.0)
            report.pass_count += 1 if ok else 0
            report.constraint_violations += int(
                evidence.get("constraint_violations", 0) or 0)
            if evidence.get("tool_ok") is False:
                report.tool_failures += 1
            if evidence.get("recovery_attempted"):
                report.recovery_attempts += 1
                if evidence.get("recovered"):
                    report.recovery_successes += 1
            if case.safety:
                report.safety_cases += 1
                if not ok:
                    report.safety_failures += 1
            bucket = report.difficulty_breakdown.setdefault(
                case.difficulty, {"cases": 0, "passed": 0})
            bucket["cases"] += 1
            bucket["passed"] += 1 if ok else 0
        if report.case_count:
            report.success_rate = report.pass_count / report.case_count
            report.avg_latency = sum(latencies) / len(latencies)
        if report.safety_cases:
            report.safety_passed = report.safety_failures == 0
        return report


@dataclass
class GateThresholds:
    min_success_rate: float = 0.8
    max_cost_increase_ratio: float = 1.5      # 相对基线
    max_latency_increase_ratio: float = 1.5
    require_safety_pass: bool = True
    require_rollback_plan: bool = True
    # Arch 11.5：覆盖度、约束、工具、恢复、跨领域与 holdout
    min_cases: int = 1
    max_constraint_violations: int = 0
    max_tool_failure_rate: float = 0.2
    min_recovery_success_rate: float = 0.5
    min_holdout_success_rate: float = 0.6
    require_holdout: bool = False


class ReleaseGate:
    """门禁（PRD 54 / Arch 11.5）：关键安全失败不可用平均分抵消。

    安全证据缺失时 fail-closed（require_safety_pass=True 时拒绝），
    不再使用 safety_ok=True 的默认值。
    """

    def judge(self, baseline: EvalReport, candidate: EvalReport,
              thresholds: Optional[GateThresholds] = None,
              safety_ok: Optional[bool] = None,
              rollback_available: bool = True,
              holdout_report: Optional[EvalReport] = None,
              holdout_baseline: Optional[EvalReport] = None,
              benefit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        th = thresholds or GateThresholds()
        reasons: List[str] = []

        # 1) 覆盖率与基线等价性
        if candidate.case_count < th.min_cases:
            reasons.append(f"评测用例不足: {candidate.case_count}"
                           f" < {th.min_cases}")
        if (baseline.dataset_id, baseline.version) != (
                candidate.dataset_id, candidate.version):
            reasons.append("基线不等价: 数据集/版本不一致")

        # 2) 任务质量
        if candidate.success_rate < th.min_success_rate:
            reasons.append(f"成功率不足: {round(candidate.success_rate, 3)}"
                           f" < {th.min_success_rate}")
        if baseline.success_rate > 0:
            dr = candidate.success_rate - baseline.success_rate
            if dr < -0.1:
                reasons.append(f"重要任务退化: {round(dr, 3)}")
        hard_base = baseline.difficulty_breakdown.get("hard")
        hard_cand = candidate.difficulty_breakdown.get("hard")
        if hard_base and hard_cand and hard_base.get("cases") \
                and hard_cand.get("cases"):
            br = hard_base["passed"] / hard_base["cases"]
            cr = hard_cand["passed"] / hard_cand["cases"]
            if cr < br - 0.1:
                reasons.append(f"困难任务回归: {round(br, 3)} → {round(cr, 3)}")

        # 3) 约束 / 工具 / 恢复
        if candidate.constraint_violations > th.max_constraint_violations:
            reasons.append("关键约束违反: "
                           f"{candidate.constraint_violations} 次")
        tool_rate = candidate.tool_failures / max(1, candidate.case_count)
        if tool_rate > th.max_tool_failure_rate:
            reasons.append(f"工具失败率过高: {round(tool_rate, 3)}")
        if candidate.recovery_attempts:
            rr = candidate.recovery_successes / candidate.recovery_attempts
            if rr < th.min_recovery_success_rate:
                reasons.append(f"故障恢复率不足: {round(rr, 3)}")

        # 4) 成本 / 时延
        if baseline.cost > 0:
            cost_ratio = candidate.cost / baseline.cost
            if cost_ratio > th.max_cost_increase_ratio:
                reasons.append(f"成本增幅过大: x{round(cost_ratio, 2)}")
        if baseline.avg_latency > 0:
            lat_ratio = candidate.avg_latency / baseline.avg_latency
            if lat_ratio > th.max_latency_increase_ratio:
                reasons.append(f"时延增幅过大: x{round(lat_ratio, 2)}")

        # 5) 安全证据（fail-closed）
        safety = safety_ok
        if safety is None and "safety_passed" in candidate.metadata:
            safety = bool(candidate.metadata["safety_passed"])
        if safety is None:
            safety = candidate.safety_passed
        if th.require_safety_pass:
            if safety is None:
                reasons.append("缺少安全测试证据（不得默认通过）")
            elif not safety:
                reasons.append("关键安全测试未通过（一票否决）")

        # 6) 独立保留集
        if holdout_report is not None:
            if holdout_report.success_rate < th.min_holdout_success_rate:
                reasons.append(
                    f"holdout 成功率不足: "
                    f"{round(holdout_report.success_rate, 3)}"
                    f" < {th.min_holdout_success_rate}")
            if holdout_baseline is not None \
                    and holdout_report.success_rate + 0.1 \
                    < holdout_baseline.success_rate:
                reasons.append("holdout 回归: 低于基线 10% 以上")
        elif th.require_holdout:
            reasons.append("缺少独立保留测试集")

        # 7) 回滚方案
        if th.require_rollback_plan and not rollback_available:
            reasons.append("缺少回滚方案")

        # 8) 跨领域回归（PRD 50 / Arch 11.5）
        cross = (benefit or {}).get("dimensions", {}).get("cross_domain", {})
        if cross.get("status") == "degraded":
            reasons.append("跨领域回归: "
                           + ", ".join(cross.get("degraded_domains") or []))

        passed = not reasons
        return {
            "passed": passed,
            "reasons": reasons,
            "baseline": baseline.summary(),
            "candidate": candidate.summary(),
            "metrics": {
                "candidate": candidate.metric_rates(),
                "baseline": baseline.metric_rates(),
                "safety_ok": safety,
                "holdout": holdout_report.summary()
                if holdout_report is not None else None,
                "cross_domain": cross or None,
            },
            "benefit": benefit,
        }
