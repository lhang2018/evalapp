"""Task-owner context provenance; global memory administration remains separate."""
from .routes import get

@get("/tasks/{task_id}/context-manifests")
def context_manifests(ctx):
    task_id = ctx.path.split("/")[2]
    if ctx.app.db.get_task(task_id) is None:
        return ctx.json(404, {"error":"任务不存在"})
    return ctx.json(200, {"manifests":ctx.app.db.list_context_manifests(task_id, ctx.query.get("run_id"))})
