"""Task-owner context provenance; global memory administration remains separate.

ALN-24-D 追加「用户记忆控制」端点（来源/纠错/删除/分析范围）。它们走
`api/routes.py` 注册表，由 `server.py` 导入本模块时自动登记，不改 server.py
的既有 if 链。
"""
from .routes import get, post, put
from ..memory.user_control import (MemoryControlError, MemoryControlService,
                                   MemoryNotFound)


def _service(app):
    service = getattr(app, "memory_control", None)
    if service is None:
        service = MemoryControlService(app.memory, db=getattr(app, "db", None),
                                       audit=getattr(app, "audit", None))
        app.memory_control = service
    return service


def _error(ctx, exc):
    status = 404 if isinstance(exc, MemoryNotFound) else 400
    return ctx.json(status, {"error": str(exc)})


def _require_role(ctx):
    role = getattr(getattr(ctx, "auth", None), "role", "anonymous")
    return role in ("operator", "admin")


@get("/tasks/{task_id}/context-manifests")
def context_manifests(ctx):
    task_id = ctx.path.split("/")[2]
    if ctx.app.db.get_task(task_id) is None:
        return ctx.json(404, {"error": "任务不存在"})
    return ctx.json(200, {"manifests":ctx.app.db.list_context_manifests(task_id, ctx.query.get("run_id"))})


# ---- 用户记忆控制（ALN-24-D）----
@get("/memory/items")
def list_memory_items(ctx):
    try:
        items = _service(ctx.app).list_memories(
            scope=ctx.query.get("scope"), kind=ctx.query.get("kind"),
            limit=int(ctx.query.get("limit") or 100))
    except (MemoryControlError, ValueError) as exc:
        return _error(ctx, exc)
    return ctx.json(200, {"items": items, "count": len(items),
                          "source_note": "来源/作用域/可信度逐项可见；证据快照不外露"})


@get("/memory/items/{memory_id}")
def get_memory_item(ctx):
    memory_id = ctx.path.split("/")[3]
    try:
        return ctx.json(200, {"item": _service(ctx.app).get_memory(memory_id)})
    except (MemoryControlError, ValueError) as exc:
        return _error(ctx, exc)


@post("/memory/items/{memory_id}/correct")
def correct_memory_item(ctx):
    if not _require_role(ctx):
        return ctx.json(403, {"error": "需要 operator 或 admin 角色"})
    memory_id = ctx.path.split("/")[3]
    body = ctx.body
    try:
        result = _service(ctx.app).correct_memory(
            memory_id, str(body.get("content") or ""),
            actor=str(body.get("actor") or "api:user"),
            reason=str(body.get("reason") or ""))
    except (MemoryControlError, ValueError) as exc:
        return _error(ctx, exc)
    return ctx.json(200, result)


@post("/memory/items/{memory_id}/delete")
def delete_memory_item(ctx):
    if not _require_role(ctx):
        return ctx.json(403, {"error": "需要 operator 或 admin 角色"})
    memory_id = ctx.path.split("/")[3]
    body = ctx.body
    try:
        result = _service(ctx.app).delete_memory(
            memory_id, actor=str(body.get("actor") or "api:user"),
            reason=str(body.get("reason") or ""))
    except (MemoryControlError, ValueError) as exc:
        return _error(ctx, exc)
    return ctx.json(200 if result.get("ok") else 500, result)


@get("/memory/scope-policy")
def get_memory_scope_policy(ctx):
    return ctx.json(200, _service(ctx.app).get_scope_policy())


@put("/memory/scope-policy")
def put_memory_scope_policy(ctx):
    if getattr(getattr(ctx, "auth", None), "role", "") != "admin":
        return ctx.json(403, {"error": "调整记忆分析范围需要 admin 角色"})
    body = ctx.body
    try:
        result = _service(ctx.app).set_scope_policy(
            allow=body.get("allow"), deny=body.get("deny"),
            actor=str(body.get("actor") or "api:admin"),
            reason=str(body.get("reason") or ""))
    except (MemoryControlError, ValueError) as exc:
        return _error(ctx, exc)
    return ctx.json(200, result)
