"""发布后监测（PRD 82 / Architecture 11.1 第 5 层 / 20.1-20.2）。

监测窗口内从真实运行任务与事件采集指标，与前一个等长窗口对比，
输出 insufficient_data / hold / promote / rollback 判定。

设计约定：
- 评测任务（runtime_manifest.source == "evaluation"）不计入真实运行监测。
- 样本不足时不得据此晋级；只有确定性的关键安全事件可以立即触发回滚。
- 手工传入的聚合指标视为单次人工观测，不参与样本量门槛，但会在结果中标注。
"""

import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from ..core.task import TaskStatus


@dataclass
class MonitorPolicy:
    window_seconds: float = 24 * 3600
    min_samples: int = 5
    min_success_rate: float = 0.80
    max_success_drop: float = 0.10
    max_error_rate: float = 0.15
    max_cancel_rate: float = 0.10
    max_cost_ratio: float = 1.50
    max_latency_ratio: float = 1.50
    max_tool_failure_rate: float = 0.20
    max_constraint_violations: int = 0
    max_cross_domain_drop: float = 0.10
    warning_margin: float = 0.05
    auto_rollback: bool = True

    def merged(self, overrides: Optional[Dict[str, Any]]) -> "MonitorPolicy":
        allowed = set(self.__dataclass_fields__)
        data = asdict(self)
        for key, value in (overrides or {}).items():
            if key in allowed and value is not None:
                data[key] = value
        # 兼容旧阈值键：success_rate 是 min_success_rate 的别名
        if "success_rate" in (overrides or {}):
            data["min_success_rate"] = overrides["success_rate"]
        return MonitorPolicy(**data)


def _usage(task: Any) -> Dict[str, Any]:
    for ev in reversed(task.result_evidence or []):
        if not isinstance(ev, dict):
            continue
        usage = (ev.get("budget") or {}).get("usage")
        if isinstance(usage, dict):
            return usage
    return {}


def _tool_failed(task: Any) -> bool:
    return any(
        isinstance(ev, dict)
        and str(ev.get("type", "")).startswith("tool")
        and ev.get("passed") is False
        for ev in (task.result_evidence or []))


def _constraint_violations(task: Any) -> int:
    total = 0
    for ev in (task.result_evidence or []):
        if isinstance(ev, dict):
            try:
                total += int(ev.get("constraint_violations", 0) or 0)
            except (TypeError, ValueError):
                pass
    return total


def collect_metrics(store: Any, since: float, until: float, *,
                    target_domain: str = "",
                    event_bus: Optional[Any] = None,
                    limit: int = 5000) -> Dict[str, Any]:
    """采集 [since, until) 内真实运行任务指标。"""
    tasks: List[Any] = []
    if store is not None:
        for task in store.list_tasks(limit=limit):
            if not (since <= task.created_at < until):
                continue
            if (task.runtime_manifest or {}).get("source") == "evaluation":
                continue
            tasks.append(task)

    terminal = [t for t in tasks if t.task_status in (
        TaskStatus.SUCCEEDED, TaskStatus.FAILED)]
    succeeded = [t for t in terminal if t.task_status == TaskStatus.SUCCEEDED]
    failed = [t for t in terminal if t.task_status == TaskStatus.FAILED]
    cancelled = [t for t in tasks if t.task_status == TaskStatus.CANCELLED]

    costs = []
    latencies = []
    tool_failures = 0
    constraint_violations = 0
    for task in terminal:
        usage = _usage(task)
        costs.append(float(usage.get("cost", 0.0) or 0.0))
        latencies.append(max(0.0, task.updated_at - task.created_at))
        tool_failures += 1 if _tool_failed(task) else 0
        constraint_violations += _constraint_violations(task)

    samples = len(terminal)
    success_rate = len(succeeded) / samples if samples else None
    error_rate = len(failed) / samples if samples else None
    total_tasks = len(tasks)
    cancel_rate = len(cancelled) / total_tasks if total_tasks else None
    avg_cost = sum(costs) / len(costs) if costs else 0.0
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

    cross_samples = 0
    cross_succeeded = 0
    if target_domain:
        for task in terminal:
            if task.domain == target_domain:
                continue
            cross_samples += 1
            if task.task_status == TaskStatus.SUCCEEDED:
                cross_succeeded += 1

    safety_incidents: List[Dict[str, Any]] = []
    if event_bus is not None:
        for evt in event_bus.query(limit=limit):
            if not (since <= evt.timestamp < until):
                continue
            etype = (evt.event_type or "").lower()
            if evt.severity != "critical":
                continue
            if any(word in etype for word in
                   ("safety", "security", "permission", "privacy")):
                safety_incidents.append({
                    "event_id": evt.event_id,
                    "event_type": evt.event_type,
                    "task_id": evt.task_id,
                    "timestamp": evt.timestamp})

    return {
        "window": {"since": since, "until": until},
        "tasks": total_tasks,
        "samples": samples,
        "successes": len(succeeded),
        "failures": len(failed),
        "cancelled": len(cancelled),
        "success_rate": round(success_rate, 4) if success_rate is not None else None,
        "error_rate": round(error_rate, 4) if error_rate is not None else None,
        "cancel_rate": round(cancel_rate, 4) if cancel_rate is not None else None,
        "total_cost": round(sum(costs), 4),
        "avg_cost": round(avg_cost, 6),
        "avg_latency": round(avg_latency, 4),
        "tool_failures": tool_failures,
        "tool_failure_rate": round(tool_failures / samples, 4) if samples else None,
        "constraint_violations": constraint_violations,
        "cross_domain": {
            "samples": cross_samples,
            "success_rate": round(cross_succeeded / cross_samples, 4)
            if cross_samples else None,
        },
        "safety_incidents": safety_incidents,
        "source": "runtime",
    }


class PostReleaseMonitor:
    def __init__(self, store: Any, event_bus: Optional[Any] = None,
                 policy: Optional[MonitorPolicy] = None):
        self.store = store
        self.bus = event_bus
        self.policy = policy or MonitorPolicy()

    def collect(self, since: float, *,
                target_domain: str = "",
                policy: Optional[MonitorPolicy] = None) -> Dict[str, Any]:
        pol = policy or self.policy
        until = time.time()
        current = collect_metrics(self.store, since, until,
                                  target_domain=target_domain,
                                  event_bus=self.bus)
        previous = collect_metrics(self.store, since - pol.window_seconds,
                                   since, target_domain=target_domain,
                                   event_bus=self.bus)
        self._attach_ratios(current, previous, pol)
        return current

    @staticmethod
    def _attach_ratios(current: Dict[str, Any], previous: Dict[str, Any],
                       policy: MonitorPolicy) -> None:
        current["previous"] = {
            "samples": previous.get("samples", 0),
            "success_rate": previous.get("success_rate"),
            "avg_cost": previous.get("avg_cost"),
            "avg_latency": previous.get("avg_latency"),
            "tool_failure_rate": previous.get("tool_failure_rate"),
            "cross_domain": previous.get("cross_domain"),
        }
        comparable = int(previous.get("samples") or 0) >= max(
            1, policy.min_samples // 2)
        if comparable and current.get("success_rate") is not None \
                and previous.get("success_rate") is not None:
            current["success_delta"] = round(
                current["success_rate"] - previous["success_rate"], 4)
        prev_cost = float(previous.get("avg_cost") or 0.0)
        if comparable and prev_cost > 0:
            current["cost_ratio"] = round(
                float(current.get("avg_cost") or 0.0) / prev_cost, 4)
        prev_lat = float(previous.get("avg_latency") or 0.0)
        if comparable and prev_lat > 0:
            current["latency_ratio"] = round(
                float(current.get("avg_latency") or 0.0) / prev_lat, 4)
        prev_cross = (previous.get("cross_domain") or {}).get("success_rate")
        cur_cross = (current.get("cross_domain") or {}).get("success_rate")
        if comparable and prev_cross is not None and cur_cross is not None \
                and int((current.get("cross_domain") or {}).get("samples") or 0) >= 1:
            current["cross_domain"]["success_delta"] = round(
                cur_cross - prev_cross, 4)

    def assess(self, metrics: Dict[str, Any],
               policy: Optional[MonitorPolicy] = None) -> Dict[str, Any]:
        pol = policy or self.policy
        reasons: List[str] = []
        warnings: List[str] = []

        incidents = metrics.get("safety_incidents") or []
        if metrics.get("safety_incident") or incidents:
            reasons.append(
                f"关键安全事件: {len(incidents) or 1} 个")
            # 关键安全事件必须立即停止推广；自动回滚仍受策略开关约束
            decision = "rollback" if pol.auto_rollback else "rollback_recommended"
            return self._result(decision, reasons, warnings, metrics, pol)

        manual = metrics.get("source") == "manual"
        samples = int(metrics.get("samples") or 0)
        if not manual and samples < pol.min_samples:
            return self._result("insufficient_data",
                                [f"样本不足: {samples} < {pol.min_samples}"],
                                warnings, metrics, pol)

        success = metrics.get("success_rate")
        if success is not None and float(success) < pol.min_success_rate:
            reasons.append(f"成功率过低: {success} < {pol.min_success_rate}")
        elif success is not None and float(success) < (
                pol.min_success_rate + pol.warning_margin):
            warnings.append("成功率接近阈值")

        delta = metrics.get("success_delta")
        if delta is not None and float(delta) < -pol.max_success_drop:
            reasons.append(f"成功率较前窗口下降: {delta}")

        error = metrics.get("error_rate")
        if error is not None and float(error) > pol.max_error_rate:
            reasons.append(f"错误率过高: {error} > {pol.max_error_rate}")

        cancel = metrics.get("cancel_rate")
        if cancel is not None and float(cancel) > pol.max_cancel_rate:
            reasons.append(f"用户取消率过高: {cancel} > {pol.max_cancel_rate}")

        tool_rate = metrics.get("tool_failure_rate")
        if tool_rate is not None and float(tool_rate) > pol.max_tool_failure_rate:
            reasons.append(
                f"工具失败率过高: {tool_rate} > {pol.max_tool_failure_rate}")

        if int(metrics.get("constraint_violations") or 0) \
                > pol.max_constraint_violations:
            reasons.append(
                f"关键约束违反: {metrics.get('constraint_violations')}")

        cost_ratio = metrics.get("cost_ratio")
        if cost_ratio is not None and float(cost_ratio) > pol.max_cost_ratio:
            reasons.append(f"成本增幅过大: x{cost_ratio} > x{pol.max_cost_ratio}")
        elif cost_ratio is not None and float(cost_ratio) > (
                1.0 + (pol.max_cost_ratio - 1.0) * 0.6):
            warnings.append("成本接近阈值")

        latency_ratio = metrics.get("latency_ratio")
        if latency_ratio is not None \
                and float(latency_ratio) > pol.max_latency_ratio:
            reasons.append(
                f"时延增幅过大: x{latency_ratio} > x{pol.max_latency_ratio}")
        elif latency_ratio is not None and float(latency_ratio) > (
                1.0 + (pol.max_latency_ratio - 1.0) * 0.6):
            warnings.append("时延接近阈值")

        cross = metrics.get("cross_domain") or {}
        cross_delta = cross.get("success_delta")
        if cross_delta is not None \
                and float(cross_delta) < -pol.max_cross_domain_drop:
            reasons.append(f"跨领域回归: {cross_delta}")

        if reasons:
            decision = "rollback" if pol.auto_rollback else "rollback_recommended"
            return self._result(decision, reasons, warnings, metrics, pol)
        if warnings:
            return self._result("hold", [], warnings, metrics, pol)
        return self._result("promote", [], [], metrics, pol)

    @staticmethod
    def _result(decision: str, reasons: List[str], warnings: List[str],
                metrics: Dict[str, Any],
                policy: MonitorPolicy) -> Dict[str, Any]:
        return {"decision": decision, "reasons": reasons,
                "warnings": warnings, "metrics": metrics,
                "policy": asdict(policy), "evaluated_at": time.time()}
