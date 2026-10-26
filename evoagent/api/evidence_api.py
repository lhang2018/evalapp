"""聚合观测与受控导出/删除 API（ALN-16-Q）。

为已交付的聚合证据查询、授权导出下载与删除执行器提供**受鉴权的 HTTP
消费端**；全部复用既有实现，不新建第二套证据/存储权威：

- 查询/导出/删除都按 `task_id` 从 `observability.repository.collect_for_task`
  聚合真实业务证据（runtime_events / audit_log / task_side_effects / approvals）；
- 导出复用 `EvidenceExporter` + `export_to_file`（隐私等级裁剪、最小化、
  路径根限制、排他创建、文件哈希入审计）；
- 删除复用 `plan_deletion` + `LayerManager.delete_task` + `execute_deletion`
  （计划摘要校验、分层逐级计数、审计留痕），删除后按仓储对账返回**实际残留**，
  明确不宣称跨备份/全域擦除；
- 主体来自服务端鉴权：请求体 `principal` 与鉴权主体不一致一律拒绝。

范围：只做 API 接线；查询聚合、导出隐私策略、删除执行与对账均沿用 16-P/16-X。
"""

import base64
import hashlib
import json
import os
import re
import uuid
from functools import wraps

from .routes import get, post
from ..observability.repository import EvidenceRepositoryError

EXPORT_ID_RE = re.compile(r"^exp_[0-9a-f]{12}$")
EXPORT_RELATIVE_PREFIX = "exports"


def _repository_response(handler):
    @wraps(handler)
    def run(ctx):
        try:
            return handler(ctx)
        except EvidenceRepositoryError as exc:
            return ctx.json(503, {"error": str(exc)})
    return run


def _boolean(body, key, default=False):
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(key + " 必须是布尔值")
    return value


def _role(ctx):
    return getattr(getattr(ctx, "auth", None), "role", "anonymous")


def _auth_principal(ctx, body=None):
    principal = getattr(getattr(ctx, "auth", None), "principal_id", "") or ""
    if not principal.strip():
        raise PermissionError("缺少已鉴权主体")
    if body is not None and body.get("principal") not in (None, "", principal):
        raise PermissionError("请求主体与鉴权主体不一致，拒绝按请求体选择主体")
    return principal.strip()


def _events(app, task_id):
    from ..observability.repository import collect_for_task
    return collect_for_task(app.db, task_id,
                            events_conn=getattr(app.bus, "_conn", None))


def _reconciliation(app, task_id):
    from ..observability.repository import deletion_reconciliation
    return deletion_reconciliation(app.db, task_id,
                                   events_conn=getattr(app.bus, "_conn", None))


def _summary(events):
    by_privacy, by_trust, by_type, actors = {}, {}, {}, set()
    for event in events:
        by_privacy[event.privacy_level] = by_privacy.get(event.privacy_level, 0) + 1
        by_trust[event.trust] = by_trust.get(event.trust, 0) + 1
        by_type[event.event_type] = by_type.get(event.event_type, 0) + 1
        if getattr(event, "actor", ""):
            actors.add(event.actor)
    return {"event_count": len(events), "by_privacy": by_privacy,
            "by_trust": by_trust, "by_type": by_type,
            "actors": sorted(actors),
            "first_at": events[0].timestamp if events else None,
            "last_at": events[-1].timestamp if events else None}


def _export_root(app):
    return os.path.join(str(app.config.data_dir), EXPORT_RELATIVE_PREFIX)


def _audit_match(app, *, actor, sha256, events_digest):
    """下载字节必须能在审计里找到对应条目（sha256 + events_digest 双匹配）。"""
    # Durable authorizations do not expire because unrelated audit traffic
    # pushed them out of a recent-items window.
    with app.db._lock:
        records = app.db._conn.execute(
            "SELECT detail FROM audit_log WHERE action=? AND actor=? AND outcome='ok'",
            ("export_download", actor)).fetchall()
    for row in records:
        detail = row["detail"]
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except ValueError:
                continue
        if not isinstance(detail, dict):
            continue
        if (detail.get("sha256") == sha256
                and detail.get("events_digest") == events_digest
                and detail.get("actor") == actor):
            return True
    return False


@get("/evidence/tasks/{task_id}")
@_repository_response
def get_task_evidence(ctx):
    if _role(ctx) != "admin":
        return ctx.json(403, {"error": "聚合证据查询需要 admin 角色"})
    task_id = ctx.path.split("/")[3]
    if ctx.app.db.get_task(task_id) is None:
        return ctx.json(404, {"error": "任务不存在"})
    events = _events(ctx.app, task_id)
    return ctx.json(200, {
        "task_id": task_id,
        "summary": _summary(events),
        "reconciliation": _reconciliation(ctx.app, task_id),
        "events": [{"event_id": e.event_id, "event_type": e.event_type,
                    "timestamp": e.timestamp, "severity": e.severity,
                    "privacy_level": e.privacy_level, "trust": e.trust,
                    "refs": e.refs.to_dict()} for e in events],
        "note": "仅投影引用与元数据；正文/凭据不在此接口返回",
    })


@post("/evidence/export")
@_repository_response
def post_evidence_export(ctx):
    role = _role(ctx)
    if role not in ("admin", "operator"):
        return ctx.json(403, {"error": "导出需要 operator 或 admin 角色"})
    body = ctx.body
    if not isinstance(body, dict):
        return ctx.json(400, {"error": "请求体必须是对象"})
    try:
        principal = _auth_principal(ctx, body)
    except PermissionError as exc:
        return ctx.json(403, {"error": str(exc)})
    task_id = str(body.get("task_id") or "").strip()
    reason = str(body.get("reason") or "").strip()
    if not task_id:
        return ctx.json(400, {"error": "导出必须限定 task_id"})
    if not reason:
        return ctx.json(400, {"error": "导出必须记录 reason"})
    if ctx.app.db.get_task(task_id) is None:
        return ctx.json(404, {"error": "任务不存在"})
    from ..observability.export import (EvidenceExporter, ExportDenied,
                                        ExportPolicy, export_to_file)
    try:
        policy = ExportPolicy(
            role=role,
            allow_sensitive=_boolean(body, "allow_sensitive") and role == "admin",
            allow_restricted=_boolean(body, "allow_restricted") and role == "admin",
            include_payload_refs=_boolean(body, "include_payload_refs", True))
    except ValueError as exc:
        return ctx.json(400, {"error": str(exc)})
    exporter = EvidenceExporter(policy)
    export_id = "exp_" + uuid.uuid4().hex[:12]
    relative = f"{EXPORT_RELATIVE_PREFIX}/{export_id}.json"
    try:
        result = export_to_file(
            exporter, _events(ctx.app, task_id), relative,
            actor=principal, reason=reason, task_id=task_id,
            allowed_root=str(ctx.app.config.data_dir),
            audit_log=ctx.app.audit)
    except ExportDenied as exc:
        return ctx.json(403, {"error": str(exc)})
    report = result["report"]
    return ctx.json(200, {
        "ok": True, "export_id": export_id, "path": relative,
        "sha256": result["sha256"],
        "events_digest": report["events_digest"],
        "count": len(report["events"]),
        "skipped": report["skipped"],
        "policy": report["policy"],
        "audit_id": result["audit_id"],
        "audit_persisted": result["audit_persisted"],
        "download": f"/evidence/exports/{export_id}/download",
        "note": "导出仅含最小化引用；sensitive/restricted 默认裁剪",
    })


@get("/evidence/exports/{export_id}/download")
def download_evidence_export(ctx):
    if _role(ctx) != "admin":
        return ctx.json(403, {"error": "导出下载需要 admin 角色"})
    export_id = ctx.path.split("/")[3]
    if not EXPORT_ID_RE.fullmatch(export_id):
        return ctx.json(400, {"error": "非法导出标识"})
    path = os.path.realpath(os.path.join(_export_root(ctx.app),
                                         f"{export_id}.json"))
    root = os.path.realpath(_export_root(ctx.app))
    try:
        if os.path.commonpath([root, path]) != root:
            return ctx.json(403, {"error": "导出路径越出授权目录"})
    except ValueError:
        return ctx.json(403, {"error": "导出路径越出授权目录"})
    if not os.path.isfile(path):
        return ctx.json(404, {"error": "导出文件不存在或已清理"})
    with open(path, "rb") as handle:
        body = handle.read()
    sha256 = hashlib.sha256(body).hexdigest()
    try:
        report = json.loads(body.decode("utf-8"))
    except ValueError:
        return ctx.json(409, {"error": "导出文件损坏，拒绝下载"})
    actor = str(report.get("actor") or "")
    events_digest = str(report.get("events_digest") or "")
    matched = _audit_match(ctx.app, actor=actor, sha256=sha256,
                           events_digest=events_digest)
    if not matched:
        return ctx.json(409, {"error": "下载字节与审计/摘要不匹配，拒绝下载"})
    return ctx.json(200, {
        "export_id": export_id, "sha256": sha256,
        "events_digest": events_digest,
        "count": len(report.get("events") or []),
        "actor": actor, "audit_match": True,
        "bytes_b64": base64.b64encode(body).decode("ascii"),
        "note": "受控下载：base64 字节；客户端校验 sha256",
    })


@post("/evidence/deletion")
@_repository_response
def post_evidence_deletion(ctx):
    if _role(ctx) != "admin":
        return ctx.json(403, {"error": "证据删除执行需要 admin 角色"})
    body = ctx.body
    if not isinstance(body, dict):
        return ctx.json(400, {"error": "请求体必须是对象"})
    try:
        principal = _auth_principal(ctx, body)
    except PermissionError as exc:
        return ctx.json(403, {"error": str(exc)})
    task_id = str(body.get("task_id") or "").strip()
    reason = str(body.get("reason") or "").strip()
    if not task_id:
        return ctx.json(400, {"error": "删除必须限定 task_id"})
    if ctx.app.db.get_task(task_id) is None:
        return ctx.json(404, {"error": "任务不存在"})
    try:
        dry_run = _boolean(body, "dry_run", True)
    except ValueError as exc:
        return ctx.json(400, {"error": str(exc)})
    supplied = body.get("plan_digest")
    if not dry_run and (not isinstance(supplied, str) or not supplied.strip()):
        return ctx.json(400, {"error": "执行删除必须提供预览计划的 plan_digest"})
    from ..observability.export import (DeletionScopeError, execute_deletion,
                                        plan_deletion)
    try:
        plan = plan_deletion(_events(ctx.app, task_id), task_id=task_id,
                             actor=principal, reason=reason)
    except ValueError as exc:
        return ctx.json(400, {"error": str(exc)})
    if supplied is not None and supplied != plan.digest:
        return ctx.json(409, {"error": "删除计划摘要不一致，拒绝执行"})
    before = _reconciliation(ctx.app, task_id)
    if before["unknown_layers"]:
        return ctx.json(503, {"error": "部分仓储残留未知，拒绝执行删除", "reconciliation": before})
    if dry_run:
        return ctx.json(200, {"ok": True, "dry_run": True,
                              "plan": plan.to_dict(),
                              "reconciliation_before": before,
                              "note": "dry-run：未删除任何数据"})
    try:
        execution = execute_deletion(ctx.app.logs, plan, actor=principal,
                                     reason=reason)
    except DeletionScopeError as exc:
        return ctx.json(409, {"error": str(exc)})
    after = _reconciliation(ctx.app, task_id)
    retained = [name for name, count in (after.get("residuals") or {}).items()
                if count != 0]
    return ctx.json(200, {
        "ok": True, "dry_run": False, "plan": plan.to_dict(),
        "execution": execution,
        "reconciliation": after,
        "retained_layers": retained,
        "fully_deleted": False,
        "note": ("执行器实际覆盖 runtime_events/execution_trace/"
                 "diagnostic_evidence/diagnostics；task_side_effects、approvals"
                 " 与审计层按现有执行器保留（见 retained_layers/reconciliation），"
                 "不宣称跨备份或全域擦除"),
    })
