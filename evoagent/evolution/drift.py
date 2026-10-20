"""环境漂移检测（PRD 44 / Architecture 9.3, 9.5）。

对比相邻时间窗，识别：接口契约变化、模型行为漂移、工具成功率变化、
任务分布变化与依赖版本变化，并给出停止/健康检查/兼容路径/进化四类处置建议。

检测是无状态、可重放的分析，不修改运行时状态；只有显式 emit=True 时才写
`drift_detected` 事件，避免观测调用自身制造事件风暴。
"""

import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..core.events import EventBus, RunEvent

ACTION_STOP = "stop"
ACTION_HEALTH = "health_check"
ACTION_COMPAT = "compatible_path"
ACTION_EVOLUTION = "evolution"

_ACTION_PRIORITY = {
    ACTION_STOP: 0,
    ACTION_HEALTH: 1,
    ACTION_COMPAT: 2,
    ACTION_EVOLUTION: 3,
}


@dataclass
class DriftPolicy:
    window_seconds: float = 3600.0
    baseline_window_seconds: float = 3600.0
    min_tool_calls: int = 5
    min_model_calls: int = 5
    min_tasks: int = 5
    tool_success_drop: float = 0.20
    contract_error_increase: float = 0.10
    model_success_drop: float = 0.10
    latency_increase_ratio: float = 1.5
    task_distribution_delta: float = 0.30
    max_events: int = 20000


class DriftDetector:
    def __init__(self, bus: EventBus, db: Any = None,
                 policy: Optional[DriftPolicy] = None):
        self.bus = bus
        self.db = db
        self.policy = policy or DriftPolicy()

    def detect(self, *, now: Optional[float] = None,
               emit: bool = False) -> Dict[str, Any]:
        p = self.policy
        now = now or time.time()
        current_start = now - p.window_seconds
        baseline_start = current_start - p.baseline_window_seconds
        current_events = self.bus.query_range(current_start, now,
                                              limit=p.max_events)
        baseline_events = self.bus.query_range(baseline_start, current_start,
                                               limit=p.max_events)
        current = _window_stats(current_events)
        baseline = _window_stats(baseline_events)
        current["tasks"] = self._task_distribution(current_start, now)
        baseline["tasks"] = self._task_distribution(baseline_start,
                                                    current_start)

        signals: List[Dict[str, Any]] = []
        self._check_tools(baseline, current, signals)
        self._check_contracts(baseline, current, signals)
        self._check_models(baseline, current, signals)
        self._check_task_distribution(baseline, current, signals)
        self._check_versions(baseline, current, signals)

        actions: List[str] = []
        for sig in signals:
            action = sig.get("recommended_action")
            if action and action not in actions:
                actions.append(action)
        actions.sort(key=lambda a: _ACTION_PRIORITY.get(a, 9))
        if not current_events and not baseline_events:
            status = "insufficient_data"
        elif signals:
            status = "drifting"
        else:
            status = "stable"
        out = {
            "status": status,
            "generated_at": now,
            "windows": {
                "current": {"start": current_start, "end": now,
                            "events": len(current_events)},
                "baseline": {"start": baseline_start,
                             "end": current_start,
                             "events": len(baseline_events)},
            },
            "signals": signals,
            "signal_count": len(signals),
            "recommended_actions": actions,
            "metrics": {
                "current": _metric_summary(current),
                "baseline": _metric_summary(baseline),
            },
        }
        if emit and signals:
            self.bus.emit(
                event_type="drift_detected",
                component_id="drift_detector",
                severity="critical"
                if any(s["severity"] == "critical" for s in signals)
                else "warning",
                payload={"signal_count": len(signals),
                         "kinds": sorted({s["kind"] for s in signals}),
                         "recommended_actions": actions})
        return out

    # ---- 各信号 ----
    def _check_tools(self, base: Dict[str, Any], cur: Dict[str, Any],
                     signals: List[Dict[str, Any]]) -> None:
        p = self.policy
        for tool in sorted(set(base["tools"]) | set(cur["tools"])):
            b = base["tools"].get(tool) or {}
            c = cur["tools"].get(tool) or {}
            if b.get("total", 0) < p.min_tool_calls \
                    or c.get("total", 0) < p.min_tool_calls:
                continue
            b_rate = b["ok"] / b["total"]
            c_rate = c["ok"] / c["total"]
            drop = b_rate - c_rate
            if drop >= p.tool_success_drop:
                signals.append({
                    "kind": "tool_success_rate",
                    "component": tool,
                    "severity": "critical" if drop >= 0.5 else "warning",
                    "detail": f"工具 {tool} 成功率下降 {round(drop, 3)}",
                    "baseline": round(b_rate, 4),
                    "current": round(c_rate, 4),
                    "delta": round(-drop, 4),
                    "recommended_action": ACTION_HEALTH,
                })

    def _check_contracts(self, base: Dict[str, Any], cur: Dict[str, Any],
                         signals: List[Dict[str, Any]]) -> None:
        p = self.policy
        if base["tool_total"] < p.min_tool_calls \
                or cur["tool_total"] < p.min_tool_calls:
            return
        b_rate = base["contract_errors"] / base["tool_total"]
        c_rate = cur["contract_errors"] / cur["tool_total"]
        increase = c_rate - b_rate
        if increase >= p.contract_error_increase:
            signals.append({
                "kind": "interface_contract",
                "component": "tool_contracts",
                "severity": "critical",
                "detail": "工具契约错误率显著上升（接口契约可能变化）",
                "baseline": round(b_rate, 4),
                "current": round(c_rate, 4),
                "delta": round(increase, 4),
                "recommended_action": ACTION_STOP,
            })

    def _check_models(self, base: Dict[str, Any], cur: Dict[str, Any],
                      signals: List[Dict[str, Any]]) -> None:
        p = self.policy
        for model in sorted(set(base["models"]) | set(cur["models"])):
            b = base["models"].get(model) or {}
            c = cur["models"].get(model) or {}
            if b.get("total", 0) < p.min_model_calls \
                    or c.get("total", 0) < p.min_model_calls:
                continue
            b_rate = b["ok"] / b["total"]
            c_rate = c["ok"] / c["total"]
            drop = b_rate - c_rate
            b_lat = b["latency_s"] / b["total"]
            c_lat = c["latency_s"] / c["total"]
            ratio = (c_lat / b_lat) if b_lat > 0 else None
            reasons = []
            if drop >= p.model_success_drop:
                reasons.append(f"成功率下降 {round(drop, 3)}")
            if ratio is not None and ratio >= p.latency_increase_ratio:
                reasons.append(f"时延升高 x{round(ratio, 2)}")
            if reasons:
                signals.append({
                    "kind": "model_behavior",
                    "component": model,
                    "severity": "warning",
                    "detail": f"模型 {model} 行为漂移: " + ", ".join(reasons),
                    "baseline": round(b_rate, 4),
                    "current": round(c_rate, 4),
                    "latency_ratio": round(ratio, 4)
                    if ratio is not None else None,
                    "delta": round(-drop, 4),
                    "recommended_action": ACTION_EVOLUTION,
                })

    def _check_task_distribution(self, base: Dict[str, Any],
                                 cur: Dict[str, Any],
                                 signals: List[Dict[str, Any]]) -> None:
        p = self.policy
        b_tasks: Counter = base["tasks"]
        c_tasks: Counter = cur["tasks"]
        b_total, c_total = sum(b_tasks.values()), sum(c_tasks.values())
        if b_total < p.min_tasks or c_total < p.min_tasks:
            return
        intents = set(b_tasks) | set(c_tasks)
        delta = 0.5 * sum(abs(b_tasks.get(i, 0) / b_total
                              - c_tasks.get(i, 0) / c_total)
                          for i in intents)
        if delta >= p.task_distribution_delta:
            signals.append({
                "kind": "task_distribution",
                "component": "runtime",
                "severity": "warning",
                "detail": f"任务意图分布变化 {round(delta, 3)}",
                "baseline": {i: b_tasks.get(i, 0) for i in sorted(intents)},
                "current": {i: c_tasks.get(i, 0) for i in sorted(intents)},
                "delta": round(delta, 4),
                "recommended_action": ACTION_COMPAT,
            })

    def _check_versions(self, base: Dict[str, Any], cur: Dict[str, Any],
                        signals: List[Dict[str, Any]]) -> None:
        changed = []
        for component in sorted(set(base["components"]) & set(cur["components"])):
            if base["components"][component] != cur["components"][component]:
                changed.append({
                    "component": component,
                    "baseline": base["components"][component],
                    "current": cur["components"][component]})
        if changed:
            signals.append({
                "kind": "component_version",
                "component": "components",
                "severity": "warning",
                "detail": "组件版本在窗口间发生变化",
                "baseline": [c["baseline"] for c in changed],
                "current": [c["current"] for c in changed],
                "changes": changed,
                "recommended_action": ACTION_COMPAT,
            })
        dep_changes = []
        for name in sorted(set(base["dependencies"]) & set(cur["dependencies"])):
            if base["dependencies"][name] != cur["dependencies"][name]:
                dep_changes.append({
                    "dependency": name,
                    "baseline": base["dependencies"][name],
                    "current": cur["dependencies"][name]})
        if dep_changes:
            signals.append({
                "kind": "dependency_version",
                "component": "dependencies",
                "severity": "critical",
                "detail": "依赖版本在窗口间发生变化（需停止并做兼容性验证）",
                "changes": dep_changes,
                "recommended_action": ACTION_STOP,
            })

    # ---- 任务分布 ----
    def _task_distribution(self, start: float, end: float) -> Counter:
        if self.db is None:
            return Counter()
        try:
            tasks = self.db.list_tasks(limit=1000)
        except Exception:  # noqa: BLE001
            return Counter()
        return Counter(t.intent or "general" for t in tasks
                       if start <= getattr(t, "created_at", 0.0) < end)


def _window_stats(events: Iterable[RunEvent]) -> Dict[str, Any]:
    tools: Dict[str, Dict[str, int]] = {}
    models: Dict[str, Dict[str, float]] = {}
    components: Dict[str, str] = {}
    dependencies: Dict[str, str] = {}
    tool_total = contract_errors = 0
    for evt in events:
        payload = evt.payload or {}
        if evt.event_type == "tool_call":
            tool_total += 1
            tool = str(payload.get("tool") or payload.get("tool_id")
                       or "unknown")
            bucket = tools.setdefault(tool, {"total": 0, "ok": 0,
                                             "contract_errors": 0})
            bucket["total"] += 1
            if payload.get("ok"):
                bucket["ok"] += 1
            else:
                err = str(payload.get("error_class") or "")
                if "contract" in err or "invalid" in err:
                    bucket["contract_errors"] += 1
                    contract_errors += 1
        elif evt.event_type == "model_call":
            model = str(payload.get("model_id") or "unknown")
            bucket = models.setdefault(model, {"total": 0, "ok": 0,
                                               "latency_s": 0.0})
            bucket["total"] += 1
            if payload.get("ok"):
                bucket["ok"] += 1
            try:
                bucket["latency_s"] += float(payload.get("latency_s") or 0.0)
            except (TypeError, ValueError):
                pass
        elif evt.event_type == "task_created":
            deps = payload.get("dependencies")
            if isinstance(deps, dict):
                for name, version in deps.items():
                    dependencies[str(name)] = str(version)
        components[evt.component_id] = evt.component_version
    return {
        "tools": tools,
        "models": models,
        "components": components,
        "dependencies": dependencies,
        "tool_total": tool_total,
        "contract_errors": contract_errors,
    }


def _metric_summary(stats: Dict[str, Any]) -> Dict[str, Any]:
    tools = {}
    for tool, b in stats.get("tools", {}).items():
        tools[tool] = {
            "calls": b["total"],
            "success_rate": round(b["ok"] / b["total"], 4)
            if b["total"] else None,
        }
    models = {}
    for model, b in stats.get("models", {}).items():
        models[model] = {
            "calls": b["total"],
            "success_rate": round(b["ok"] / b["total"], 4)
            if b["total"] else None,
            "avg_latency_s": round(b["latency_s"] / b["total"], 4)
            if b["total"] else None,
        }
    tasks = stats.get("tasks")
    return {
        "tools": tools,
        "models": models,
        "tool_calls": stats.get("tool_total"),
        "contract_error_rate": round(
            stats.get("contract_errors", 0)
            / stats["tool_total"], 4) if stats.get("tool_total") else None,
        "tasks": dict(tasks) if isinstance(tasks, Counter) else tasks or {},
        "component_versions": dict(stats.get("components", {})),
        "dependencies": dict(stats.get("dependencies", {})),
    }
