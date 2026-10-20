"""指标聚合（PRD 37-40 / 82 / Architecture 11.4, 19）。

指标来自结构化运行事件与任务账本，不把原始日志直接当成绩效结论。
"""

from collections import Counter
from typing import Any, Dict, List

from .task import TaskStatus

TERMINAL = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


class MetricsAggregator:
    def __init__(self, db: Any, bus: Any = None, ledger: Any = None,
                 releases: Any = None, evolution: Any = None,
                 tools: Any = None):
        self.db = db
        self.bus = bus
        self.ledger = ledger
        self.releases = releases
        self.evolution = evolution
        self.tools = tools

    def snapshot(self, limit: int = 500) -> Dict[str, Any]:
        tasks = self.db.list_tasks(limit=limit)
        status_counts = Counter(t.task_status.value for t in tasks)
        terminal = [t for t in tasks if t.task_status in TERMINAL]
        succeeded = sum(1 for t in terminal
                        if t.task_status == TaskStatus.SUCCEEDED)
        latencies: List[float] = []
        model_calls = tool_calls = 0
        cost = 0.0
        failure_by_intent: Counter = Counter()
        for t in terminal:
            latencies.append(max(0.0, t.updated_at - t.created_at))
            budget = _budget_of(t)
            usage = budget.get("usage", {}) if isinstance(budget, dict) else {}
            model_calls += int(usage.get("model_calls", 0))
            tool_calls += int(usage.get("tool_calls", 0))
            cost += float(usage.get("cost", 0.0))
            if t.task_status == TaskStatus.FAILED:
                failure_by_intent[t.intent or "general"] += 1
        candidates = Counter(c.status.value for c in
                             (self.evolution.all_candidates()
                              if self.evolution is not None else []))
        return {
            "tasks": {
                "total": len(tasks),
                "by_status": dict(status_counts),
                "terminal": len(terminal),
                "success_rate": round(succeeded / len(terminal), 4)
                if terminal else None,
                "avg_latency_s": round(sum(latencies) / len(latencies), 3)
                if latencies else None,
                "failure_by_intent": dict(failure_by_intent),
                "model_calls": model_calls,
                "tool_calls": tool_calls,
                "cost": round(cost, 4),
            },
            "events": self.bus.counts() if self.bus is not None else {},
            "diagnostics": {
                "cases": len(self.db.list_diagnostic_cases(limit=1000)),
            },
            "evolution": {
                "candidates": dict(candidates),
                "datasets": sorted(getattr(self.evolution, "datasets", {}) or {}),
            },
            "budget": {
                "runtime_day": self.ledger.usage("runtime", "day")
                if self.ledger is not None else {},
                "runtime_month": self.ledger.usage("runtime", "month")
                if self.ledger is not None else {},
                "evolution_day": self.ledger.usage("evolution", "day")
                if self.ledger is not None else {},
            },
            "releases": {
                "current": self.releases.current_release_id
                if self.releases is not None else None,
                "count": len(self.releases.manifests)
                if self.releases is not None else 0,
                "deployments": len(self.releases.deployments)
                if self.releases is not None else 0,
                "rollbacks": len(self.releases.rollback_records)
                if self.releases is not None else 0,
            },
            "tools_health": self.tools.health() if self.tools is not None else {},
        }

    def prometheus(self, limit: int = 500) -> str:
        snap = self.snapshot(limit=limit)
        lines: List[str] = []
        declared = set()

        def add(name: str, value: Any, labels: str = "",
                kind: str = "gauge") -> None:
            if value is None:
                return
            if name not in declared:
                lines.append(f"# TYPE {name} {kind}")
                declared.add(name)
            suffix = f"{{{labels}}}" if labels else ""
            lines.append(f"{name}{suffix} {value}")

        add("evoagent_tasks_total", snap["tasks"]["total"])
        for status, n in snap["tasks"]["by_status"].items():
            add("evoagent_tasks_by_status", n, f'status="{status}"')
        add("evoagent_task_success_rate", snap["tasks"]["success_rate"])
        add("evoagent_task_avg_latency_seconds", snap["tasks"]["avg_latency_s"])
        add("evoagent_model_calls_total", snap["tasks"]["model_calls"])
        add("evoagent_tool_calls_total", snap["tasks"]["tool_calls"])
        add("evoagent_cost_total", snap["tasks"]["cost"])
        for intent, n in snap["tasks"]["failure_by_intent"].items():
            add("evoagent_task_failures_by_intent", n, f'intent="{intent}"')
        for status, n in snap["evolution"]["candidates"].items():
            add("evoagent_evolution_candidates", n, f'status="{status}"')
        for scope, usage in snap["budget"].items():
            for metric, value in usage.items():
                add(f"evoagent_budget_{metric}", value, f'scope="{scope}"')
        for sev, n in snap.get("events", {}).get("by_severity", {}).items():
            add("evoagent_events_total", n, f'severity="{sev}"')
        return "\n".join(lines) + "\n"


def _budget_of(task: Any) -> Dict[str, Any]:
    for ev in reversed(task.result_evidence or []):
        if isinstance(ev, dict) and isinstance(ev.get("budget"), dict):
            return ev["budget"]
    return {}
