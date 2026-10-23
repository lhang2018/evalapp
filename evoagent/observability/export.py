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


# ---------------------------------------------------------------- 实际执行（ALN-16-X）

class DeletionScopeError(ValueError):
    """删除计划与执行范围不一致（digest 不匹配/越 task），fail-closed。"""


def _file_sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def export_to_file(exporter: EvidenceExporter, events: Iterable[Any], path: str,
                   *, actor: str, reason: str, task_id: Optional[str] = None,
                   allowed_root: Optional[str] = None,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """把导出报告写入文件（含审计头），文件哈希入审计；路径逃逸即拒绝。

    路径约束：绝对路径或相对 ``allowed_root``（默认当前工作目录）解析后
    必须仍落在根内——``..`` 或 symlink 逃逸均拒绝（fail-closed）。
    """
    import json
    import os

    report = exporter.export(events, actor=actor, reason=reason,
                             task_id=task_id, now=now)
    root = os.path.realpath(allowed_root) if allowed_root \
        else os.path.realpath(os.getcwd())
    target = os.path.realpath(os.path.join(root, path))
    if os.path.commonpath([root, target]) != root:
        raise ExportDenied(f"导出路径越出授权根目录: {path}")
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(report, ensure_ascii=False, indent=1,
                           sort_keys=True))
    file_digest = _file_sha256(target)
    audit = {
        "kind": "export_download",
        "actor": actor, "reason": reason,
        "task_id": task_id or "",
        "path": target, "sha256": file_digest,
        "events_digest": report["events_digest"],
        "count": len(report["events"]),
        "at": report["created_at"],
    }
    return {"report": report, "file": target,
            "sha256": file_digest, "audit": audit}


def execute_deletion(deleter, plan: DeletionPlan, *, actor: str,
                     reason: str,
                     verify_digest: bool = True) -> Dict[str, Any]:
    """按删除计划实际执行逐层删除（fail-closed + 幂等，ALN-16-X）。

    ``deleter`` 须提供 ``delete_task(task_id) -> Dict[str, int]``
    （``core/log_layers.py::LogLayers.delete_task`` 即满足）。

    fail-closed：
    - 计划的 digest 与 event_ids 重算不一致 → 拒绝，不删任何数据；
    - actor/reason 缺失 → 拒绝；
    - 范围必须仍是单一 task（scope 非空）。
    幂等：同一计划二次执行时已删数据计数为 0，不算失败。
    """
    import time as _time
    if not actor or not reason:
        raise DeletionScopeError("执行删除必须记录 actor 与 reason")
    if not plan.scope:
        raise DeletionScopeError("删除计划缺少 task 范围")
    if verify_digest:
        digest = canonical_digest(list(plan.event_ids))
        if digest != plan.digest:
            raise DeletionScopeError(
                "删除计划 digest 校验失败——计划可能被篡改，拒绝执行")
    counts = deleter.delete_task(plan.scope) if plan.event_ids \
        else {k: 0 for k in
              ("runtime_event", "execution_trace", "diagnostic_evidence",
               "diagnostics")}
    total = sum(int(v) for v in counts.values())
    tombstone = dict(plan.tombstone)
    tombstone["executed_at"] = _time.time()
    tombstone["executed_by"] = actor
    tombstone["execute_reason"] = reason
    tombstone["executed_counts"] = dict(counts)
    tombstone["executed_total"] = total
    return {
        "ok": True,
        "scope": plan.scope,
        "counts": dict(counts),
        "total": total,
        "tombstone": tombstone,
        "digest": plan.digest,
    }
