"""E-07 四类信号采集器（GAP 第二批第 7 项）。

补上「从失败、成本异常、质量退化和能力缺口提出改进」的自动链路：

    真实运行证据（事件总线）
        → SignalCollector（判据判定 + 冷却去重）
        → ProblemTicket(signal_type=…, target_component=…)
        → EvolutionController.register_problem
        → run_signal_pipeline 消费 → propose_candidate（隔离候选，
           是否晋级仍完全由门禁裁定）

契约与判据默认值的单一来源：core/contracts.py 的 EVOLUTION_SIGNAL_SPEC。
证据不足（样本不够 / 判据不足）不出信号——04 卷 E-07 设计决策：
「证据不足时保持未知或候选状态，不自动改变稳定资产」。
"""

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core import contracts as C
from .controller import ProblemTicket

__all__ = ["SignalThresholds", "SignalCollector"]

# 失败信号严重级：诊断建案即已确认故障（分类结论随案可查），取 high
_FAILURE_SEVERITY = "high"
# 质量退化信号：monitor.assess 的质量退化类理由关键词（口径契约化）
_QUALITY_DROP_MARKERS = ("成功率较前窗口下降",)


@dataclass
class SignalThresholds:
    """四类信号判据参数（默认值来自 contracts，测试可覆盖）。"""

    cooldown_seconds: Dict[str, float] = field(
        default_factory=lambda: dict(C.EVOLUTION_SIGNAL_COOLDOWN_SPEC))
    cost_ratio: float = C.COST_ANOMALY_RATIO
    cost_floor: float = C.COST_ANOMALY_FLOOR
    cost_min_recent: int = C.COST_ANOMALY_MIN_RECENT
    gap_min_fallbacks: int = C.CAPABILITY_GAP_MIN_FALLBACKS
    gap_window_seconds: float = C.CAPABILITY_GAP_WINDOW_SECONDS


class SignalCollector:
    """订阅运行事件，判定四类信号并登记问题票据。

    收集器不知道候选如何评审——那是 evaluator/gate 的职责；它只负责
    把真实运行证据翻译成带命名规范的 ProblemTicket。
    """

    def __init__(self, controller: Any,
                 thresholds: Optional[SignalThresholds] = None,
                 task_loader: Optional[Callable[[str], Any]] = None):
        self.controller = controller
        self.thresholds = thresholds or SignalThresholds()
        self.task_loader = task_loader
        # (signal_type, target_component) → 最近出票时刻（冷却去重）
        self._last_emit: Dict[Tuple[str, str], float] = {}
        # 能力缺口：intent → 窗口内兜底时刻列表
        self._fallbacks: Dict[str, List[float]] = {}
        # 成本异常：近期已完成真实运行任务的账单成本窗口
        self._cost_history: deque = deque(maxlen=20)

    # ---- 总线接入 ------------------------------------------------------
    def attach(self, bus: Optional[Any]) -> None:
        if bus is None:
            return
        bus.subscribe(self.on_event)

    def on_event(self, event: Any) -> Optional[Any]:
        et = getattr(event, "event_type", "")
        if et == "diagnostic_case_created":
            return self.on_failure_signal(getattr(event, "payload", None) or {})
        if et == "task_finalized":
            if self.task_loader is None or not getattr(event, "task_id", None):
                return None
            return self.inspect_task_cost(self.task_loader(event.task_id))
        if et == "expert_fallback":
            return self.on_expert_fallback(getattr(event, "payload", None) or {})
        if et == "evolution_monitored":
            return self.on_quality_regression(
                getattr(event, "payload", None) or {})
        return None

    # ---- 1) 失败信号：诊断建案即出 -------------------------------------
    def on_failure_signal(self, payload: Dict[str, Any]) -> Optional[Any]:
        case_id = payload.get("case_id") or ""
        task_id = payload.get("task_id") or ""
        if not case_id or not task_id:
            return None
        domain = "general"
        task = self.task_loader(task_id) if self.task_loader else None
        if task is not None:
            domain = getattr(task, "domain", "") or "general"
        categories = [str(c) for c in (payload.get("categories") or [])]
        evidence = [f"diagnostic_case/{case_id}", f"task/{task_id}"]
        if categories:
            evidence.append("categories=" + ",".join(categories))
        return self._register(
            C.EVOLUTION_SIGNAL_FAILURE,
            f"expert/{domain}",
            f"诊断建案 {case_id}：{len(categories) or 1} 个故障假设待修复",
            severity=_FAILURE_SEVERITY,
            source_task_id=task_id,
            evidence=evidence,
            hypothesis=(
                f"任务 {task_id} 失败已诊断成案（{case_id}）；按诊断假设构造"
                f"专家 {domain} 的局部修复候选，经隔离评测后再裁决"),
            change_scope=C.EVOLUTION_SIGNAL_SPEC[
                C.EVOLUTION_SIGNAL_FAILURE]["change_scope"],
        )

    # ---- 2) 成本异常信号：任务账单 vs 近期基线 -------------------------
    def inspect_task_cost(self, task: Any) -> Optional[Any]:
        if task is None:
            return None
        # 评测样本不计入真实成本口径（PRD 82）
        if (getattr(task, "runtime_manifest", None) or {}).get(
                "source") == "evaluation":
            return None
        cost = self._task_cost(task)
        th = self.thresholds
        ticket = None
        recent = list(self._cost_history)
        if len(recent) >= th.cost_min_recent:
            recent_sorted = sorted(recent)
            mid = len(recent_sorted) // 2
            median = recent_sorted[mid]
            if median > 0 and cost > max(th.cost_ratio * median, th.cost_floor):
                ticket = self._register(
                    C.EVOLUTION_SIGNAL_COST_ANOMALY, "budget",
                    f"任务 {getattr(task, 'task_id', '?')} 成本 {cost:.4f} "
                    f"超近窗中位数 {median:.4f} 的 {th.cost_ratio:g} 倍判据",
                    severity="medium",
                    source_task_id=getattr(task, "task_id", None),
                    evidence=[f"task/{getattr(task, 'task_id', '?')}",
                              f"cost={cost:.4f}",
                              f"recent_median={median:.4f}",
                              f"recent_samples={len(recent)}"],
                    hypothesis=(
                        "成本异常源于低效执行链（冗余工具调用/上下文膨胀）；"
                        "构造预算画像收敛候选（budget_policy），先隔离评测"),
                    change_scope=C.EVOLUTION_SIGNAL_SPEC[
                        C.EVOLUTION_SIGNAL_COST_ANOMALY]["change_scope"],
                )
        self._cost_history.append(cost)
        return ticket

    def _task_cost(self, task: Any) -> float:
        for row in (getattr(task, "result_evidence", None) or []):
            usage = ((row.get("budget") or {}).get("usage")
                     or {}) if isinstance(row, dict) else {}
            try:
                return float(usage.get("cost") or 0.0)
            except (TypeError, ValueError):  # noqa: PERF203
                return 0.0
        return 0.0

    # ---- 3) 质量退化信号：发布后监测退化结论 ---------------------------
    def on_quality_regression(self, payload: Dict[str, Any]) -> Optional[Any]:
        reasons = [str(r) for r in (payload.get("reasons") or [])]
        dropped = [r for r in reasons
                   if any(m in r for m in _QUALITY_DROP_MARKERS)]
        if not dropped:
            return None
        candidate_id = payload.get("candidate_id") or ""
        target = "release/unknown"
        cand = self.controller.candidates.get(candidate_id) \
            if hasattr(self.controller, "candidates") else None
        if cand is not None:
            target = cand.target_component
        return self._register(
            C.EVOLUTION_SIGNAL_QUALITY_REGRESSION, target,
            f"发布候选 {candidate_id} 监测质量退化：{'；'.join(dropped)}",
            severity="high",
            source_task_id=None,
            evidence=[f"candidate/{candidate_id}"] + dropped,
            hypothesis=(
                f"候选 {candidate_id} 在真实运行中质量退化（{dropped[0]}）；"
                f"构造对齐基线行为的回退/修正候选，交门禁评审"),
        )

    # ---- 4) 能力缺口信号：意图无专属专家兜底 --------------------------
    def on_expert_fallback(self, payload: Dict[str, Any]) -> Optional[Any]:
        intent = (payload.get("intent") or "").strip()
        if intent and intent != "general":
            now = time.time()
            window = self.thresholds.gap_window_seconds
            hits = [t for t in self._fallbacks.get(intent, [])
                    if now - t <= window]
            hits.append(now)
            self._fallbacks[intent] = hits
            if len(hits) >= self.thresholds.gap_min_fallbacks:
                evidence = [f"intent/{intent}",
                            f"expert/{payload.get('expert_id') or 'general'}",
                            f"fallbacks={len(hits)}"]
                self._fallbacks[intent] = []   # 出票后重新计数
                return self._register(
                    C.EVOLUTION_SIGNAL_CAPABILITY_GAP, f"expert/{intent}",
                    f"意图 {intent} 在能力目录反复无专属专家兜底"
                    f"（{len(hits)} 次）",
                    severity="medium",
                    evidence=evidence,
                    hypothesis=(
                        f"为意图 {intent} 建专属专家并扩能力目录"
                        f"（expert/{intent}），以消除 general 兜底路径"),
                    change_scope=C.EVOLUTION_SIGNAL_SPEC[
                        C.EVOLUTION_SIGNAL_CAPABILITY_GAP]["change_scope"],
                )
        return None

    # ---- 登记与去重 ----------------------------------------------------
    def _register(self, signal_type: str, target_component: str,
                  description: str, *, severity: str = "medium",
                  source_task_id: Optional[str] = None,
                  evidence: Optional[List[str]] = None,
                  hypothesis: str = "",
                  change_scope: str = "local") -> Optional[Any]:
        key = (signal_type, target_component)
        now = time.time()
        cooldown = float(self.thresholds.cooldown_seconds.get(
            signal_type, 0.0))
        last = self._last_emit.get(key)
        if last is not None and cooldown > 0 and now - last < cooldown:
            return None
        self._last_emit[key] = now
        ticket = ProblemTicket(
            ticket_id=f"tk_{uuid.uuid4().hex[:8]}",
            description=description,
            severity=severity,
            source_task_id=source_task_id,
            evidence=list(evidence or []),
            signal_type=signal_type,
            target_component=target_component,
            hypothesis=hypothesis,
            change_scope=change_scope,
        )
        self.controller.register_problem(ticket)
        bus = getattr(self.controller, "bus", None)
        if bus is not None:
            try:
                bus.emit(event_type="evolution_signal",
                         component_id="evolution",
                         payload={"signal_type": signal_type,
                                  "target_component": target_component,
                                  "ticket_id": ticket.ticket_id,
                                  "severity": severity})
            except Exception:  # noqa: BLE001 —— 事件自身故障不影响执行
                pass
        return ticket
