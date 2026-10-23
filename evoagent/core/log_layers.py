"""日志分层、保留期限与访问策略（PRD 38-39 / Arch 9.2）。

四层日志各有独立存储策略：
- runtime_event：结构化运行事件，用于统计、筛选与诊断；
- execution_trace：关键执行步骤，用于还原复杂任务；
- diagnostic_evidence：经授权的工具返回/文件/界面证据；
- audit：权限、发布、回滚与重要系统操作审计。

原始日志不能自动等同于记忆或进化结论。敏感层数据不因“能读出”就默认可导出。
"""

import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# 允许进入执行轨迹的事件类型（其余事件只留在 runtime_event 层）
TRACE_EVENT_TYPES = {
    "task_created", "task_finalized", "task_failed", "state_transition",
    "tool_call", "model_call", "react_finished", "context_quality_warning",
    "task_paused", "task_resumed", "task_cancelled",
    "side_effect_state_unknown", "diagnostic_failed",
    "evolution_candidate", "evolution_evaluated", "evolution_deployed",
    "evolution_monitored", "audit",
}


@dataclass(frozen=True)
class LayerPolicy:
    layer: str
    table: str
    retention_days: float
    roles: Tuple[str, ...]
    description: str
    max_payload_chars: int = 2048

    def to_dict(self) -> Dict[str, Any]:
        return {"layer": self.layer, "table": self.table,
                "retention_days": self.retention_days,
                "roles": list(self.roles),
                "description": self.description,
                "max_payload_chars": self.max_payload_chars}


# 权限：admin 可读全部；operator 只读事件与执行轨迹
LAYER_POLICIES: Dict[str, LayerPolicy] = {
    "runtime_event": LayerPolicy(
        "runtime_event", "runtime_events", 30, ("operator", "admin"),
        "结构化运行事件：任务状态、工具调用、成本与异常", 4096),
    "execution_trace": LayerPolicy(
        "execution_trace", "execution_traces", 14, ("operator", "admin"),
        "详细执行轨迹：关键步骤与组件版本", 2048),
    "diagnostic_evidence": LayerPolicy(
        "diagnostic_evidence", "diagnostic_evidence", 7, ("admin",),
        "原始诊断证据：工具返回、文件引用与界面证据", 4096),
    "audit": LayerPolicy(
        "audit", "audit_log", 365, ("admin",),
        "进化审计：权限、发布、回滚与重要系统操作", 8192),
}


def get_policy(layer: str) -> LayerPolicy:
    return LAYER_POLICIES[layer]


def role_can_read(layer: str, role: str) -> bool:
    if role == "admin":
        return True
    policy = LAYER_POLICIES.get(layer)
    return bool(policy and role in policy.roles)


def redact_payload(layer: str, privacy: str,
                   payload: Any, max_chars: Optional[int] = None) -> Dict[str, Any]:
    """导出/落盘前的保守脱敏：仅 standard 保留内容，其余只留标记。"""
    if privacy != "standard":
        return {"redacted": True, "privacy_classification": privacy}
    limit = max_chars if max_chars is not None else \
        LAYER_POLICIES[layer].max_payload_chars
    clean = _sanitize(payload)
    text = json.dumps(clean, ensure_ascii=False, default=str)
    if len(text) > limit:
        return {"_truncated": True, "preview": text[:max(0, limit - 32)]}
    return clean if isinstance(clean, dict) else {"value": clean}


def _sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, dict):
        return {str(k)[:64]: _sanitize(v, depth + 1)
                for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, depth + 1) for v in list(value)[:20]]
    return str(value)[:200]


def _summary(evt: Any) -> str:
    kind = evt.event_type
    payload = evt.payload if isinstance(evt.payload, dict) else {}
    if kind == "tool_call":
        text = f"tool={payload.get('tool')} ok={payload.get('ok')}"
        if payload.get("error_class"):
            text += f" error={payload.get('error_class')}"
    elif kind == "model_call":
        text = (f"model={payload.get('model_id')} op={payload.get('op')} "
                f"ok={payload.get('ok')}")
    elif kind == "state_transition":
        text = f"to={payload.get('to')}"
    elif kind in ("task_failed", "diagnostic_failed"):
        text = f"reason={payload.get('reason')}"
    elif kind == "task_finalized":
        text = f"status={payload.get('status')}"
    elif kind == "context_quality_warning":
        bad = [k for k in ("overflow", "untrusted_instructions",
                           "missing_state") if payload.get(k)]
        if payload.get("constraint_retention") is not None \
                and payload.get("constraint_retention") < 1.0:
            bad.append("constraint_retention")
        text = "issues=" + ",".join(bad or ["quality"])
    elif kind == "react_finished":
        text = f"stop={payload.get('stop_reason')} steps={payload.get('steps')}"
    else:
        text = kind
    return text[:300]


class ExecutionTracer:
    """把关键事件写入执行轨迹层（Arch 9.2）。"""

    def __init__(self, bus: Any, db: Any):
        self.bus = bus
        self.db = db
        self._steps: Dict[str, int] = {}
        self._lock = threading.Lock()
        if bus is not None:
            bus.subscribe(self.record)

    def record(self, evt: Any) -> Optional[str]:
        if evt.event_type not in TRACE_EVENT_TYPES:
            return None
        task_id = evt.task_id or evt.run_id
        if not task_id:
            return None
        with self._lock:
            step = self._steps.get(task_id)
            if step is None:
                step = self.db.max_trace_step(task_id)
            step += 1
            self._steps[task_id] = step
        policy = LAYER_POLICIES["execution_trace"]
        sensitive = evt.privacy_classification != "standard"
        summary = "" if sensitive else _summary(evt)
        payload = redact_payload("execution_trace",
                                 evt.privacy_classification, evt.payload,
                                 policy.max_payload_chars)
        trace_id = "trc_" + uuid.uuid4().hex[:16]
        self.db.save_trace(
            trace_id, task_id, evt.run_id, step, evt.timestamp,
            evt.event_type, summary,
            json.dumps(payload, ensure_ascii=False, default=str),
            evt.privacy_classification)
        return trace_id


class EvidenceStore:
    """诊断证据层（PRD 39）：只有授权且 standard 的证据可导出。"""

    ALLOWED_PRIVACY = {"standard", "sensitive"}

    def __init__(self, db: Any):
        self.db = db

    def add(self, task_id: Optional[str], kind: str, ref: str,
            summary: str, privacy: str = "standard",
            authorized: bool = False) -> str:
        if privacy not in self.ALLOWED_PRIVACY:
            raise ValueError(f"证据隐私等级不可入队: {privacy}")
        evidence_id = "ev_" + uuid.uuid4().hex[:16]
        self.db.save_evidence(evidence_id, task_id, kind, ref,
                              summary[:1000], privacy, authorized)
        return evidence_id

    def list(self, task_id: Optional[str] = None,
             limit: int = 200) -> list:
        return self.db.list_evidence(task_id=task_id, limit=limit)

    @staticmethod
    def exportable(evidence: Dict[str, Any]) -> bool:
        return (evidence.get("privacy_classification") == "standard"
                and bool(evidence.get("authorized")))

    def list_exportable(self, task_id: Optional[str] = None) -> list:
        return [e for e in self.list(task_id=task_id)
                if self.exportable(e)]

    def delete(self, task_id: Optional[str] = None) -> int:
        return self.db.delete_evidence(task_id=task_id)


class LayerManager:
    """保留期限清理与按任务隐私删除（PRD 38-39）。"""

    def __init__(self, db: Any, bus: Any = None,
                 evidence: Optional[EvidenceStore] = None):
        self.db = db
        self.bus = bus
        self.evidence = evidence or EvidenceStore(db)

    def counts(self) -> Dict[str, int]:
        events = self.bus.count() if self.bus is not None else 0
        return {"runtime_event": int(events),
                "execution_trace": self.db.count_traces(),
                "diagnostic_evidence": self.db.count_evidence(),
                "audit": self.db.count_audit()}

    def stats(self, role: str = "admin") -> Dict[str, Any]:
        counts = self.counts()
        layers = []
        for layer, policy in LAYER_POLICIES.items():
            item = policy.to_dict()
            item["count"] = counts.get(layer, 0)
            item["readable"] = role_can_read(layer, role)
            layers.append(item)
        return {"layers": layers}

    def purge(self, now: Optional[float] = None) -> Dict[str, int]:
        now = time.time() if now is None else now
        deleted: Dict[str, int] = {}
        for layer, policy in LAYER_POLICIES.items():
            cutoff = now - policy.retention_days * 86400
            if layer == "runtime_event":
                n = self.bus.purge_before(cutoff) if self.bus else 0
            elif layer == "execution_trace":
                n = self.db.delete_traces(before=cutoff)
            elif layer == "diagnostic_evidence":
                n = self.db.delete_evidence(before=cutoff)
            else:
                n = self.db.purge_audit_before(cutoff)
            deleted[layer] = n
        return deleted

    def delete_task(self, task_id: str) -> Dict[str, Any]:
        """删除一个任务在各层的日志与证据；审计层只由管理员显式清理。"""
        out = {
            "runtime_event": self.bus.delete_task_events(task_id)
            if self.bus else 0,
            "execution_trace": self.db.delete_traces(task_id=task_id),
            "diagnostic_evidence": self.db.delete_evidence(task_id=task_id),
        }
        out["diagnostics"] = self.db.delete_diagnostics_for_task(task_id)
        return out

    def validate_deletion_plan(self, plan: Any) -> None:
        """Reject references that currently belong to another task before writes.

        Missing IDs are allowed for replay/retention cleanup. This preflight is
        not a global transaction with the EventBus connection.
        """
        from ..observability.export import DeletionScopeError
        if self.bus is None:
            return
        own_ids = {e.event_id for e in self.bus.query(
            task_id=plan.scope, limit=max(1, self.bus.count(plan.scope)))}
        if any(self.bus.exists(eid) and eid not in own_ids for eid in plan.event_ids):
            raise DeletionScopeError("删除计划包含其他任务的事件引用")

    def record_deletion(self, plan: Any, *, actor: str, reason: str,
                        outcome: str, detail: Dict[str, Any]) -> str:
        """Persist each intent/result as a separate audit row, without payloads."""
        audit_id = "del_" + uuid.uuid4().hex
        self.db.save_audit(audit_id, actor, "privacy_deletion", plan.scope,
                           outcome, {**detail, "executed_by": actor,
                                     "execute_reason": reason})
        return audit_id
