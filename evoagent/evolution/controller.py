"""进化控制器（PRD 12-13 / 46-50 / Architecture 10）。

职责：问题优先级排序、影响范围分析、根因假设管理、优化方法选择、
候选任务生成、实验分配、评测调度、收益分析、风险分级、发布资格、回滚触发。
进化控制器本身也在版本管理和评测范围内（Arch 22.4）。
"""

import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.budget import BudgetController, BudgetExceeded, BudgetLimits
from ..core.events import EventBus
from ..tools.registry import ToolRegistry
from .benefit import evaluate_benefit
from .candidates import (ALLOWED, CandidateStatus, EvolutionCandidate,
                         IllegalCandidateTransition, TERMINAL as
                         TERMINAL_CANDIDATE_STATES)
from .evaluator import (EvalDataset, Evaluator, EvalReport, GateThresholds,
                        ReleaseGate)
from ..core import contracts as _contracts
from .monitor import MonitorPolicy, PostReleaseMonitor
from .regression import FailureRegressionBuilder
from .safety import SafetySuite

# PRD 80 风险分级 L0-L4
RISK_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}

# E-13 轮次审计登记簿行类别（ALN-09；contracts.py 在途占用，集成时并入）
EVOLUTION_ROUND_AUDIT_KIND = "evolution_round"

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
    # E-07（GAP 第二批第 7 项）：signal 型票据四字段——由
    # evolution/signals.py 从四类信号自动生成，经 run_signal_pipeline
    # 自动转候选；无 signal_type 的手工票据只入观察队列，不自动提案
    signal_type: str = ""           # contracts.EVOLUTION_SIGNAL_TYPES
    target_component: str = ""      # expert/<id> | tool/<id> | router | budget
    hypothesis: str = ""            # 可反驳假设（候选 hypothesis 来源）
    change_scope: str = "local"     # local | budget_policy | experts
    candidate_id: Optional[str] = None   # 消费后回填
    consumed: bool = False


class EvolutionController:
    def __init__(self, evaluator: Optional[Evaluator] = None,
                 gate: Optional[ReleaseGate] = None,
                 budget: Optional[Any] = None,
                 event_bus: Optional[EventBus] = None,
                 store: Optional[Any] = None,
                 releases: Optional[Any] = None,
                 datasets: Optional[Dict[str, EvalDataset]] = None,
                 budget_config: Optional[Any] = None,
                 experts: Optional[Any] = None,
                 max_concurrent_evaluations: Optional[int] = None):
        self.evaluator = evaluator
        self.gate = gate or ReleaseGate()
        # 生产路径要求安全证据与独立保留集（Arch 11.2 / 11.5）
        self.gate_thresholds = GateThresholds(require_safety_pass=True,
                                              require_holdout=True)
        self.safety = SafetySuite()
        self.budget = budget
        self.budget_config = budget_config
        self.bus = event_bus
        self.store = store
        self.releases = releases
        self.datasets: Dict[str, EvalDataset] = dict(datasets or {})
        # 能力依赖图来源（GAP 子步④：影响分析进候选）
        self.experts = experts
        self.holdout_dataset: Optional[EvalDataset] = self._find_holdout()
        self.candidates: Dict[str, EvolutionCandidate] = {}
        self.tickets: deque = deque()
        self._seq = 0
        # E-13（GAP 第二批第 9 项）：并发评测上限；「进行中评测」=
        # TESTING/EVALUATING 状态候选（04 卷 E-13 实验编排）
        self.max_concurrent_evaluations = max(1, int(
            max_concurrent_evaluations
            or _contracts.EVOLUTION_MAX_CONCURRENT_EVALUATIONS))
        # E-12（GAP 第二批第 8 项）：失败案例 → 回归资产转换器
        self.regression_builder = FailureRegressionBuilder(self)
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
        self.holdout_dataset = self._find_holdout()

    def _find_holdout(self) -> Optional[EvalDataset]:
        return next((ds for ds in self.datasets.values() if ds.is_holdout()),
                    None)

    # ---- 进化预算池（PRD 55-56 / Arch 12.4）----
    def _evolution_pool_problem(self) -> Optional[str]:
        """日/月进化预算池与正式任务隔离；返回耗尽原因或 None。"""
        if self.budget is None or self.budget_config is None:
            return None
        cfg = self.budget_config
        try:
            self.budget.check(
                "evolution",
                max_model_calls=cfg.evolution_daily_model_calls,
                max_cost=cfg.evolution_daily_cost, period="day")
            self.budget.check(
                "evolution",
                max_model_calls=cfg.evolution_monthly_model_calls,
                max_cost=cfg.evolution_monthly_cost, period="month")
        except BudgetExceeded as e:
            return str(e)
        return None

    def _make_candidate_budget(self, cand: EvolutionCandidate
                               ) -> Optional[BudgetController]:
        """单候选实验预算；用量同时计入进化日/月池。"""
        if self.budget is None:
            return None
        cfg = self.budget_config
        limits = BudgetLimits()
        if cfg is not None:
            limits = BudgetLimits(
                max_model_calls=cfg.candidate_max_model_calls,
                max_tool_calls=cfg.candidate_max_tool_calls,
                max_cost=cfg.candidate_max_cost,
                max_seconds=cfg.candidate_max_seconds)
        return BudgetController(
            limits, ledger=self.budget,
            ledger_scope=f"candidate:{cand.candidate_id}",
            extra_scopes=["evolution"])

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

    # ---- E-07 消费链：问题票据 → 自动候选提案（GAP 第二批第 7 项）----
    def _baseline_version(self) -> str:
        """候选基线=当前稳定版本：来自发布清单，无发布时 unreleased。"""
        if self.releases and getattr(self.releases, "current_release_id", None):
            m = self.releases.manifests.get(self.releases.current_release_id)
            if m is not None:
                return m.version
        return "unreleased"

    def _has_open_candidate_for(self, target_component: str) -> bool:
        """组件级节流：该组件已有未决候选（未到终态）时不再自动提案。"""
        for cand in self.candidates.values():
            if (cand.target_component == target_component
                    and cand.status not in TERMINAL_CANDIDATE_STATES):
                return True
        return False

    def run_signal_pipeline(self, max_candidates: int = 1) -> List[str]:
        """消费 signal 型问题票据 → 自动 propose_candidate。

        - 只消费 signal 型票据（含 signal_type）；手工票据等待人工。
        - 组件级节流：同组件已有未决候选则跳过（候选状态本身就是
          证据在途，04 卷 E-07 设计决策）。
        - 提案成功即回填 ticket.candidate_id 并消费出队；失败（预算/
          校验）保留票据待下一轮——证据不足保持候选状态，不丢弃证据。
        """
        proposed: List[str] = []
        for ticket in list(self.tickets):
            if len(proposed) >= max(1, int(max_candidates)):
                break
            if not ticket.signal_type or ticket.consumed:
                continue
            if self._has_open_candidate_for(ticket.target_component):
                continue
            try:
                cand = self.propose_candidate(
                    self._baseline_version(),
                    ticket.target_component,
                    ticket.description,
                    ticket.hypothesis or ticket.description,
                    change_scope=ticket.change_scope or "local",
                    evidence=list(ticket.evidence),
                )
            except Exception as e:  # noqa: BLE001 —— 票据保留不丢证据
                if self.bus:
                    self.bus.emit(
                        event_type="evolution_signal_skip",
                        component_id="evolution",
                        payload={"ticket_id": ticket.ticket_id,
                                 "error": f"{type(e).__name__}: {e}"})
                ticket.consumed = False
                continue
            ticket.consumed = True
            ticket.candidate_id = cand.candidate_id
            proposed.append(cand.candidate_id)
        self.tickets = deque(
            t for t in self.tickets if not (t.signal_type and t.consumed))
        return proposed

    # ---- E-13 实验编排轮（队列 / 预算 / 并发 / 停止条件）--------------
    def run_evolution_round(self, max_evaluations: int = 1) -> Dict[str, Any]:
        """一轮进化实验编排：失败固化 → 信号提案 → 受评测。

        不拥有发布权：评测与门禁决策交给 evaluator/gate；停止条件
        （contracts EVOLUTION_STOP_*）只停止本轮，绝不静默丢证据。
        """
        report: Dict[str, Any] = {
            "converted": [], "skipped_unknown": [], "already_converted": 0,
            "proposed": [], "evaluated": [], "errors": [],
            "stop_reasons": [],
        }
        # 停止条件①：实验预算池耗尽（日/月池与正式任务隔离）
        problem = self._evolution_pool_problem()
        if problem:
            report["stop_reasons"].append(
                f"{_contracts.EVOLUTION_STOP_BUDGET_EXHAUSTED}: {problem}")
            self._audit_round(report)
            return report
        # E-12：失败案例先固化（转换在提案之前，证据先行）
        converted = self.regression_builder.convert_pending()
        report["converted"] = converted.get("converted", [])
        report["skipped_unknown"] = converted.get("skipped_unknown", [])
        report["already_converted"] = converted.get("already", 0)
        # 提案（无未决证据 → 记停止原因）
        report["proposed"] = self.run_signal_pipeline(max_candidates=1)
        if not report["proposed"]:
            report["stop_reasons"].append(
                _contracts.EVOLUTION_STOP_NO_PENDING_EVIDENCE)
        # E-13 故障恢复（ALN-09）：崩溃残留 TESTING/EVALUATING 的候选是
        # 死锁（不再有进程持有评测），必须先正向续跑（run_evaluation 从
        # 当前态接续），否则永远不会前进——恢复不受并发占位阻断，受
        # 轮额度约束
        stuck = [c for c in self.candidates.values()
                 if c.status in (CandidateStatus.TESTING,
                                 CandidateStatus.EVALUATING)]
        for cand in stuck[:max(1, int(max_evaluations))]:
            later_problem = self._evolution_pool_problem()
            if later_problem:
                report["stop_reasons"].append(
                    f"{_contracts.EVOLUTION_STOP_BUDGET_EXHAUSTED}: "
                    f"{later_problem}")
                self._audit_round(report)
                return report
            try:
                self.evaluate_candidate(cand.candidate_id)
                report["evaluated"].append(cand.candidate_id)
            except Exception as e:  # noqa: BLE001 —— 记因则止，不静默
                report["errors"].append(
                    f"{cand.candidate_id}: {type(e).__name__}: {e}")
                report["stop_reasons"].append(
                    _contracts.EVOLUTION_STOP_EVALUATION_ERROR)
                self._audit_round(report)
                return report
        # 停止条件②：并发上限（恢复后的存活进行中计数；新评测受其约束）
        in_flight = [c for c in self.candidates.values()
                     if c.status in (CandidateStatus.TESTING,
                                     CandidateStatus.EVALUATING)]
        slots = self.max_concurrent_evaluations - len(in_flight)
        if slots <= 0:
            report["stop_reasons"].append(
                _contracts.EVOLUTION_STOP_CONCURRENCY_LIMIT)
            self._audit_round(report)
            return report
        # 评测就绪候选（CANDIDATE_CREATED），受并发余量与轮剩余额度约束
        ready = [c for c in self.candidates.values()
                 if c.status == CandidateStatus.CANDIDATE_CREATED]
        remaining = max(0, max(1, int(max_evaluations))
                        - len(report["evaluated"]))
        for cand in ready[:min(slots, remaining)]:
            # 每次评测前重查预算池（评测本身消耗预算）
            later_problem = self._evolution_pool_problem()
            if later_problem:
                report["stop_reasons"].append(
                    f"{_contracts.EVOLUTION_STOP_BUDGET_EXHAUSTED}: "
                    f"{later_problem}")
                self._audit_round(report)
                break
            try:
                self.evaluate_candidate(cand.candidate_id)
                report["evaluated"].append(cand.candidate_id)
            except Exception as e:  # noqa: BLE001 —— 记因则止，不静默
                report["errors"].append(
                    f"{cand.candidate_id}: {type(e).__name__}: {e}")
                report["stop_reasons"].append(
                    _contracts.EVOLUTION_STOP_EVALUATION_ERROR)
                self._audit_round(report)
                break
        self._audit_round(report)
        return report

    # ---- E-13 轮次审计与恢复证据（ALN-09）---------------------------
    def _audit_round(self, report: Dict[str, Any]) -> None:
        """编排轮结果落证据登记簿（即故障恢复的接续依据；故障不阻断）。"""
        store = getattr(self, "store", None)
        if store is None or not hasattr(store, "save_evidence"):
            return
        try:
            payload = {
                "converted": report.get("converted", []),
                "proposed": report.get("proposed", []),
                "evaluated": report.get("evaluated", []),
                "errors": report.get("errors", []),
                "stop_reasons": report.get("stop_reasons", []),
            }
            store.save_evidence(
                evidence_id=f"round_{uuid.uuid4().hex[:12]}",
                task_id=None,
                kind=EVOLUTION_ROUND_AUDIT_KIND,
                ref="evolution_round",
                summary=json.dumps(payload, ensure_ascii=False,
                                   sort_keys=True),
            )
        except Exception:  # noqa: BLE001
            pass

    def _next_candidate_id(self) -> str:
        self._seq += 1
        return f"cand_{self._seq:03d}"

    def _impact_for(self, target_component: str) -> Optional[Dict[str, Any]]:
        """候选创建时的能力影响分析（GAP 子步④真实调用方①）。

        target_component 可为 registered capability_id 或组件名；无依赖图
        或查询故障时返回 None，绝不阻断候选创建。
        """
        if self.experts is None:
            return None
        try:
            result = self.experts.impact_analysis(target_component)
            return result if isinstance(result, dict) else None
        except Exception:  # noqa: BLE001 —— 影响分析故障不阻断候选流程
            return None

    # ---- 候选生命周期 ----
    def propose_candidate(self, baseline_version: str, target_component: str,
                          target_problem: str, hypothesis: str,
                          change_scope: str = "local",
                          evidence: Optional[List[str]] = None,
                          target_domain: str = "") -> EvolutionCandidate:
        cand = EvolutionCandidate(
            candidate_id=self._next_candidate_id(),
            baseline_version=baseline_version,
            target_component=target_component,
            target_problem=target_problem,
            hypothesis=hypothesis,
            change_scope=change_scope,
            root_cause_evidence=list(evidence or []),
            target_domain=target_domain,
            # PRD 50：候选必须有明确回滚版本
            rollback_version=baseline_version,
            # GAP 子步④：候选创建即落影响分析传递闭包（故障不阻断）
            impact_result=self._impact_for(target_component),
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

    def _pick_dataset(self, cand: Any,
                      dataset_id: str = "") -> "Optional[EvalDataset]":
        """评测集选择（GAP 子步④：影响闭包定回归范围）。

        显式 dataset_id / 候选自带 dataset 优先；未指定时，若候选的
        影响闭包触及 shared 共享内核 → 选 builtin_holdout（更全的
        领域回归）；否则维持原口径（首可用数据集 builtin_smoke）。
        """
        ds = self.datasets.get(dataset_id
                               or getattr(cand, "eval_dataset_id", ""))
        if ds is None and (getattr(cand, "impact_result", None) or {}).get(
                "affected_closure") and "builtin_holdout" in self.datasets:
            ds = self.datasets["builtin_holdout"]
        return ds or next(iter(self.datasets.values()), None)

    def evaluate_candidate(self, candidate_id: str,
                           dataset_id: Optional[str] = None,
                           baseline: bool = True) -> Dict[str, Any]:
        """完整评测路径：基线 → 候选 → 门禁 → 报告落盘（PRD 51-54）。"""
        cand = self.candidates.get(candidate_id)
        if cand is None:
            raise KeyError(candidate_id)
        if self.evaluator is None:
            raise RuntimeError("评测器未装配：无法运行候选评测")
        ds = self._pick_dataset(cand, dataset_id)
        if ds is None:
            raise ValueError("没有可用评测集")
        cand.eval_dataset_id = ds.dataset_id
        # GAP ⑤c：评测套件引用传入执行链（run_case → manifest_extras）
        _runner = getattr(self.evaluator, "runner", None)
        if _runner is not None and hasattr(_runner, "suite_ref"):
            _runner.suite_ref = f"{ds.dataset_id}/{ds.version}"
        baseline_report = None
        if baseline:
            baseline_report = self.evaluator.run(
                ds, baseline=True, metadata={"kind": "baseline"})
        return self.run_evaluation(cand, ds, baseline_report)

    def run_evaluation(self, cand: EvolutionCandidate,
                       dataset: EvalDataset,
                       baseline_report: Optional[EvalReport] = None
                       ) -> Dict[str, Any]:
        if self.evaluator is None:
            raise RuntimeError("评测器未装配：无法运行候选评测")
        # E-13 故障恢复（ALN-09）：崩溃残留 TESTING/EVALUATING 的候选从
        # 当前态正向续跑（状态机不允许倒退，故按现状跳过已完成转移）
        if cand.status not in (CandidateStatus.TESTING,
                               CandidateStatus.EVALUATING):
            cand.transition(CandidateStatus.TESTING, "进入隔离实验环境测试",
                            {"dataset": dataset.dataset_id})
        if cand.status == CandidateStatus.TESTING:
            cand.transition(CandidateStatus.EVALUATING, "对比基线与候选")

        # 进化预算池与正式任务隔离（PRD 55-56 / Arch 12.4）
        pool_problem = self._evolution_pool_problem()
        if pool_problem:
            reason = f"进化预算耗尽: {pool_problem}"
            cand.transition(CandidateStatus.REJECTED, reason)
            self._persist(cand)
            if self.bus:
                self.bus.emit(event_type="evolution_evaluated",
                              component_id="evolution",
                              payload={"candidate_id": cand.candidate_id,
                                       "passed": False,
                                       "reasons": [reason]})
            return {"passed": False, "reasons": [reason],
                    "budget_exhausted": pool_problem, "safety": {}}

        runner = getattr(self.evaluator, "runner", None)
        candidate_budget = self._make_candidate_budget(cand)
        if candidate_budget is not None and hasattr(runner, "set_budget"):
            runner.set_budget(candidate_budget)
        try:
            # 安全证据由确定性套件给出，缺失时门禁 fail-closed（Arch 11.5）
            safety = self.safety.run()
            cand.safety_result = safety

            report = self.evaluator.run(
                dataset, baseline=False,
                metadata={"candidate": cand.candidate_id,
                          "dataset_version": dataset.version})
            holdout_report: Optional[EvalReport] = None
            holdout_baseline: Optional[EvalReport] = None
            if dataset.is_holdout():
                holdout_report, holdout_baseline = report, baseline_report
            elif self.holdout_dataset is not None:
                # 独立保留集只用于门禁，不参与调优（PRD 52 / Arch 11.2）
                if baseline_report is not None:
                    holdout_baseline = self.evaluator.run(
                        self.holdout_dataset, baseline=True,
                        metadata={"kind": "holdout_baseline"})
                holdout_report = self.evaluator.run(
                    self.holdout_dataset, baseline=False,
                    metadata={"candidate": cand.candidate_id,
                              "kind": "holdout"})
                cand.holdout_result = holdout_report.summary()
        finally:
            if candidate_budget is not None and hasattr(runner, "set_budget"):
                runner.set_budget(None)

        budget_problem = (candidate_budget.exceeded()
                          if candidate_budget is not None else None)
        experiment_budget = (candidate_budget.snapshot()
                             if candidate_budget is not None else None)
        cand.quality_result = report.summary()
        # PRD 50：收益按相对基线的增量记录，而不是候选绝对值
        base_cost = baseline_report.cost if baseline_report is not None else 0.0
        base_latency = (baseline_report.avg_latency
                        if baseline_report is not None else 0.0)
        cand.cost_delta = report.cost - base_cost
        cand.latency_delta = report.avg_latency - base_latency
        benefit = evaluate_benefit(
            baseline_report, report,
            change_scope=cand.change_scope, risk_level=cand.risk_level,
            safety=safety, monitor=cand.monitor_result,
            target_domain=cand.target_domain)
        cand.benefit_result = benefit
        if self.store is not None:
            self.store.save_eval_report(
                f"eval_{cand.candidate_id}_{int(time.time())}",
                dataset.dataset_id, cand.candidate_id,
                {"baseline": baseline_report.summary() if baseline_report else None,
                 "candidate": report.summary(),
                 "holdout": holdout_report.summary()
                 if holdout_report is not None else None,
                 "safety": safety,
                 "benefit": benefit,
                 "experiment_budget": experiment_budget})
        if baseline_report is not None:
            gate = self.gate.judge(
                baseline_report, report,
                thresholds=self.gate_thresholds,
                safety_ok=safety["passed"],
                rollback_available=bool(cand.rollback_version),
                holdout_report=holdout_report,
                holdout_baseline=holdout_baseline,
                benefit=benefit)
            if budget_problem:
                gate["passed"] = False
                gate["reasons"] = list(gate.get("reasons") or []) + [
                    f"候选实验预算耗尽: {budget_problem}"]
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
                                       "reasons": gate["reasons"],
                                       "safety_passed": safety["passed"],
                                       "holdout": holdout_report.summary()
                                       if holdout_report is not None else None})
            return {**gate, "report": report.summary(),
                    "holdout": holdout_report.summary()
                    if holdout_report is not None else None,
                    "safety": safety, "benefit": benefit,
                    "experiment_budget": experiment_budget}
        self._persist(cand)
        return {"passed": None, "report": report.summary(), "safety": safety,
                "benefit": benefit,
                "experiment_budget": experiment_budget}

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
        cand.deployed_at = time.time()
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

    def monitor(self, cand: EvolutionCandidate,
                metrics: Optional[Dict[str, Any]] = None,
                thresholds: Optional[Dict[str, float]] = None,
                policy: Optional[MonitorPolicy] = None) -> Dict[str, Any]:
        """发布后监测（PRD 82）：自动采集或接受手工指标，按窗口判定。"""
        if cand.status != CandidateStatus.MONITORING:
            return {"decision": "not_monitoring",
                    "status": cand.status.value,
                    "candidate_id": cand.candidate_id}
        pol = (policy or MonitorPolicy()).merged(thresholds)
        monitor = PostReleaseMonitor(self.store, self.bus, pol)
        if metrics:
            metrics = dict(metrics)
            # 手工聚合指标视为单次人工观测，不参与样本量门槛
            metrics.setdefault("source", "manual")
        else:
            if self.store is None:
                return {"decision": "no_data_source",
                        "reasons": ["缺少 metrics 且无历史任务数据源"],
                        "candidate_id": cand.candidate_id}
            since = cand.deployed_at or (time.time() - pol.window_seconds)
            metrics = monitor.collect(since,
                                      target_domain=cand.target_domain,
                                      policy=pol)

        result = monitor.assess(metrics, pol)
        result["candidate_id"] = cand.candidate_id
        cand.monitor_result = result
        decision = result["decision"]

        if decision == "rollback":
            reason = "; ".join(result["reasons"])
            self.rollback(cand, reason)
            result["release"] = (self.releases.rollback(reason)
                                 if self.releases else {"ok": False})
        elif decision == "promote":
            self.promote(cand, metrics)
        else:
            # 样本不足 / 观望 / 建议回滚：保持 MONITORING，不晋级
            self._persist(cand)
        result["status"] = cand.status.value

        if self.bus:
            self.bus.emit(event_type="evolution_monitored",
                          component_id="evolution",
                          payload={"candidate_id": cand.candidate_id,
                                   "decision": decision,
                                   "reasons": result["reasons"],
                                   "warnings": result.get("warnings", []),
                                   "samples": (metrics or {}).get("samples")})
        return result

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
