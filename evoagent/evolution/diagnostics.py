"""根因诊断（PRD 41-45 / Architecture 9）。

原则：
- 先区分故障来源（环境/工具/智能体内部/复合），再选择处理路径；不把所有失败
  归咎于提示词，也不全部归咎于环境。
- 假设由运行证据确定性生成；模型生成的解释不能被当作真实根因（Arch 9.4）。
- 置信度是诊断评分，不是校准概率；每条假设保留支持/反对证据与剩余不确定性。
- 诊断实验使用固定探针，与生产任务隔离并受预算限制。
- 诊断不发布任何改动；候选升级与发布仍走进化门禁（PRD 45/81）。
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from ..acceptance.verify import Verdict
from ..core.events import EventBus
from ..core.task import Task, TaskStatus
from ..models.base import ChatMessage
from ..tools.contract import ToolResult


class FaultCategory(str, Enum):
    """PRD 42 故障分类：A 环境 / B 工具 / C 智能体内部 / D 复合。"""

    ENVIRONMENT = "ENVIRONMENT"
    TOOL = "TOOL"
    AGENT = "AGENT"
    COMPOSITE = "COMPOSITE"


class CaseStatus(str, Enum):
    OPEN = "open"
    DIAGNOSED = "diagnosed"
    INCONCLUSIVE = "inconclusive"
    CLOSED = "closed"


class HypothesisStatus(str, Enum):
    PROPOSED = "proposed"
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"


@dataclass
class Hypothesis:
    hypothesis_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    case_id: str = ""
    category: FaultCategory = FaultCategory.AGENT
    component: str = ""
    statement: str = ""
    supporting_evidence: List[str] = field(default_factory=list)
    opposing_evidence: List[str] = field(default_factory=list)
    confidence: float = 0.3
    suggested_probes: List[str] = field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    source: str = "rules"                 # rules | model（模型输出仅为辅助解释）
    created_at: float = field(default_factory=time.time)

    @property
    def uncertainty(self) -> float:
        return round(max(0.0, 1.0 - self.confidence), 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id, "case_id": self.case_id,
            "category": self.category.value, "component": self.component,
            "statement": self.statement,
            "supporting_evidence": self.supporting_evidence,
            "opposing_evidence": self.opposing_evidence,
            "confidence": self.confidence, "uncertainty": self.uncertainty,
            "suggested_probes": self.suggested_probes,
            "status": self.status.value, "source": self.source,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Hypothesis":
        d = dict(d)
        d.pop("uncertainty", None)
        d["category"] = FaultCategory(d.get("category", "AGENT"))
        d["status"] = HypothesisStatus(d.get("status", "proposed"))
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


@dataclass
class DiagnosticExperiment:
    experiment_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    case_id: str = ""
    hypothesis_id: str = ""
    method: str = ""
    result: str = "inconclusive"          # pass | fail | inconclusive
    evidence: Dict[str, Any] = field(default_factory=dict)
    cost: float = 0.0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DiagnosticExperiment":
        d = dict(d)
        d["evidence"] = d.get("evidence") or {}
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


@dataclass
class DiagnosticCase:
    case_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str = ""
    run_id: str = ""
    status: CaseStatus = CaseStatus.OPEN
    symptom: str = ""
    evidence_snapshot: Dict[str, Any] = field(default_factory=dict)
    version_manifest: Dict[str, Any] = field(default_factory=dict)
    candidate_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id, "task_id": self.task_id,
            "run_id": self.run_id, "status": self.status.value,
            "symptom": self.symptom,
            "evidence_snapshot": self.evidence_snapshot,
            "version_manifest": self.version_manifest,
            "candidate_id": self.candidate_id,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DiagnosticCase":
        d = dict(d)
        d["status"] = CaseStatus(d.get("status", "open"))
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


# 证据类别 -> 生成的基础假设。
# key 是假设的 (category, component)，同一键被多次命中时合并证据并加分。
_RULES: List[Dict[str, Any]] = [
    {"error_class": "timeout", "category": FaultCategory.ENVIRONMENT,
     "component": "dependencies/network", "confidence": 0.55,
     "statement": "依赖服务或网络超时，任务在当前环境下无法完成",
     "probes": ["tool_health"]},
    {"error_class": "timeout", "category": FaultCategory.TOOL,
     "component": "tool/{tool}/timeout", "confidence": 0.40,
     "statement": "工具超时策略过短或执行被阻塞",
     "probes": ["calc_roundtrip"]},
    {"error_class": "network_or_unavailable", "category": FaultCategory.ENVIRONMENT,
     "component": "dependencies/availability", "confidence": 0.70,
     "statement": "外部依赖不可用或返回错误状态",
     "probes": ["tool_health"]},
    {"error_class": "auth_or_permission", "category": FaultCategory.ENVIRONMENT,
     "component": "permissions/system", "confidence": 0.50,
     "statement": "系统权限或凭据状态发生变化，原授权不再有效",
     "probes": ["tool_health"]},
    {"error_class": "auth_or_permission", "category": FaultCategory.AGENT,
     "component": "agent/permissions", "confidence": 0.50,
     "statement": "智能体请求了越权工具或越界参数",
     "probes": []},
    {"error_class": "invalid_arguments", "category": FaultCategory.AGENT,
     "component": "planning/prompt", "confidence": 0.55,
     "statement": "规划或提示词遗漏了工具参数约束",
     "probes": []},
    {"error_class": "invalid_arguments", "category": FaultCategory.TOOL,
     "component": "tool/{tool}/contract", "confidence": 0.35,
     "statement": "工具契约与实际实现不一致",
     "probes": ["calc_roundtrip"]},
    {"error_class": "tool_defect", "category": FaultCategory.TOOL,
     "component": "tool/{tool}", "confidence": 0.65,
     "statement": "工具实现存在缺陷",
     "probes": ["calc_roundtrip", "file_write_sandbox"]},
    {"error_class": "tool_defect_or_env", "category": FaultCategory.TOOL,
     "component": "tool/{tool}", "confidence": 0.65,
     "statement": "工具实现或运行环境存在缺陷",
     "probes": ["calc_roundtrip", "tool_health"]},
    {"error_class": "budget_exceeded", "category": FaultCategory.AGENT,
     "component": "react/loop", "confidence": 0.50,
     "statement": "执行循环无进展，导致预算被耗尽",
     "probes": ["state_machine_selfcheck"]},
    {"error_class": "budget_exceeded", "category": FaultCategory.AGENT,
     "component": "harness/budget", "confidence": 0.40,
     "statement": "预算策略对当前任务过紧",
     "probes": []},
    {"stop_reason": "repeated_failure", "category": FaultCategory.AGENT,
     "component": "react/recovery", "confidence": 0.60,
     "statement": "同一失败模式重复出现，恢复策略未能跳出循环",
     "probes": ["state_machine_selfcheck"]},
    {"stop_reason": "no_progress", "category": FaultCategory.AGENT,
     "component": "react/loop", "confidence": 0.65,
     "statement": "重复行动检测触发，ReAct 陷入无进展循环",
     "probes": ["state_machine_selfcheck"]},
    {"stop_reason": "needs_user", "category": FaultCategory.AGENT,
     "component": "intent/classifier", "confidence": 0.55,
     "statement": "意图存在歧义，系统按风险暂停等待澄清",
     "probes": []},
    {"stop_reason": "max_iter", "category": FaultCategory.AGENT,
     "component": "react/loop", "confidence": 0.50,
     "statement": "达到最大循环次数仍未完成，计划或终止条件可能不完整",
     "probes": []},
]

_CONTRACT_RULE = {
    "category": FaultCategory.TOOL, "component": "tool/{tool}/contract",
    "confidence": 0.70, "statement": "工具返回数据不符合契约",
    "probes": ["calc_roundtrip"],
}
_SIDE_EFFECT_RULES = [
    {"category": FaultCategory.COMPOSITE,
     "component": "runtime/side-effects", "confidence": 0.60,
     "statement": "外部副作用可能已发生但结果未知，需先核实实际状态",
     "probes": ["file_write_sandbox"]},
    {"category": FaultCategory.AGENT, "component": "runtime/recovery",
     "confidence": 0.50, "statement": "运行时恢复流程对副作用状态处理不充分",
     "probes": ["state_machine_selfcheck"]},
]
_ACCEPTANCE_RULES = [
    {"category": FaultCategory.AGENT, "component": "planning/acceptance",
     "confidence": 0.50, "statement": "计划目标与验收条件不一致，任务产出未满足验收",
     "probes": []},
    {"category": FaultCategory.AGENT, "component": "harness/verifier",
     "confidence": 0.40, "statement": "验收器配置或判定规则与任务目标不匹配",
     "probes": []},
]
_NO_TOOL_RULES = [
    {"category": FaultCategory.AGENT, "component": "planning/classifier",
     "confidence": 0.50, "statement": "任务在规划或分类阶段即失败，未进入工具执行",
     "probes": []},
]


class DiagnosisEngine:
    """诊断案例生成、探针实验与候选升级。"""

    def __init__(self, db: Any = None, bus: Optional[EventBus] = None,
                 probe_runner: Any = None, config: Any = None,
                 evolution: Any = None, model: Any = None):
        self.db = db
        self.bus = bus
        self.probe_runner = probe_runner
        self.config = config
        self.evolution = evolution
        # 仅用于生成「辅助解释」；不参与规则判定，也没有根因权威（Arch 9.4）
        self.model = model

    # ================= 案例生成 =================
    def record_failure(self, task: Task, verdict: Optional[Verdict],
                       tool_results: Optional[List[ToolResult]] = None,
                       stop_reason: str = "") -> DiagnosticCase:
        if self.db is not None:
            existing = self.db.get_case_by_task(task.task_id)
            if existing:
                return existing
        tool_results = list(tool_results or [])
        case = DiagnosticCase(
            task_id=task.task_id,
            run_id=f"run_{task.task_id}",
            symptom=(task.failure_reason or (verdict.summary() if verdict else "")
                     or "任务失败"),
            version_manifest=dict(task.version_manifest or {}),
            evidence_snapshot=_snapshot(verdict, tool_results, stop_reason,
                                        task, source="runtime"),
        )
        hypotheses = self._generate_hypotheses(case, tool_results, stop_reason,
                                               verdict, task)
        if not hypotheses:
            hypotheses = [_fallback_hypothesis(case)]
        for h in hypotheses:
            h.case_id = case.case_id
        case.status = (CaseStatus.DIAGNOSED
                       if any(h.confidence >= 0.5 for h in hypotheses)
                       else CaseStatus.INCONCLUSIVE)
        self._persist_case(case, hypotheses)
        self._emit("diagnostic_case_created", {
            "case_id": case.case_id, "task_id": task.task_id,
            "hypotheses": len(hypotheses),
            "categories": [h.category.value for h in hypotheses]})
        return case

    def diagnose_task(self, task_id: str) -> Optional[DiagnosticCase]:
        """回溯已失败任务（证据来自持久化事件）。"""
        if self.db is None:
            return None
        existing = self.db.get_case_by_task(task_id)
        if existing:
            return existing
        task = self.db.get_task(task_id)
        if task is None or task.task_status != TaskStatus.FAILED:
            return None
        events = self.bus.query(task_id=task_id, limit=200) if self.bus else []
        stop_reason = ""
        tool_errors: List[Dict[str, Any]] = []
        for ev in events:
            if ev.event_type == "tool_call" and ev.payload.get("ok") is False:
                tool_errors.append({
                    "tool": ev.payload.get("tool"),
                    "error_class": ev.payload.get("error_class"),
                })
        for ev in reversed(events):
            if ev.event_type in ("task_failed", "task_finalized"):
                reason = (ev.payload or {}).get("reason", "")
                if "repeated" in reason:
                    stop_reason = "repeated_failure"
                break
        verdict = _verdict_from_task(task)
        case = self.record_failure(
            task, verdict,
            [_synthetic_result(e) for e in tool_errors],
            stop_reason=stop_reason)
        case.evidence_snapshot["source"] = "replay"
        self._persist_case(case, self._hypotheses_of(case))
        return case

    # ---- 规则生成 ----
    def _generate_hypotheses(self, case: DiagnosticCase,
                             tool_results: List[ToolResult],
                             stop_reason: str, verdict: Optional[Verdict],
                             task: Task) -> List[Hypothesis]:
        merged: Dict[tuple, Hypothesis] = {}
        errors = [r for r in tool_results if not r.ok]
        error_classes = {r.error_class for r in errors if r.error_class}

        def add(rule: Dict[str, Any], tool: str, evidence: str) -> None:
            category = rule["category"]
            component = rule["component"].format(tool=tool or "unknown")
            key = (category, component)
            if key in merged:
                h = merged[key]
                if evidence not in h.supporting_evidence:
                    h.supporting_evidence.append(evidence)
                    h.confidence = min(0.90, round(h.confidence + 0.10, 2))
                return
            merged[key] = Hypothesis(
                case_id=case.case_id, category=category, component=component,
                statement=rule["statement"],
                supporting_evidence=[evidence],
                confidence=float(rule["confidence"]),
                suggested_probes=list(rule.get("probes") or []))

        for r in errors:
            tool = str(r.evidence.get("tool") or "unknown")
            for rule in _RULES:
                if rule.get("error_class") == r.error_class:
                    add(rule, tool,
                        f"tool={tool} error_class={r.error_class} error={r.error}")
            # 只有「执行了但返回不合契约」才算契约问题；
            # 未执行失败（如连接失败）的 contract_valid=False 是默认值，不是证据。
            if r.executed and r.contract_valid is False:
                add(_CONTRACT_RULE, tool,
                    f"tool={tool} 返回未通过契约校验")
            if r.side_effect_uncertain:
                for rule in _SIDE_EFFECT_RULES:
                    add(rule, tool,
                        f"tool={tool} 副作用状态未确认")

        if stop_reason:
            for rule in _RULES:
                if rule.get("stop_reason") == stop_reason:
                    add(rule, "", f"stop_reason={stop_reason}")

        all_ok = bool(tool_results) and all(r.ok for r in tool_results)
        if verdict is not None and not verdict.passed and all_ok:
            for rule in _ACCEPTANCE_RULES:
                add(rule, "", "工具均成功但验收未通过")
        if not tool_results and verdict is not None and not verdict.passed:
            for rule in _NO_TOOL_RULES:
                add(rule, "", f"任务 {task.task_id} 无工具调用即失败")

        hypotheses = list(merged.values())
        # 同轮成功调用作为反对证据（弱化“整体环境故障”与“全局缺陷”）
        ok_tools = sorted({str(r.evidence.get("tool")) for r in tool_results if r.ok})
        for h in hypotheses:
            if h.category == FaultCategory.ENVIRONMENT and ok_tools:
                h.opposing_evidence.append(
                    f"同一运行中其他工具调用成功: {', '.join(ok_tools)}")
            if h.category == FaultCategory.AGENT and all_ok:
                h.opposing_evidence.append("全部工具调用成功，问题可能不在工具层")
        # 加分后重新限制上限
        for h in hypotheses:
            h.confidence = round(min(0.90, max(0.10, h.confidence)), 2)
            if h.status == HypothesisStatus.PROPOSED and h.confidence >= 0.7:
                h.status = HypothesisStatus.SUPPORTED
        return hypotheses

    # ---- 模型辅助假设（Arch 9.4：仅作解释，不作根因）----
    def suggest_hypotheses(self, case_id: str,
                           max_items: Optional[int] = None) -> Dict[str, Any]:
        """让模型基于已有证据提出候选解释。

        安全约束：
        - 每条假设必须引用证据快照中真实存在的片段，否则丢弃；
        - 置信度封顶 ``_MODEL_CONFIDENCE_CAP``，状态保持 proposed；
        - 不改变案例状态，不触发探针，不发布任何改动；
        - 与确定性规则假设重复的 (category, component) 直接跳过。
        """
        if self.model is None:
            raise RuntimeError("未配置模型，无法生成辅助假设")
        case = self.get_case(case_id)
        if case is None:
            raise KeyError(case_id)
        default_limit = getattr(self.config, "max_assist_hypotheses", 3)
        try:
            limit = int(max_items) if max_items is not None else default_limit
        except (TypeError, ValueError):
            limit = default_limit
        limit = max(1, min(limit, 5))
        existing = self._hypotheses_of(case)
        model_id = ""
        try:
            model_id = self.model.profile().model_id
        except Exception:  # noqa: BLE001
            pass
        try:
            raw = self.model.generate([
                ChatMessage(role="system", content=_ASSIST_SYSTEM_PROMPT),
                ChatMessage(role="user", content=_assist_prompt(case, existing)),
            ])
        except Exception as e:  # noqa: BLE001 —— 模型失败不影响诊断本身
            return {"case_id": case.case_id, "advisory": True,
                    "model_id": model_id, "created": [],
                    "skipped": [{"reason":
                                 f"model_error: {type(e).__name__}"}]}

        items, parse_error = _parse_hypotheses(raw)
        if items is None:
            return {"case_id": case.case_id, "advisory": True,
                    "model_id": model_id, "created": [],
                    "skipped": [{"reason": parse_error or "invalid_json"}]}

        evidence_text = _evidence_text(case)
        known_keys = {(h.category, h.component.lower()) for h in existing}
        created: List[Hypothesis] = []
        skipped: List[Dict[str, Any]] = []
        for item in items:
            if len(created) >= limit:
                break
            parsed, reason = _validate_model_hypothesis(
                item, case, evidence_text)
            if parsed is None:
                skipped.append({"reason": reason,
                                "statement": str(item)[:200]})
                continue
            key = (parsed.category, parsed.component.lower())
            if key in known_keys:
                skipped.append({"reason": "duplicate",
                                "component": parsed.component})
                continue
            known_keys.add(key)
            parsed.source = "model"
            created.append(parsed)

        for h in created:
            if self.db is not None:
                self.db.save_hypothesis(h)
        case.updated_at = time.time()
        if self.db is not None:
            self.db.save_diagnostic_case(case)
        if created:
            self._emit("diagnostic_hypotheses_suggested", {
                "case_id": case.case_id, "created": len(created),
                "skipped": len(skipped), "model_id": model_id})
        return {"case_id": case.case_id, "advisory": True,
                "model_id": model_id,
                "created": [h.to_dict() for h in created],
                "skipped": skipped}

    # ================= 诊断实验 =================
    def run_experiment(self, case: DiagnosticCase, hypothesis: Hypothesis,
                       method: str, fault: Optional[str] = None
                       ) -> DiagnosticExperiment:
        if self.probe_runner is None:
            raise RuntimeError("未配置探针执行器")
        limit = getattr(self.config, "max_experiments_per_case", 3)
        used = (self.db.list_experiments(case.case_id)
                if self.db is not None else [])
        if len(used) >= limit:
            raise PermissionError(f"诊断实验次数已达上限 {limit}")
        daily_limit = getattr(self.config, "max_daily_experiments", 50)
        if self.db is not None and daily_limit:
            since = _today_start()
            if self.db.count_experiments_since(since) >= daily_limit:
                raise PermissionError(f"每日诊断实验已达上限 {daily_limit}")

        started = time.time()
        probe = self.probe_runner.run(method, fault=fault)
        exp = DiagnosticExperiment(
            case_id=case.case_id, hypothesis_id=hypothesis.hypothesis_id,
            method=method,
            result=("pass" if probe.passed is True else
                    "fail" if probe.passed is False else "inconclusive"),
            evidence=dict(getattr(probe, "evidence", {}) or {}),
            cost=float(getattr(probe, "cost", 0.0) or 0.0),
            started_at=started, finished_at=time.time())

        # 探针结果只调整假设评分，不直接宣布根因（Arch 9.4）
        if probe.passed is False:
            hypothesis.supporting_evidence.append(f"探针 {method} 失败")
            hypothesis.confidence = min(0.90, round(hypothesis.confidence + 0.10, 2))
            hypothesis.status = (HypothesisStatus.SUPPORTED
                                 if hypothesis.confidence >= 0.6
                                 else HypothesisStatus.PROPOSED)
        elif probe.passed is True:
            hypothesis.opposing_evidence.append(f"探针 {method} 通过")
            hypothesis.confidence = max(0.10, round(hypothesis.confidence - 0.10, 2))
            hypothesis.status = (HypothesisStatus.CONTRADICTED
                                 if hypothesis.confidence < 0.30
                                 else HypothesisStatus.PROPOSED)
        else:
            hypothesis.status = HypothesisStatus.INCONCLUSIVE

        case.updated_at = time.time()
        if self.db is not None:
            self.db.save_experiment(exp)
            self.db.save_hypothesis(hypothesis)
            self.db.save_diagnostic_case(case)
        self._emit("diagnostic_experiment", {
            "case_id": case.case_id, "method": method, "result": exp.result})
        return exp

    # ================= 候选升级 =================
    def promote_to_candidate(self, case: DiagnosticCase, hypothesis_id: str,
                             change_scope: Optional[str] = None,
                             risk_level: Optional[str] = None) -> Any:
        if self.evolution is None:
            raise RuntimeError("未配置进化控制器")
        hypothesis = next((h for h in self._hypotheses_of(case)
                           if h.hypothesis_id == hypothesis_id), None)
        if hypothesis is None:
            raise KeyError(f"假设不存在: {hypothesis_id}")
        if not hypothesis.supporting_evidence:
            raise ValueError("假设缺少支持证据，不能创建候选（PRD 4.4）")
        if hypothesis.source == "model":
            verified = bool(self.db is not None and any(
                e.hypothesis_id == hypothesis_id
                for e in self.db.list_experiments(case.case_id)))
            if not verified:
                raise ValueError(
                    "模型辅助假设需先经固定探针验证才能升级候选（Arch 9.4）")
        baseline = (case.version_manifest.get("version")
                    or case.version_manifest.get("release_id") or "unreleased")
        cand = self.evolution.candidate_from_hypothesis(
            hypothesis, baseline_version=baseline,
            change_scope=change_scope, risk_level=risk_level)
        case.candidate_id = cand.candidate_id
        case.updated_at = time.time()
        if self.db is not None:
            self.db.save_diagnostic_case(case)
        self._emit("diagnostic_candidate_created", {
            "case_id": case.case_id, "candidate_id": cand.candidate_id})
        return cand

    # ================= 查询辅助 =================
    def get_case(self, case_id: str) -> Optional[DiagnosticCase]:
        return self.db.get_diagnostic_case(case_id) if self.db else None

    def trends(self, limit: int = 200) -> Dict[str, Any]:
        """失败趋势聚合（PRD 42 / Arch 9.3）：按状态、故障类别与组件统计。"""
        from collections import Counter
        cases = self.db.list_diagnostic_cases(limit=limit) if self.db else []
        by_status: Counter = Counter()
        by_category: Counter = Counter()
        by_component: Counter = Counter()
        by_intent: Counter = Counter()
        for case in cases:
            by_status[case.status.value] += 1
            by_intent[str(case.evidence_snapshot.get("intent") or "unknown")] += 1
            for h in self._hypotheses_of(case):
                by_category[h.category.value] += 1
                by_component[h.component] += 1
        return {
            "window": limit,
            "cases": len(cases),
            "by_status": dict(by_status),
            "by_category": dict(by_category),
            "by_intent": dict(by_intent),
            "top_components": [
                {"component": k, "count": v} for k, v in
                by_component.most_common(10)],
        }

    def _hypotheses_of(self, case: DiagnosticCase) -> List[Hypothesis]:
        return self.db.list_hypotheses(case.case_id) if self.db else []

    def _persist_case(self, case: DiagnosticCase,
                      hypotheses: List[Hypothesis]) -> None:
        if self.db is None:
            return
        self.db.save_diagnostic_case(case)
        for h in hypotheses:
            h.case_id = case.case_id
            self.db.save_hypothesis(h)

    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.bus is not None:
            self.bus.emit(event_type=event_type, component_id="diagnostics",
                          payload=payload)


_MODEL_CONFIDENCE_CAP = 0.5
_MAX_STATEMENT_CHARS = 400
_MAX_COMPONENT_CHARS = 120
_MAX_PROMPT_CHARS = 6000

_ASSIST_SYSTEM_PROMPT = (
    "你是故障诊断助手。你只能基于用户给出的证据快照提出可能的解释，"
    "不能声称已确认根因，也不能编造未出现在证据中的事实。"
    "每条假设必须给出 evidence_refs，数组元素必须逐字来自证据快照。"
    "只输出 JSON 数组，不要输出其他文字。字段："
    "category（ENVIRONMENT|TOOL|AGENT|COMPOSITE）、component、statement、"
    "evidence_refs（字符串数组）、suggested_probes（字符串数组）、confidence（0-1）。"
)


def _assist_prompt(case: DiagnosticCase, existing: List[Hypothesis]) -> str:
    from .probes import PROBES
    evidence = json.dumps(case.evidence_snapshot, ensure_ascii=False,
                          indent=1, default=str)
    if len(evidence) > _MAX_PROMPT_CHARS:
        evidence = evidence[:_MAX_PROMPT_CHARS] + "\n...（已截断）"
    prior = [f"[{h.category.value}] {h.component}: {h.statement}"
             for h in existing]
    return (
        f"症状: {case.symptom}\n"
        f"证据快照:\n{evidence}\n"
        f"已有规则假设:\n" + ("\n".join(prior) if prior else "（无）") + "\n"
        f"可用固定探针: {', '.join(sorted(PROBES))}\n"
        "请提出最多 3 条有证据支撑的候选解释。"
    )


def _evidence_text(case: DiagnosticCase) -> str:
    """可被模型引用的证据文本（JSON + 规范化行），全部来自真实快照。"""
    snap = case.evidence_snapshot or {}
    parts = [json.dumps(snap, ensure_ascii=False, default=str)]
    if snap.get("failure_reason"):
        parts.append(f"failure_reason={snap['failure_reason']}")
    if snap.get("stop_reason"):
        parts.append(f"stop_reason={snap['stop_reason']}")
    for f in snap.get("tool_failures") or []:
        parts.append(
            f"tool={f.get('tool')} error_class={f.get('error_class')} "
            f"error={f.get('error')}")
    verdict = snap.get("verdict") or {}
    for check in verdict.get("checks") or []:
        if isinstance(check, dict):
            parts.append(f"check={check.get('id')} passed={check.get('passed')}")
    return "\n".join(parts).lower()


def _parse_hypotheses(raw: str):
    """解析模型输出；返回 (items, error)。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    candidates = [text]
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            parsed = parsed.get("hypotheses")
        if isinstance(parsed, list):
            return parsed, ""
    return None, "invalid_json"


def _validate_model_hypothesis(item: Any, case: DiagnosticCase,
                               evidence_text: str):
    """校验单条模型假设；返回 (Hypothesis|None, skip_reason)。"""
    if not isinstance(item, dict):
        return None, "invalid_item"
    try:
        category = FaultCategory(str(item.get("category", "")).upper())
    except ValueError:
        return None, "invalid_category"
    component = str(item.get("component") or "").strip()
    statement = str(item.get("statement") or "").strip()
    if not component or len(component) > _MAX_COMPONENT_CHARS:
        return None, "invalid_component"
    if not statement or len(statement) > _MAX_STATEMENT_CHARS:
        return None, "invalid_statement"
    refs = item.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        return None, "missing_evidence_refs"
    bound = [str(r).strip() for r in refs
             if isinstance(r, (str, int, float))
             and str(r).strip()
             and str(r).strip().lower() in evidence_text]
    if not bound:
        return None, "evidence_refs_not_in_snapshot"
    try:
        confidence = float(item.get("confidence", 0.3))
    except (TypeError, ValueError):
        confidence = 0.3
    confidence = round(max(0.10, min(_MODEL_CONFIDENCE_CAP, confidence)), 2)

    from .probes import PROBES
    probes = item.get("suggested_probes")
    probes = [p for p in probes if p in PROBES] if isinstance(probes, list) else []

    return Hypothesis(
        case_id=case.case_id, category=category, component=component,
        statement=statement,
        supporting_evidence=[f"引用证据: {r}" for r in bound[:5]],
        confidence=confidence, suggested_probes=probes[:4],
        status=HypothesisStatus.PROPOSED, source="model"), ""


def _snapshot(verdict: Optional[Verdict], tool_results: List[ToolResult],
              stop_reason: str, task: Task, source: str) -> Dict[str, Any]:
    return {
        "source": source,
        "stop_reason": stop_reason,
        "failure_reason": task.failure_reason,
        "intent": task.intent,
        "domain": task.domain,
        "confidence": task.confidence,
        "tool_calls": len(tool_results),
        "tool_failures": [
            {"tool": r.evidence.get("tool"), "error_class": r.error_class,
             "error": r.error}
            for r in tool_results if not r.ok],
        "verdict": ({"passed": verdict.passed, "checks": verdict.checks}
                    if verdict else None),
    }


def _fallback_hypothesis(case: DiagnosticCase) -> Hypothesis:
    return Hypothesis(
        case_id=case.case_id, category=FaultCategory.COMPOSITE,
        component="unknown", statement="证据不足，无法确定根因，需要补充探针",
        supporting_evidence=["未命中已知故障模式"],
        confidence=0.30, suggested_probes=["tool_health"],
        status=HypothesisStatus.INCONCLUSIVE)


def _synthetic_result(err: Dict[str, Any]) -> ToolResult:
    return ToolResult(ok=False, error=f"replayed {err.get('error_class')}",
                      error_class=err.get("error_class"),
                      evidence={"tool": err.get("tool")})


def _verdict_from_task(task: Task) -> Optional[Verdict]:
    for ev in reversed(task.result_evidence):
        v = ev.get("verdict")
        if isinstance(v, dict) and "passed" in v:
            return Verdict(passed=bool(v["passed"]),
                           checks=v.get("checks") or [])
    return Verdict(passed=False, checks=[])


def _today_start() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
