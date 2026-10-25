"""任务产物端点（ALN-24-A）：产物列表与预览下载。

- GET /tasks/{id}/artifacts：列出该任务全部有效产物（文件系统存在性
  为准，元数据来自 task.result_evidence——无文件不显示下载）。
- GET /tasks/{id}/artifacts/{artifact_id}/preview：返回产物内容预览
  （前 4KB 文本或 base64 二进制），供客户端展示。
"""

from pathlib import Path

from .routes import get

PREVIEW_LIMIT = 4096


@get("/tasks/{task_id}/artifacts")
def list_task_artifacts(ctx):
    app = ctx.app
    task_id = ctx.path.split("/")[2]
    task = app.db.get_task(task_id)
    if task is None:
        return ctx.json(404, {"error": "任务不存在"})
    from ..delivery.artifacts import ArtifactStore
    store = ArtifactStore(str(app.config.data_dir))
    visible = store.list_for_task(task.result_evidence)
    return ctx.json(200, {"artifacts": [
        {"artifact_id": a["artifact_id"], "node_id": a["node_id"],
         "kind": a["kind"], "sha256": a["sha256"], "bytes": a["bytes"],
         "description": a.get("description", ""),
         "created_at": a.get("created_at")}
        for a in visible]})


@get("/tasks/{task_id}/artifacts/{artifact_id}/preview")
def preview_artifact(ctx):
    app = ctx.app
    parts = ctx.path.split("/")
    task_id, artifact_id = parts[2], parts[4]
    task = app.db.get_task(task_id)
    if task is None:
        return ctx.json(404, {"error": "任务不存在"})
    from ..delivery.artifacts import ArtifactStore
    store = ArtifactStore(str(app.config.data_dir))
    meta = next((e for e in (task.result_evidence or [])
                 if isinstance(e, dict)
                 and e.get("artifact_id") == artifact_id), None)
    if meta is None:
        return ctx.json(404, {"error": "产物不存在"})
    content = store.read(meta)
    if content is None:
        return ctx.json(410, {"error": "产物文件缺失或校验不符"})
    truncated = len(content) > PREVIEW_LIMIT
    preview = content[:PREVIEW_LIMIT]
    kind = meta.get("kind", "binary")
    is_text = kind in ("markdown", "text", "json", "report")
    return ctx.json(200, {
        "artifact_id": artifact_id,
        "kind": kind,
        "bytes": len(content),
        "truncated": truncated,
        "text": preview.decode("utf-8", errors="replace") if is_text else None,
        "base64": None if is_text else preview.hex(),
    })
