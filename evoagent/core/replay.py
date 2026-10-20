"""任务运行回放（Arch 20.3）。

只从持久化的事件、执行轨迹、检查点与版本清单重建任务过程；
回放是只读的，绝不重新执行工具或任何真实外部副作用。
非 `standard` 隐私级别的载荷一律脱敏，只保留事件类型与时间。
"""

import time
from typing import Any, Dict, List

REDACTION_REASON = "privacy_classification"


class ReplayService:
    def __init__(self, db, bus, releases=None):
        self.db = db
        self.bus = bus
        self.releases = releases

    def build(self, task_id: str, *,
              max_items: int = 2000) -> Dict[str, Any]:
        task = self.db.get_task(task_id)
        if task is None:
            raise KeyError("任务不存在")

        events = self.bus.query(task_id=task_id, limit=max_items)
        traces = self.db.list_traces(task_id=task_id, limit=max_items)
        runs = self.db.list_runs(task_id=task_id)
        checkpoint = self.db.get_latest_checkpoint(task_id)

        redacted = 0
        timeline: List[Dict[str, Any]] = []
        for e in events:
            privacy = e.privacy_classification or "standard"
            payload = e.payload
            if privacy != "standard":
                payload = {"redacted": True, "reason": REDACTION_REASON,
                           "privacy_classification": privacy}
                redacted += 1
            timeline.append({
                "source": "event", "at": e.timestamp,
                "kind": e.event_type, "severity": e.severity,
                "component_id": e.component_id,
                "component_version": e.component_version,
                "correlation_id": e.correlation_id,
                "causation_id": e.causation_id,
                "privacy_classification": privacy, "payload": payload,
            })
        for t in traces:
            privacy = t.get("privacy_classification") or "standard"
            payload = t.get("payload") or {}
            if privacy != "standard":
                payload = {"redacted": True, "reason": REDACTION_REASON,
                           "privacy_classification": privacy}
                redacted += 1
            timeline.append({
                "source": "trace", "at": t.get("at") or 0.0,
                "kind": t.get("kind") or "", "step": t.get("step") or 0,
                "summary": t.get("summary") or "",
                "privacy_classification": privacy, "payload": payload,
            })
        timeline.sort(key=lambda x: (x["at"], x.get("step") or 0))

        manifest = task.runtime_manifest or {}
        versions = {
            "task": task.version_manifest or {},
            "release_id": (self.releases.current_release_id
                           if self.releases else None),
            "model_route": manifest.get("model_route"),
        }
        warnings = ["回放为只读重建，不会重新执行工具或外部副作用"]
        if redacted:
            warnings.append(f"{redacted} 条非 standard 载荷已脱敏")
        return {
            "task_id": task.task_id,
            "user_goal": task.user_goal,
            "task_status": task.task_status.value,
            "intent": task.intent,
            "domain": task.domain,
            "risk_level": task.risk_level.value,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "result": task.result,
            "failure_reason": task.failure_reason,
            "read_only": True,
            "reexecute_side_effects": False,
            "versions": versions,
            "plan": {
                "execution_plan": manifest.get("execution_plan"),
                "plan_progress": manifest.get("plan_progress"),
            },
            "checkpoint": checkpoint,
            "runs": runs,
            "timeline": timeline,
            "stats": {
                "events": len(events),
                "traces": len(traces),
                "timeline": len(timeline),
                "redacted": redacted,
                "generated_at": time.time(),
            },
            "warnings": warnings,
        }
