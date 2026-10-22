"""Administrator task scopes and independently verified effect reconciliation."""
from .routes import get, post


def admin(ctx):
    if ctx.auth.role != "admin":
        ctx.json(403, {"error": "该操作需要admin权限"})
        return False
    return True


@get("/tasks/{task_id}/authorization")
def get_scope(ctx):
    if admin(ctx):
        row = ctx.app.tools.authorizations.current(ctx.path.split("/")[2])
        ctx.json(200 if row else 404, row or {"error": "任务授权不存在"})


@post("/tasks/{task_id}/authorization")
def issue_scope(ctx):
    if not admin(ctx):
        return
    task = ctx.app.db.get_task(ctx.path.split("/")[2])
    if task is None:
        return ctx.json(404, {"error": "任务不存在"})
    try:
        ref = ctx.app.tools.authorizations.issue(task.task_id, ctx.body.get("policy"),
                    actor="api:admin", ttl_seconds=ctx.body.get("ttl_seconds", 86400))
        # Do not save a stale Task over a concurrent runtime status transition.
        ctx.app.audit.record("admin", "authorization.issue", target=task.task_id, detail={"authorization_ref": ref})
        ctx.json(201, {"authorization_ref": ref})
    except (ValueError, TypeError) as exc:
        ctx.json(400, {"error": str(exc)})


@post("/authorizations/{ref}/revoke")
def revoke_scope(ctx):
    if admin(ctx):
        ref = ctx.path.split("/")[2]
        changed = ctx.app.tools.authorizations.revoke(ref)
        ctx.app.audit.record("admin", "authorization.revoke", target=ref, detail={"changed": changed})
        ctx.json(200, {"revoked": changed})


@get("/effects/{effect_id}")
def get_effect(ctx):
    if admin(ctx):
        row = ctx.app.tools.effects.get(ctx.path.split("/")[2])
        ctx.json(200 if row else 404, row or {"error": "副作用不存在"})


@post("/effects/{effect_id}/reconcile")
def reconcile_effect(ctx):
    if not admin(ctx):
        return
    identity = ctx.path.split("/")[2]
    try:
        result = ctx.app.tools.reconcile_effect(identity, ctx.body.get("receipt"))
        ctx.app.audit.record("admin", "effect.reconcile", target=identity, detail={"ok": result.ok})
        ctx.json(200, {"effect": ctx.app.tools.effects.get(identity), "result": result.to_dict()})
    except (ValueError, TypeError, KeyError) as exc:
        ctx.json(409, {"error": str(exc), "required_action": "reconcile"})
