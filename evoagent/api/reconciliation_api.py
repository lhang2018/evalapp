"""WAITING_USER(reconciliation) 人工裁决 API（ALN-07-C 缺口补齐）。

- ``GET  /tasks/{id}/reconciliation``：只读列出未决目标与已记录裁决；operator/admin。
- ``POST /tasks/{id}/reconciliation/decisions``：admin 记录逐目标裁决。

裁决**只记录证据**，不改变任务状态、不触发重放；恢复仍走既有 resume 路径。
"""

from .routes import get, post
from ..core.reconciliation_decisions import (
    ReconciliationDecisionConflict,
    ReconciliationDecisionError,
    ReconciliationDecisions,
    pending,
)


def _role(ctx):
    return getattr(getattr(ctx, "auth", None), "role", "anonymous")


def _principal(ctx):
    return (getattr(getattr(ctx, "auth", None), "principal_id", "") or "").strip() \
        or "admin"


@get("/tasks/{task_id}/reconciliation")
def get_task_reconciliation(ctx):
    if _role(ctx) not in ("admin", "operator"):
        return ctx.json(403, {"error": "副作用裁决查询需要 operator 或 admin"})
    task_id = ctx.path.split("/")[2]
    task = ctx.app.db.get_task(task_id)
    if task is None:
        return ctx.json(404, {"error": "任务不存在"})
    store = ReconciliationDecisions(ctx.app.db)
    info = pending(task)
    return ctx.json(200, {
        **info,
        "decisions": store.for_task(task_id),
        "note": "只读：列出未决目标与已记录裁决；不触发重放",
    })


@post("/tasks/{task_id}/reconciliation/decisions")
def post_task_reconciliation_decisions(ctx):
    if _role(ctx) != "admin":
        return ctx.json(403, {"error": "副作用裁决需要 admin 角色"})
    task_id = ctx.path.split("/")[2]
    task = ctx.app.db.get_task(task_id)
    if task is None:
        return ctx.json(404, {"error": "任务不存在"})
    body = ctx.body
    if not isinstance(body, dict):
        return ctx.json(400, {"error": "请求体必须是对象"})
    override = body.get("override", False)
    if not isinstance(override, bool):
        return ctx.json(400, {"error": "override 必须是布尔值"})
    store = ReconciliationDecisions(ctx.app.db)
    try:
        result = store.record(
            task, body.get("decisions"),
            actor=_principal(ctx), reason=str(body.get("reason") or ""),
            override=override)
    except ReconciliationDecisionConflict as exc:
        return ctx.json(409, {"error": str(exc)})
    except ReconciliationDecisionError as exc:
        return ctx.json(400, {"error": str(exc)})
    return ctx.json(200, result)
