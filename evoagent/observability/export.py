"""证据最小化导出、外部内容来源标记与删除计划（ALN-16）。

导出默认不携带 payload 正文；角色与隐私等级决定可见范围，秘密模式
强制抹除。外部内容一律保留不可信来源；删除计划可由显式执行器按任务执行。
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


def export_to_file(exporter: EvidenceExporter, events: Iterable[Any], path: str,
                   *, actor: str, reason: str, task_id: Optional[str] = None,
                   allowed_root: Optional[str] = None,
                   now: Optional[float] = None,
                   audit_log: Any = None) -> Dict[str, Any]:
    """把导出报告写入文件（含审计头），文件哈希入审计；路径逃逸即拒绝。

    路径约束：绝对路径或相对 ``allowed_root``（默认当前工作目录）解析后
    必须仍落在根内——``..`` 或 symlink 逃逸均拒绝（fail-closed）。
    目标排他创建，已有文件（含硬链接）拒绝覆盖。宿主应使用私有导出目录；
    路径预检不提供对抗并发恶意替换父目录的文件系统隔离。
    audit_log 可显式传入持有 Database 的 AuditLog 以持久化文件审计。
    """
    import json
    import os

    if audit_log is not None and getattr(audit_log, "db", None) is None:
        raise ExportDenied("导出审计未配置持久存储")
    report = exporter.export(events, actor=actor, reason=reason,
                             task_id=task_id, now=now)
    root = os.path.realpath(allowed_root) if allowed_root \
        else os.path.realpath(os.getcwd())
    target = os.path.realpath(os.path.join(root, path))
    try:
        within_root = os.path.commonpath([root, target]) == root
    except ValueError:  # Different Windows drives have no common path.
        within_root = False
    if not within_root:
        raise ExportDenied(f"导出路径越出授权根目录: {path}")
    body = json.dumps(report, ensure_ascii=False, indent=1,
                      sort_keys=True).encode("utf-8")
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        raise ExportDenied("导出目标已存在，请使用新文件名") from exc
    with os.fdopen(fd, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    import hashlib
    file_digest = hashlib.sha256(body).hexdigest()
    audit = {
        "kind": "export_download",
        "actor": actor, "reason": reason,
        "task_id": task_id or "",
        "path": target, "sha256": file_digest,
        "events_digest": report["events_digest"],
        "count": len(report["events"]),
        "at": report["created_at"],
    }
    audit_id = None
    if audit_log is not None:
        audit_id = audit_log.record(actor, "export_download", target=task_id or "",
                                    detail=audit)
    return {"report": report, "file": target,
            "sha256": file_digest, "audit": audit,
            "audit_id": audit_id, "audit_persisted": audit_id is not None}


def execute_deletion(deleter, plan: DeletionPlan, *, actor: str,
                     reason: str,
                     verify_digest: bool = True) -> Dict[str, Any]:
    """按删除计划实际执行逐层删除（fail-closed + 幂等，ALN-16-X）。

    ``deleter`` 须提供 ``delete_task(task_id)``，支持嵌套的逐层计数。
    ``core/log_layers.py::LayerManager`` 还持久化 started/ok/failed 审计。
    兼容无审计存储的自定义删除器时显式返回 audit_persisted=False。

    fail-closed：
    - 计划的 digest 与 event_ids 重算不一致 → 拒绝，不删任何数据；
    - actor/reason 缺失 → 拒绝；
    - scope 必须与原计划墓碑的任务/actor/reason/摘要/数量一致；
    - verify_digest 为兼容参数，不再允许跳过校验。
    删除语义是整个 task 的日志/证据，包括事件快照为空时的诊断证据。
    这是摘要一致性校验，不替代调用方的授权，也不是签名凭证。
    多层存储无跨连接原子事务；部分失败保留审计并抛出，不返回假成功。
    """
    import time as _time
    if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
        raise DeletionScopeError("执行删除必须记录 actor 与 reason")
    if not isinstance(plan.scope, str) or not plan.scope.strip():
        raise DeletionScopeError("删除计划缺少 task 范围")
    digest = canonical_digest(list(plan.event_ids))
    if digest != plan.digest:
        raise DeletionScopeError("删除计划 digest 校验失败，拒绝执行")
    tombstone = dict(plan.tombstone)
    expected = {"root_task_id": plan.scope, "actor": plan.actor,
                "reason": plan.reason, "digest": plan.digest,
                "deleted_count": len(plan.event_ids)}
    if any(tombstone.get(k) != v for k, v in expected.items()):
        raise DeletionScopeError("删除计划范围与墓碑不一致，拒绝执行")
    validator = getattr(deleter, "validate_deletion_plan", None)
    if callable(validator):
        validator(plan)
    recorder = getattr(deleter, "record_deletion", None)
    audit_ids = []

    def record(outcome, detail):
        if callable(recorder):
            audit_ids.append(recorder(plan, actor=actor, reason=reason,
                                      outcome=outcome, detail=detail))

    # Refuse to start a destructive operation if its durable intent cannot be saved.
    record("started", tombstone)
    try:
        counts = deleter.delete_task(plan.scope)
        total = _deletion_count(counts)
    except Exception as exc:
        try:
            record("failed", {**tombstone, "error_type": type(exc).__name__,
                              "partial_deletion_possible": True})
        except Exception:
            pass  # Keep the original error; durable started receipt remains.
        raise
    tombstone["executed_at"] = _time.time()
    tombstone["executed_by"] = actor
    tombstone["execute_reason"] = reason
    tombstone["executed_counts"] = dict(counts)
    tombstone["executed_total"] = total
    record("ok", tombstone)
    return {
        "ok": True,
        "scope": plan.scope,
        "counts": dict(counts),
        "total": total,
        "tombstone": tombstone,
        "digest": plan.digest,
        "audit_persisted": callable(recorder),
        "audit_ids": audit_ids,
    }


def _deletion_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_deletion_count(item) for item in value.values())
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("删除计数必须是非负整数或逐层计数字典")
    return value
