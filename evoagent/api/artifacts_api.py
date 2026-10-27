"""任务产物端点（ALN-24-A / ALN-30-B）：产物列表、预览与全量下载。

- GET /tasks/{id}/artifacts：列出该任务全部有效产物（文件系统存在性
  为准，元数据来自 task.result_evidence——无文件不显示下载）。
- GET /tasks/{id}/artifacts/{artifact_id}/preview：返回产物内容预览
  （前 4KB 文本或 base64 二进制），供客户端展示。
- GET /tasks/{id}/artifacts/{artifact_id}/download：返回**全量**产物
  （ALN-30-B 缺口：此前只有 4KB 预览，客户端无法提供完整成果下载）。
  读取时按元数据校验大小与 sha256；超过上限返回 413，不完整/被篡改 410。
"""

import base64
import hashlib

from .routes import get

PREVIEW_LIMIT = 4096
#: 全量下载上限，避免一次读入超大文件耗尽内存（超出返回 413）。
DOWNLOAD_MAX_BYTES = 32 * 1024 * 1024
_TEXT_KINDS = ("markdown", "text", "json", "report")


@get("/tasks/{task_id}/artifacts")
def list_task_artifacts(ctx):
    app = ctx.app
    task_id = ctx.path.split("/")[2]
    task = app.db.get_task(task_id)
    if task is None:
        return ctx.json(404, {"error": "任务不存在"})
    from ..delivery.artifacts import ArtifactStore
    store = ArtifactStore(str(app.config.data_dir))
    visible = store.list_for_task(task.result_evidence, task_id=task_id)
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
    content = store.read(meta, limit=PREVIEW_LIMIT, task_id=task_id)
    if content is None:
        return ctx.json(410, {"error": "产物文件缺失或校验不符"})
    truncated = meta["bytes"] > PREVIEW_LIMIT
    preview = content
    kind = meta.get("kind", "binary")
    is_text = kind in ("markdown", "text", "json", "report")
    return ctx.json(200, {
        "artifact_id": artifact_id,
        "kind": kind,
        "bytes": meta["bytes"],
        "truncated": truncated,
        "text": preview.decode("utf-8", errors="replace") if is_text else None,
        "base64": None if is_text else base64.b64encode(preview).decode("ascii"),
    })


@get("/tasks/{task_id}/artifacts/{artifact_id}/download")
def download_artifact(ctx):
    """全量下载产物（ALN-30-B）。按元数据校验大小与摘要，不返回假成功。"""
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
    size = meta.get("bytes")
    if type(size) is not int or size < 0:
        return ctx.json(410, {"error": "产物元数据不可信"})
    if size > DOWNLOAD_MAX_BYTES:
        return ctx.json(413, {"error": "产物超过下载上限",
                              "bytes": size, "limit": DOWNLOAD_MAX_BYTES})
    content = store.read(meta, task_id=task_id)
    if content is None:
        return ctx.json(410, {"error": "产物文件缺失或校验不符"})
    kind = meta.get("kind", "binary")
    is_text = kind in _TEXT_KINDS
    return ctx.json(200, {
        "artifact_id": artifact_id,
        "kind": kind,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "truncated": False,
        "text": content.decode("utf-8", errors="replace") if is_text else None,
        "base64": None if is_text else base64.b64encode(content).decode("ascii"),
    })
