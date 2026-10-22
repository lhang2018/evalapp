"""证据最小化导出、外部内容来源标记与删除计划（ALN-16）。

导出默认不携带 payload 正文；角色与隐私等级决定可见范围，秘密模式
强制抹除。外部内容一律保留不可信来源，删除只产出审计墓碑与引用清单。
"""

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .contract import (EvidenceEvent, PRIVACY_LEVELS, as_evidence_event,
                       canonical_digest)
from .credentials import scrub_text, scrub_value

EXPORT_SCHEMA = "evoagent.observability.export/1"
LEVEL_ORDER = {level: index for index, level in enumerate(PRIVACY_LEVELS)}
EXPORTABLE_CAP = {"operator": "internal", "admin": "restricted"}


class ExportDenied(PermissionError):
    pass


@dataclass(frozen=True)
class ExternalContent:
    content: str
    origin: str
    unverified: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"content": scrub_text(self.content), "origin": self.origin,
                "trust": "untrusted", "instruction_authority": False,
                "unverified": self.unverified}


def wrap_external(content: str, origin: str) -> ExternalContent:
    """外部网页/文件/工具返回/诊断包统一打不可信来源标记。"""
    if not origin:
        raise ValueError("外部内容必须保留来源")
    if not content:
        raise ValueError("外部内容不能为空")
    return ExternalContent(content=content, origin=origin)


@dataclass(frozen=True)
class ExportPolicy:
    role: str = "operator"
    allow_sensitive: bool = False
    allow_restricted: bool = False
    include_payload_refs: bool = True

    def __post_init__(self) -> None:
        if self.role not in EXPORTABLE_CAP:
            raise ValueError(f"未知角色: {self.role}")
        if self.role != "admin" and (self.allow_sensitive
                                     or self.allow_restricted):
            raise ValueError("只有 admin 可以放宽敏感等级")

    def max_level(self) -> str:
        if self.role == "admin":
            if self.allow_restricted:
                return "restricted"
            if self.allow_sensitive:
                return "sensitive"
        return "internal"

    def allows(self, level: str) -> bool:
        return LEVEL_ORDER[level] <= LEVEL_ORDER[self.max_level()]


class EvidenceExporter:
    def __init__(self, policy: Optional[ExportPolicy] = None) -> None:
        self.policy = policy or ExportPolicy()

    def export(self, events: Iterable[Any], *, actor: str, reason: str,
               task_id: Optional[str] = None,
               now: Optional[float] = None) -> Dict[str, Any]:
        if not actor or not reason:
            raise ExportDenied("导出必须记录 actor 与 reason")
        normalized = [as_evidence_event(item) for item in events]
        if task_id:
            normalized = [item for item in normalized
                          if item.refs.task_id == task_id]

        exported: List[Dict[str, Any]] = []
        skipped = {"privacy": 0, "scope": 0}
        for event in normalized:
            if not self.policy.allows(event.privacy_level):
                skipped["privacy"] += 1
                continue
            exported.append(self._minimize(event))
        exported.sort(key=lambda item: (item["timestamp"], item["event_id"]))

        counts: Dict[str, int] = {}
        for item in exported:
            counts[item["privacy_level"]] = counts.get(
                item["privacy_level"], 0) + 1
        digest = canonical_digest(exported)
        return {
            "schema_version": EXPORT_SCHEMA,
            "actor": actor,
            "reason": reason,
            "created_at": time.time() if now is None else now,
            "task_id": task_id or "",
            "policy": {"role": self.policy.role,
                       "max_level": self.policy.max_level()},
            "events": exported,
            "counts": counts,
            "skipped": skipped,
            "events_digest": digest,
        }

    def _minimize(self, event: EvidenceEvent) -> Dict[str, Any]:
        item = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "timestamp": event.timestamp,
            "severity": event.severity,
            "privacy_level": event.privacy_level,
            "fault_level": event.fault_level,
            "origin": event.origin,
            "trust": event.trust,
            "refs": event.refs.to_dict(),
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
            "sequence_no": event.sequence_no,
        }
        if event.trust == "untrusted":
            item["untrusted_origin"] = event.untrusted_origin
        if self.policy.include_payload_refs and event.payload_ref:
            item["payload_ref"] = event.payload_ref
        return scrub_value(item)


@dataclass(frozen=True)
class DeletionPlan:
    scope: str
    actor: str
    reason: str
    event_ids: Tuple[str, ...]
    digest: str
    tombstone: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "evoagent.observability.deletion/1",
            "scope": self.scope,
            "actor": self.actor,
            "reason": self.reason,
            "event_ids": list(self.event_ids),
            "digest": self.digest,
            "tombstone": dict(self.tombstone),
        }


def plan_deletion(events: Iterable[Any], *, task_id: str, actor: str,
                  reason: str, now: Optional[float] = None) -> DeletionPlan:
    if not task_id or not actor or not reason:
        raise ValueError("删除必须限定 task 并记录 actor 与 reason")
    ids = []
    for item in events:
        event = as_evidence_event(item)
        if event.refs.task_id == task_id:
            ids.append(event.event_id)
    ids.sort()
    digest = canonical_digest(ids)
    return DeletionPlan(
        scope=task_id, actor=actor, reason=reason,
        event_ids=tuple(ids), digest=digest,
        tombstone={"root_task_id": task_id, "actor": actor,
                   "reason": reason, "deleted_count": len(ids),
                   "digest": digest,
                   "at": time.time() if now is None else now})
