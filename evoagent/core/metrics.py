"""指标聚合（PRD 37-40 / 82 / Architecture 11.4, 19, 20.1）。

指标来自结构化运行事件与任务账本，不把原始日志直接当成绩效结论。
"""

import threading
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from .alerts import AlertPolicy, evaluate_alerts
from .task import TaskStatus

TERMINAL = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
EVOLUTION_PASSED = {"PROMOTED"}
EVOLUTION_FAILED = {"REJECTED", "ROLLED_BACK"}


class MetricsAggregator:
    def __init__(self, db: Any, bus: Any = None, ledger: Any = None,
                 releases: Any = None, evolution: Any = None,
                 tools: Any = None, *, started_at: Optional[float] = None,
                 logs: Any = None, drift: Any = None,
                 drift_cache_seconds: float = 60.0,
                 alert_policy: Optional[AlertPolicy] = None,
                 daily_cost_limit: Optional[float] = None):
        self.db = db
        self.bus = bus
        self.ledger = ledger
        self.releases = releases
        self.evolution = evolution
        self.tools = tools
        self.started_at = started_at or time.time()
        self.logs = logs
        self.drift = drift
        self.drift_cache_seconds = drift_cache_seconds
        self._drift_cache: Optional[Dict[str, Any]] = None
        self._drift_cached_at = 0.0
        self._drift_lock = threading.Lock()
        self.alert_policy = alert_policy or AlertPolicy()
        self.daily_cost_limit = daily_cost_limit

    def drift_report(self) -> Optional[Dict[str, Any]]:
        """短缓存漂移报告：指标轮询不应每秒重复扫描事件窗口。"""
        if self.drift is None:
            return None
        now = time.time()
        with self._drift_lock:
            if self._drift_cache is not None \
                    and now - self._drift_cached_at < self.drift_cache_seconds:
                return self._drift_cache
        try:
            report = self.drift.detect()
        except Exception:  # noqa: BLE001 —— 观测面不得因检测异常中断
            return None
        with self._drift_lock:
            self._drift_cache = report
            self._drift_cached_at = time.time()
        return report

    def snapshot(self, limit: int = 500,
                 include_drift: bool = True) -> Dict[str, Any]:
        tasks = self.db.list_tasks(limit=limit)
        status_counts = Counter(t.task_status.value for t in tasks)
        terminal = [t for t in tasks if t.task_status in TERMINAL]
        succeeded = sum(1 for t in terminal
                        if t.task_status == TaskStatus.SUCCEEDED)
        latencies: List[float] = []
        model_calls = tool_calls = 0
        cost = 0.0
        failure_by_intent: Counter = Counter()
        by_intent: Dict[str, Dict[str, Any]] = {}

        recovery_attempts = recovery_successes = 0
        context_tasks = context_overflow = 0
        retention_values: List[float] = []
        untrusted_instructions = cross_domain_candidates = 0
        memory_candidates = memory_used = dropped_memories = 0

        for t in terminal:
            latency = max(0.0, t.updated_at - t.created_at)
            latencies.append(latency)
            budget = _budget_of(t)
            usage = budget.get("usage", {}) if isinstance(budget, dict) else {}
            calls = int(usage.get("model_calls", 0))
            tcalls = int(usage.get("tool_calls", 0))
            tcost = float(usage.get("cost", 0.0))
            model_calls += calls
            tool_calls += tcalls
            cost += tcost
            if t.task_status == TaskStatus.FAILED:
                failure_by_intent[t.intent or "general"] += 1
            bucket = by_intent.setdefault(t.intent or "general", {
                "total": 0, "succeeded": 0, "failed": 0,
                "cost": 0.0, "latency_s": 0.0, "model_calls": 0,
                "tool_calls": 0})
            bucket["total"] += 1
            bucket["succeeded"] += 1 if t.task_status == TaskStatus.SUCCEEDED else 0
            bucket["failed"] += 1 if t.task_status == TaskStatus.FAILED else 0
            bucket["cost"] += tcost
            bucket["latency_s"] += latency
            bucket["model_calls"] += calls
            bucket["tool_calls"] += tcalls

            if _recovery_attempted(t):
                recovery_attempts += 1
                if t.task_status == TaskStatus.SUCCEEDED:
                    recovery_successes += 1

        for intent, b in by_intent.items():
            b["success_rate"] = round(b["succeeded"] / b["total"], 4) \
                if b["total"] else None
            b["avg_cost"] = round(b["cost"] / b["total"], 6) \
                if b["total"] else None
            b["avg_latency_s"] = round(b["latency_s"] / b["total"], 3) \
                if b["total"] else None

        for t in tasks:
            ctx = (t.runtime_manifest or {}).get("context") or {}
            if not ctx:
                continue
            context_tasks += 1
            quality = ctx.get("quality") or {}
            if quality.get("overflow"):
                context_overflow += 1
            if quality.get("constraint_retention") is not None:
                retention_values.append(
                    float(quality.get("constraint_retention")))
            untrusted_instructions += int(
                quality.get("untrusted_instructions") or 0)
            cross_domain_candidates += int(
                quality.get("cross_domain_candidates") or 0)
            memory_candidates += int(ctx.get("memory_candidates") or 0)
            memory_used += int(ctx.get("memory_used") or 0)
            dropped_memories += int(ctx.get("dropped_memories") or 0)

        consecutive_failures = 0
        for t in sorted(terminal, key=lambda x: x.updated_at, reverse=True):
            if t.task_status == TaskStatus.FAILED:
                consecutive_failures += 1
            else:
                break

        candidates = Counter(c.status.value for c in
                             (self.evolution.all_candidates()
                              if self.evolution is not None else []))
        decided = sum(candidates.get(s, 0)
                      for s in EVOLUTION_PASSED | EVOLUTION_FAILED)
        passed = sum(candidates.get(s, 0) for s in EVOLUTION_PASSED)

        deployments = len(self.releases.deployments) \
            if self.releases is not None else 0
        rollbacks = len(self.releases.rollback_records) \
            if self.releases is not None else 0

        latest_memory_eval = _latest_memory_eval(self.db)
        event_counts = self.bus.counts() if self.bus is not None else {}
        event_agg = self.bus.aggregate() if self.bus is not None else {}
        tool_stats = event_agg.get("tool_calls", {}) if event_agg else {}
        model_stats = event_agg.get("model_calls", {}) if event_agg else {}
        react_stats = event_agg.get("react", {}) if event_agg else {}
        security_incidents = event_agg.get("security_incidents", 0) \
            if event_agg else 0

        snap: Dict[str, Any] = {
            "environment": {
                "healthy": True,
                "uptime_s": round(time.time() - self.started_at, 3),
                "started_at": self.started_at,
            },
            "tasks": {
                "total": len(tasks),
                "by_status": dict(status_counts),
                "terminal": len(terminal),
                "success_rate": round(succeeded / len(terminal), 4)
                if terminal else None,
                "avg_latency_s": round(sum(latencies) / len(latencies), 3)
                if latencies else None,
                "avg_cost": round(cost / len(terminal), 6)
                if terminal else None,
                "consecutive_failures": consecutive_failures,
                "failure_by_intent": dict(failure_by_intent),
                "by_intent": by_intent,
                "model_calls": model_calls,
                "tool_calls": tool_calls,
                "cost": round(cost, 4),
            },
            "model_calls": model_stats,
            "tools": {
                "calls": tool_stats.get("total", 0),
                "ok": tool_stats.get("ok", 0),
                "failed": tool_stats.get("failed", 0),
                "contract_errors": tool_stats.get("contract_errors", 0),
                "contract_error_rate": tool_stats.get("contract_error_rate"),
                "error_classes": tool_stats.get("error_classes", {}),
            },
            "react": react_stats,
            "recovery": {
                "attempts": recovery_attempts,
                "successes": recovery_successes,
                "success_rate": round(recovery_successes / recovery_attempts, 4)
                if recovery_attempts else None,
            },
            "context": {
                "tasks": context_tasks,
                "overflow": context_overflow,
                "overflow_rate": round(context_overflow / context_tasks, 4)
                if context_tasks else None,
                "constraint_retention_avg": round(
                    sum(retention_values) / len(retention_values), 4)
                if retention_values else None,
                "key_constraint_omission_rate": round(
                    1.0 - sum(retention_values) / len(retention_values), 4)
                if retention_values else None,
                "untrusted_instructions": untrusted_instructions,
                "cross_domain_candidates": cross_domain_candidates,
            },
            "memory": {
                "candidates": memory_candidates,
                "used": memory_used,
                "hit_rate": round(memory_used / memory_candidates, 4)
                if memory_candidates else None,
                "dropped": dropped_memories,
                "evaluation": latest_memory_eval,
            },
            "events": {
                **event_counts,
                "security_incidents": security_incidents,
                "context_warnings": event_agg.get("context_warnings", 0)
                if event_agg else 0,
            },
            "diagnostics": {
                "cases": len(self.db.list_diagnostic_cases(limit=1000)),
            },
            "evolution": {
                "candidates": dict(candidates),
                "datasets": sorted(getattr(self.evolution, "datasets", {}) or {}),
                "decided": decided,
                "pass_rate": round(passed / decided, 4) if decided else None,
            },
            "budget": {
                "runtime_day": self.ledger.usage("runtime", "day")
                if self.ledger is not None else {},
                "runtime_month": self.ledger.usage("runtime", "month")
                if self.ledger is not None else {},
                "evolution_day": self.ledger.usage("evolution", "day")
                if self.ledger is not None else {},
                "daily_cost_limit": self.daily_cost_limit,
            },
            "releases": {
                "current": self.releases.current_release_id
                if self.releases is not None else None,
                "count": len(self.releases.manifests)
                if self.releases is not None else 0,
                "deployments": deployments,
                "rollbacks": rollbacks,
                "rollback_rate": round(rollbacks / deployments, 4)
                if deployments else None,
            },
            "tools_health": self.tools.health() if self.tools is not None else {},
            "logs": self.logs.counts() if self.logs is not None else {},
        }
        if include_drift:
            report = self.drift_report()
            if report is not None:
                snap["drift"] = report
        snap["alerts"] = evaluate_alerts(
            snap, self.alert_policy, self.daily_cost_limit)
        return snap

    def prometheus(self, limit: int = 500) -> str:
        snap = self.snapshot(limit=limit)
        lines: List[str] = []
        declared = set()

        def add(name: str, value: Any, labels: str = "",
                kind: str = "gauge") -> None:
            # 无样本时输出 NaN 而非省略，保证序列稳定可供告警规则引用
            if value is None:
                value = "NaN"
            if name not in declared:
                lines.append(f"# TYPE {name} {kind}")
                declared.add(name)
            suffix = f"{{{labels}}}" if labels else ""
            lines.append(f"{name}{suffix} {value}")

        add("evoagent_uptime_seconds", snap["environment"]["uptime_s"])
        add("evoagent_tasks_total", snap["tasks"]["total"])
        for status, n in snap["tasks"]["by_status"].items():
            add("evoagent_tasks_by_status", n, f'status="{status}"')
        add("evoagent_task_success_rate", snap["tasks"]["success_rate"])
        add("evoagent_task_avg_latency_seconds", snap["tasks"]["avg_latency_s"])
        add("evoagent_task_avg_cost", snap["tasks"]["avg_cost"])
        add("evoagent_consecutive_failures",
            snap["tasks"]["consecutive_failures"])
        add("evoagent_model_calls_total", snap["tasks"]["model_calls"])
        add("evoagent_tool_calls_total", snap["tasks"]["tool_calls"])
        add("evoagent_cost_total", snap["tasks"]["cost"])
        for intent, bucket in snap["tasks"]["by_intent"].items():
            add("evoagent_task_success_rate_by_intent",
                bucket["success_rate"], f'intent="{intent}"')
            add("evoagent_task_avg_cost_by_intent",
                bucket["avg_cost"], f'intent="{intent}"')
        for intent, n in snap["tasks"]["failure_by_intent"].items():
            add("evoagent_task_failures_by_intent", n, f'intent="{intent}"')

        models = snap["model_calls"]
        add("evoagent_model_call_success_rate", models.get("success_rate"))
        add("evoagent_model_call_failures_total", models.get("failed"))
        for model, bucket in models.get("by_model", {}).items():
            add("evoagent_model_call_success_rate_by_model",
                round(bucket["ok"] / bucket["total"], 4)
                if bucket["total"] else None, f'model="{model}"')
        add("evoagent_tool_contract_error_rate",
            snap["tools"].get("contract_error_rate"))
        add("evoagent_tool_failures_total", snap["tools"].get("failed"))
        add("evoagent_react_runs_total", snap["react"].get("runs"))
        add("evoagent_react_anomaly_rate", snap["react"].get("anomaly_rate"))
        drift = snap.get("drift") or {}
        add("evoagent_drift_detected",
            1 if drift.get("status") == "drifting" else 0)
        add("evoagent_drift_signals", drift.get("signal_count") or 0)
        for kind, n in Counter(s.get("kind") for s in
                               (drift.get("signals") or [])).items():
            add("evoagent_drift_signals_by_kind", n, f'kind="{kind}"')
        for action in drift.get("recommended_actions") or []:
            add("evoagent_drift_recommended_action", 1,
                f'action="{action}"')
        add("evoagent_recovery_success_rate",
            snap["recovery"].get("success_rate"))
        add("evoagent_context_overflow_rate",
            snap["context"].get("overflow_rate"))
        add("evoagent_memory_hit_rate", snap["memory"].get("hit_rate"))
        memory_eval = snap["memory"].get("evaluation") or {}
        add("evoagent_memory_eval_recall", memory_eval.get("recall"))
        add("evoagent_memory_eval_precision", memory_eval.get("precision"))
        add("evoagent_memory_eval_expired_misuse_rate",
            memory_eval.get("expired_misuse_rate"))
        add("evoagent_memory_eval_conflict_correctness",
            memory_eval.get("conflict_correctness"))
        add("evoagent_evolution_pass_rate",
            snap["evolution"].get("pass_rate"))
        add("evoagent_release_rollback_rate",
            snap["releases"].get("rollback_rate"))
        add("evoagent_security_incidents",
            snap["events"].get("security_incidents"))
        for layer, n in snap.get("logs", {}).items():
            add("evoagent_log_layer_items", n, f'layer="{layer}"')
        for status, n in snap["evolution"]["candidates"].items():
            add("evoagent_evolution_candidates", n, f'status="{status}"')
        for scope, usage in snap["budget"].items():
            if not isinstance(usage, dict):
                continue
            for metric, value in usage.items():
                add(f"evoagent_budget_{metric}", value, f'scope="{scope}"')
        for sev, n in snap.get("events", {}).get("by_severity", {}).items():
            add("evoagent_events_total", n, f'severity="{sev}"')
        for alert in snap.get("alerts", []):
            add("evoagent_alerts", 1,
                f'rule="{alert["rule"]}",severity="{alert["severity"]}"')
        return "\n".join(lines) + "\n"


def _latest_memory_eval(db: Any) -> Optional[Dict[str, Any]]:
    """最近一次记忆评测（PRD 28）；无报告时返回 None，不伪造指标。"""
    fn = getattr(db, "list_memory_eval_reports", None)
    if fn is None:
        return None
    try:
        rows = fn(limit=1)
    except Exception:  # noqa: BLE001 —— 观测面不得因存储异常中断
        return None
    if not rows:
        return None
    row = rows[0]
    data = row.get("data") or {}
    runtime = data.get("runtime_feedback") or {}
    return {
        "report_id": row.get("report_id"),
        "dataset_id": row.get("dataset_id"),
        "version": row.get("version"),
        "created_at": row.get("created_at"),
        "case_count": data.get("case_count"),
        "write_accuracy": data.get("write_accuracy"),
        "recall": data.get("recall"),
        "precision": data.get("precision"),
        "mrr": data.get("mrr"),
        "expired_misuse_rate": data.get("expired_misuse_rate"),
        "conflict_correctness": data.get("conflict_correctness"),
        "memory_benefit": runtime.get("memory_benefit"),
        "memory_caused_error_rate": runtime.get("memory_caused_error_rate"),
    }


def _budget_of(task: Any) -> Dict[str, Any]:
    for ev in reversed(task.result_evidence or []):
        if isinstance(ev, dict) and isinstance(ev.get("budget"), dict):
            return ev["budget"]
    return {}


def _recovery_attempted(task: Any) -> bool:
    if (task.runtime_manifest or {}).get("resumed_from"):
        return True
    return any(isinstance(ev, dict) and ev.get("recovery_attempted")
               for ev in (task.result_evidence or []))
