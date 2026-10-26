"""用户反馈端点（ALN-29-C）：反馈绑定真实 task/run 并关联最小证据。

- POST /feedback：提交反馈（文本经凭据防线脱敏/拒绝；task/run 必须真实
  存在才允许关联）；重复反馈自动聚合计数。
- GET /feedback、/feedback/{id}：admin 审计面（列表/详情，含 cause_status
  恒为 unverified 的根因纪律说明）。
"""

import json
from .routes import get, post


@post("/feedback")
def submit_feedback(ctx):
    body = ctx.body
    if not isinstance(body, dict):
        return ctx.json(400, {"error": "请求体必须是对象"})
    app = ctx.app
    for field in ("task_id", "run_id"):
        if body.get(field) is not None and not isinstance(body[field], str):
            return ctx.json(400, {"error": f"{field} 必须是字符串"})
    task_id = (body.get("task_id") or "").strip() or None
    run_id = (body.get("run_id") or "").strip() or None
    if task_id and app.db.get_task(task_id) is None:
        return ctx.json(404, {"error": "任务不存在，无法关联反馈"})
    if run_id:
        with app.db._lock:
            row = app.db._conn.execute(
                "SELECT task_id, version_manifest FROM task_runs WHERE run_id=?",
                (run_id,)).fetchone()
        if row is None:
            return ctx.json(404, {"error": "运行不存在，无法关联反馈"})
        if task_id is not None and row["task_id"] != task_id:
            return ctx.json(400, {"error": "运行不属于指定任务"})
        task_id = row["task_id"]
        if app.db.get_task(task_id) is None:
            return ctx.json(404, {"error": "运行关联的任务不存在"})
    manifest_id = None
    if run_id:
        try:
            manifest = json.loads(row["version_manifest"] or "{}")
        except (TypeError, ValueError):
            return ctx.json(409, {"error": "运行清单不可读，无法关联反馈，请先核对该运行"})
        if not isinstance(manifest, dict):
            return ctx.json(409, {"error": "运行清单不可读，无法关联反馈，请先核对该运行"})
        manifest_id = manifest.get("runtime_manifest_id")
    elif task_id:
        task = app.db.get_task(task_id)
        manifest_id = (task.runtime_manifest or {}).get(
            "runtime_manifest_id")
    from ..observability.feedback import FeedbackError, FeedbackStore
    store = FeedbackStore(app.db._conn, lock=app.db._lock)
    try:
        out = store.submit(
            category=body.get("category", ""),
            text=body.get("text", ""),
            task_id=task_id, run_id=run_id, manifest_id=manifest_id,
            evidence_refs=body.get("evidence_refs", []))
    except FeedbackError as exc:
        return ctx.json(400, {"error": str(exc)})
    return ctx.json(201, out)


@get("/feedback")
def list_feedback(ctx):
    if ctx.auth.role != "admin":
        return ctx.json(403, {"error": "该操作需要 admin 权限"})
    from ..observability.feedback import FeedbackStore
    store = FeedbackStore(ctx.app.db._conn, lock=ctx.app.db._lock)
    task_id = ctx.query.get("task_id") or None
    return ctx.json(200, {"feedback": store.list_recent(task_id=task_id)})


@get("/feedback/{feedback_id}")
def get_feedback(ctx):
    if ctx.auth.role != "admin":
        return ctx.json(403, {"error": "该操作需要 admin 权限"})
    from ..observability.feedback import FeedbackStore
    store = FeedbackStore(ctx.app.db._conn, lock=ctx.app.db._lock)
    row = store.get(ctx.path.split("/")[-1])
    return ctx.json(200, row) if row else ctx.json(404, {"error": "反馈不存在"})
