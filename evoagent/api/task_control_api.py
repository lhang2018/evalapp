"""Task clarification and durable execution history."""
from .routes import get, post


@post("/tasks/{task_id}/recover")
def recover_task(ctx):
    if ctx.auth.role != "admin":
        return ctx.json(403, {"error": "该操作需要 admin 权限"})
    try:
        task = ctx.app.controller.recover_task(ctx.path.split("/")[2], wait=False)
    except KeyError:
        return ctx.json(404, {"error": "任务不存在"})
    except (ValueError, PermissionError) as exc:
        return ctx.json(409, {"error": str(exc)})
    return ctx.json(202, task.to_dict())


@post("/tasks/{task_id}/answer")
def answer_task(ctx):
    task_id = ctx.path.split("/")[2]
    body = ctx.body
    if not isinstance(body, dict):
        return ctx.json(400, {"error": "请求体必须是对象"})
    try:
        task, accepted = ctx.app.controller.answer_task(
            task_id, body.get("request_id"), body.get("answer"), wait=False)
    except KeyError:
        return ctx.json(404, {"error": "任务不存在"})
    except TypeError as exc:
        return ctx.json(400, {"error": str(exc)})
    except (ValueError, PermissionError) as exc:
        return ctx.json(409, {"error": str(exc)})
    return ctx.json(202 if accepted else 200, {
        "task_id": task_id, "accepted": accepted,
        "task_status": task.task_status.value,
        "run_id": task.current_run_id})


@get("/tasks/{task_id}/runs")
def task_runs(ctx):
    task_id = ctx.path.split("/")[2]
    if ctx.app.db.get_task(task_id) is None:
        return ctx.json(404, {"error": "任务不存在"})
    return ctx.json(200, {"runs": ctx.app.db.list_runs(task_id)})
