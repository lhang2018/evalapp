"""审批记录查询接口（F-04）。注册表路由——server.py 零改动。

读侧审计面：审批记录含动作摘要/目标对象/一次性消费结果，
按 admin 权限开放（与其余审计类端点一致）。
"""

from .routes import get


@get("/approvals")
def list_recent_approvals(ctx):
    """最近审批记录（limit 默认 50，需 admin）。"""
    if ctx.auth.role != "admin":
        ctx.json(403, {"error": "该操作需要 admin 权限"})
        return
    try:
        limit = int(ctx.query.get("limit", "50"))
    except (TypeError, ValueError):
        ctx.json(400, {"error": "非法 limit"})
        return
    ctx.json(200, {"approvals": ctx.app.approvals.list_recent(limit=limit)})


@get("/approvals/")
def list_task_approvals(ctx):
    """按任务查询审批记录：/approvals/?task_id=...（需 admin）。"""
    if ctx.auth.role != "admin":
        ctx.json(403, {"error": "该操作需要 admin 权限"})
        return
    task_id = str(ctx.query.get("task_id", ""))
    if not task_id:
        ctx.json(400, {"error": "缺少 task_id"})
        return
    ctx.json(200, {"approvals":
                   ctx.app.approvals.list_for_task(task_id)})
