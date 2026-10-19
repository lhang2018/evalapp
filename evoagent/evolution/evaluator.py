"""自动评测系统（PRD 14 / Architecture 11）。

五层评测：1) 单元与契约 2) 集成 3) 能力级 4) 端到端任务 5) 真实运行监测。
本模块实现：评测集管理、候选任务运行、基线对比、门禁指标与报告。
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


@dataclass
class EvalDataset:
    dataset_id: str
    version: str = "1.0.0"
    task_category: str = "general"
    source: str = "runtime"
    privacy: str = "standard"
    cases: List[EvalCase] = field(default_factory=list)


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

    def summary(self) -> Dict[str, Any]:
        return {"dataset": f"{self.dataset_id}@{self.version}",
                "baseline": self.baseline, "success_rate": round(self.success_rate, 3),
                "cost": round(self.cost, 4), "avg_latency": round(self.avg_latency, 3),
                "metadata": self.metadata}


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
        report = EvalReport(dataset.dataset_id, dataset.version, baseline=baseline,
                            metadata=dict(metadata or {}))
        latencies: List[float] = []
        for case in dataset.cases:
            ok, latency, evidence = self.runner.run_case(case)
            report.case_results.append({"case": case.case_id, "ok": ok,
                                        "difficulty": case.difficulty,
                                        **evidence})
            latencies.append(latency)
            report.cost += evidence.get("cost", 0.0)
        report.success_rate = (sum(1 for c in report.case_results if c["ok"])
                               / len(dataset.cases)) if dataset.cases else 0.0
        report.avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        return report


@dataclass
class GateThresholds:
    min_success_rate: float = 0.8
    max_cost_increase_ratio: float = 1.5      # 相对基线
    max_latency_increase_ratio: float = 1.5
    require_safety_pass: bool = True
    require_rollback_plan: bool = True


class ReleaseGate:
    """门禁（PRD 54）：候选至少满足的条件。关键安全失败不可用平均分抵消（Arch 11.5）。"""

    def judge(self, baseline: EvalReport, candidate: EvalReport,
              thresholds: Optional[GateThresholds] = None,
              safety_ok: bool = True,
              rollback_available: bool = True) -> Dict[str, Any]:
        th = thresholds or GateThresholds()
        reasons: List[str] = []
        sr_ok = candidate.success_rate >= th.min_success_rate
        if not sr_ok:
            reasons.append(f"成功率不足: {candidate.summary()['success_rate']}"
                           f" < {th.min_success_rate}")
        if baseline.success_rate > 0:
            dr = candidate.success_rate - baseline.success_rate
            if dr < -0.1:
                reasons.append(f"重要任务退化: {round(dr, 3)}")
        if baseline.cost > 0:
            cost_ratio = candidate.cost / baseline.cost
            if cost_ratio > th.max_cost_increase_ratio:
                reasons.append(f"成本增幅过大: x{round(cost_ratio, 2)}")
        if baseline.avg_latency > 0:
            lat_ratio = candidate.avg_latency / baseline.avg_latency
            if lat_ratio > th.max_latency_increase_ratio:
                reasons.append(f"时延增幅过大: x{round(lat_ratio, 2)}")
        if th.require_safety_pass and not safety_ok:
            # 安全测试失败 → 一票否决，不允许用总体平均分抵消
            reasons.append("关键安全测试未通过（一票否决）")
        if th.require_rollback_plan and not rollback_available:
            reasons.append("缺少回滚方案")
        passed = not reasons
        return {"passed": passed, "reasons": reasons,
                "baseline": baseline.summary(), "candidate": candidate.summary()}
