"""业务仓储 → EvidenceEvent 装配层（ALN-16-P）。

16-A 交付了 EvidenceEvent 契约、16-X 交付了导出与删除执行，但业务
数据（运行事件/审计/副作用账本/审批）一直没有以证据事件形态接入
观测链——契约悬空。本模块把四类业务行投影为 EvidenceEvent 并聚合，
使 export / build_decision_chain / plan_deletion 首次消费真实数据。

身份与隐私规则（验收：受信身份隔离 + 最小证据与脱敏）：
- 服务端权威表（runtime_events / audit_log / task_side_effects /
  approvals）投影为 origin=server、trust=trusted；
- 外部导入内容（payload 携带 untrusted 标记或来源诊断包）必须保留
  untrusted_origin，绝不提升为 trusted；
- 敏感载荷（设备动作目标/参数、审批动作摘要）投影为 restricted 且
  redacted=True，只保留引用字段与计数，不复制正文；
- 全部投影过 assert_context_safe（凭证不进证据）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from .contract import (
    EventRefs,
    to_four_level,
    EvidenceContractError,
    EvidenceEvent,
    as_evidence_event,
    infer_fault_level,
)
from .credentials import assert_context_safe

#: 投影时视为敏感（restricted + 只留引用）的事件类型/表来源。
_RESTRICTED_EVENT_TYPES = {"device_action", "send_message", "payment"}


class EvidenceRepositoryError(RuntimeError):
    """A configured evidence store could not be read; never an empty result."""


def _refs_from_row(row: Dict[str, Any], **extra: str) -> EventRefs:
    values = {name: str(row.get(name) or "") for name in
              ("task_id", "run_id", "node_run_id", "effect_id",
               "approval_id", "diagnostic_id", "candidate_id",
               "release_id")}
    values.update({k: v for k, v in extra.items() if v})
    return EventRefs(**values)


def project_runtime_event(row: Dict[str, Any]) -> EvidenceEvent:
    """runtime_events 行 → EvidenceEvent（列名与契约同源，直传为主）。

    行内 refs 为 JSON 字符串（events 落库形态）；外部导入内容行
    （trust=untrusted）必须带 untrusted_origin，缺失即拒绝——受信
    身份隔离不因投影而放宽。
    """
    refs_raw = row.get("refs") or {}
    if isinstance(refs_raw, str):
        refs_raw = json.loads(refs_raw or "{}")
    refs = EventRefs(**{k: str(v) for k, v in (refs_raw or {}).items()
                        if k in EventRefs.FIELDS and v})
    if not refs.task_id:
        refs = EventRefs(task_id=str(row.get("task_id") or ""),
                         run_id=str(row.get("run_id") or ""),
                         node_run_id=refs.node_run_id,
                         effect_id=refs.effect_id,
                         approval_id=refs.approval_id,
                         diagnostic_id=refs.diagnostic_id,
                         candidate_id=refs.candidate_id,
                         release_id=refs.release_id)
    payload_raw = row.get("payload")
    payload = json.loads(payload_raw) if isinstance(payload_raw, str) \
        else dict(payload_raw or {})
    trust = str(row.get("trust") or "trusted")
    origin = str(row.get("origin") or "server")
    untrusted_origin = str(row.get("untrusted_origin") or "")
    if trust == "untrusted" and not untrusted_origin:
        raise EvidenceContractError(
            f"不可信运行事件缺少来源: {row.get('event_id')}")
    event_type = str(row.get("event_type") or "info")
    severity = str(row.get("severity") or "info")
    normalized = to_four_level(str(row.get("privacy_classification")
                                    or "internal"))
    restricted = event_type in _RESTRICTED_EVENT_TYPES
    fail_safe = normalized == "restricted" and not restricted
    restricted = restricted or fail_safe
    event = EvidenceEvent(
        event_id=str(row.get("event_id") or ""),
        event_type=event_type,
        timestamp=float(row.get("timestamp") or 0.0),
        origin=origin,
        trust=trust,
        untrusted_origin=untrusted_origin,
        privacy_level="restricted" if restricted else normalized,
        fault_level=str(row.get("fault_level") or
                        infer_fault_level(event_type, severity, payload)),
        severity=severity,
        component_id=str(row.get("component_id") or "system"),
        component_version=str(row.get("component_version") or "0.1.0"),
        refs=refs,
        # restricted 最小证据：正文不复制，只留存在性引用
        payload={} if restricted else payload,
        payload_ref=str(row.get("payload_ref") or "") or None,
        correlation_id=row.get("correlation_id") or None,
        causation_id=row.get("causation_id") or None,
        sequence_no=row.get("sequence_no"),
        redacted=bool(restricted or row.get("redacted")),
    )
    event.validate()
    return event


def project_audit_row(row: Dict[str, Any]) -> EvidenceEvent:
    """audit_log 行 → EvidenceEvent（受信审计证据，内部级）。"""
    detail_raw = row.get("detail") or {}
    detail = json.loads(detail_raw) if isinstance(detail_raw, str)         else dict(detail_raw)
    assert_context_safe(detail)
    target = str(row.get("target") or "")
    event = EvidenceEvent(
        event_id=str(row.get("audit_id") or ""),
        event_type=f"audit.{row.get('action') or 'unknown'}",
        timestamp=float(row.get("at") or 0.0),
        origin="server", trust="trusted",
        privacy_level="internal",
        fault_level=infer_fault_level(
            str(row.get("action") or ""), "info", detail),
        severity="info",
        component_id="audit",
        refs=_refs_from_row(row, task_id=target if target.startswith("t")
                            else "", audit_id=str(row.get("audit_id") or "")),
        payload=detail,
    )
    event.validate()
    return event


def project_side_effect_row(row: Dict[str, Any]) -> EvidenceEvent:
    """task_side_effects 行 → EvidenceEvent。

    副作用的 args/摘要可能含敏感目标（设备动作/外部接收方）：
    restricted + redacted，只保留状态与引用——最小证据。
    """
    status = str(row.get("status") or "planned")
    payload = {"status": status, "tool_id": str(row.get("tool_id") or ""),
               "idempotency_key": str(row.get("idempotency_key") or "")}
    event = EvidenceEvent(
        event_id=str(row.get("effect_id") or ""),
        event_type=f"side_effect.{status}",
        timestamp=float(row.get("updated_at") or row.get("created_at") or 0.0),
        origin="server", trust="trusted",
        privacy_level="restricted",
        fault_level="degraded" if status == "unknown" else
        ("transient" if status in ("planned", "submitted") else "none"),
        severity="warn" if status == "unknown" else "info",
        component_id="side_effects",
        refs=_refs_from_row(row),
        payload={},  # restricted：不携带正文
        payload_ref=str(row.get("effect_id") or ""),
        redacted=True,
    )
    event.validate()
    return event


def project_approval_row(row: Dict[str, Any]) -> EvidenceEvent:
    """approvals 行 → EvidenceEvent（审批动作摘要敏感：restricted）。"""
    status = str(row.get("status") or "pending")
    event = EvidenceEvent(
        event_id=str(row.get("approval_id") or ""),
        event_type=f"approval.{status}",
        timestamp=float(row.get("created_at") or 0.0),
        origin="server", trust="trusted",
        privacy_level="restricted",
        fault_level="none",
        severity="info",
        component_id="approvals",
        refs=_refs_from_row(
            row, approval_id=str(row.get("approval_id") or "")),
        payload={},
        payload_ref=str(row.get("approval_id") or ""),
        redacted=True,
    )
    event.validate()
    return event


def _rows(conn, sql: str, args: tuple) -> List[Dict[str, Any]]:
    try:
        cursor = conn.execute(sql, args)
        rows = cursor.fetchall()
        if not rows:
            return []
        first = rows[0]
        if isinstance(first, dict):
            return [dict(r) for r in rows]
        # 原生 sqlite3 连接（EventBus）无 row_factory：按 cursor 列名映射
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, r)) for r in rows]
    except sqlite3.OperationalError as exc:
        # Optional domains may not have created their tables yet. A closed,
        # locked or incompatible configured store is not an empty repository.
        if str(exc).startswith("no such table:"):
            return []
        raise EvidenceRepositoryError("证据仓储读取失败，不能返回完整证据结论") from exc
    except Exception as exc:
        raise EvidenceRepositoryError("证据仓储读取失败，不能返回完整证据结论") from exc


def collect_for_task(db: Any, task_id: str,
                     *, events_conn: Any = None) -> List[EvidenceEvent]:
    """聚合任务相关的全部业务证据事件（按时间序）。

    覆盖 runtime_events / audit_log / task_side_effects / approvals
    四类业务仓储；导出、决策链与删除计划首次消费真实数据走这里。

    部署形态：EventBus 默认落独立 events.db（App 装配），其余三表在
    主库——events_conn 传事件库连接（App 传 ``app.bus._conn``）；
    缺省回落主库连接（单库部署/测试形态）。
    """
    if not task_id:
        raise ValueError("聚合证据必须限定 task")
    conn = getattr(db, "_conn", None) or db
    events_conn = events_conn or conn
    events: List[EvidenceEvent] = []
    events.extend(
        project_runtime_event(row) for row in
        _rows(events_conn, "SELECT * FROM runtime_events WHERE task_id=?"
              " ORDER BY timestamp", (task_id,)))
    events.extend(
        project_audit_row(row) for row in
        _rows(conn, "SELECT * FROM audit_log WHERE target=?"
              " ORDER BY at", (task_id,)))
    events.extend(
        project_side_effect_row(row) for row in
        _rows(conn, "SELECT * FROM task_side_effects WHERE task_id=?"
              " ORDER BY updated_at", (task_id,)))
    events.extend(
        project_approval_row(row) for row in
        _rows(conn, "SELECT * FROM approvals WHERE task_id=?"
              " ORDER BY created_at", (task_id,)))
    events.sort(key=lambda e: (e.timestamp, e.event_id))
    return events


def deletion_reconciliation(db: Any, task_id: str,
                            *, events_conn: Any = None) -> Dict[str, Any]:
    """删除/保留/失败可对账：执行后核对业务仓储残留（16-P 验收③）。

    返回各仓储的残留行数与结论：全部归零 = 完全删除；restricted
    级在导出报告中本就只留引用（无正文残留）；任何残留都显式列出，
    不返回假成功。
    """
    conn = getattr(db, "_conn", None) or db
    events_conn = events_conn or conn
    residuals = {}
    sources = (("runtime_events", "task_id=?", events_conn),
               ("task_side_effects", "task_id=?", conn),
               ("approvals", "task_id=?", conn),
               ("audit_log", "target=?", conn))
    for table, where, source in sources:
        try:
            rows = _rows(source, f"SELECT COUNT(*) AS n FROM {table}"
                                 f" WHERE {where}", (task_id,))
            n = int(rows[0]["n"]) if rows else 0
        except EvidenceRepositoryError:
            n = None  # 已配置仓储不可读：残留未知，不可按零处理。
        residuals[table] = n
    unknown = [name for name, count in residuals.items() if count is None]
    clean = all(n == 0 for n in residuals.values())
    return {"task_id": task_id, "residuals": residuals,
            "unknown_layers": unknown, "fully_deleted": clean}


__all__ = [
    "project_runtime_event", "project_audit_row",
    "project_side_effect_row", "project_approval_row",
    "collect_for_task", "deletion_reconciliation",
    "EvidenceContractError", "as_evidence_event",
    "EvidenceRepositoryError",
]
