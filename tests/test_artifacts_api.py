# -*- coding: utf-8 -*-
"""ALN-30-B 缺口修复：任务产物**全量下载**端点。

此前只有 4KB 预览，客户端无法提供完整成果下载。本测试用真实 ApiServer +
真实 ArtifactStore 文件校验全量下载、摘要、二进制以及拒绝路径（404/410/413）。
"""

import base64
import hashlib
import json
import threading
import urllib.error
import urllib.request

import pytest

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.task import Task, TaskStatus
from evoagent.delivery.artifacts import ArtifactStore


@pytest.fixture
def api(tmp_path):
    app = App(data_dir=str(tmp_path))
    store = ArtifactStore(str(app.config.data_dir))
    task = Task(user_goal="30-B 下载探针", task_status=TaskStatus.SUCCEEDED)
    text_body = ("全量下载验证\n" * 800).encode("utf-8")          # > 4KB
    binary_body = bytes(range(256)) * 4
    text_meta = store.save(task.task_id, "n1", "markdown", text_body,
                           description="长文本成果")
    bin_meta = store.save(task.task_id, "n2", "binary", binary_body)
    oversize = dict(text_meta, artifact_id=f"{task.task_id}:n3:big",
                    bytes=30 * 1024 * 1024 * 1024)
    task.result_evidence = [text_meta, bin_meta, oversize]
    app.db.save_task(task)
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (app, task.task_id, f"http://127.0.0.1:{server.port}",
               {"text": (text_meta, text_body), "bin": (bin_meta, binary_body),
                "oversize": oversize["artifact_id"]})
    finally:
        server.shutdown()
        app.db.close()


def call(base, path):
    request = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_full_text_download_is_not_truncated(api):
    app, task_id, base, bodies = api
    meta, body = bodies["text"]
    assert len(body) > 4096
    status, payload = call(
        base, f"/tasks/{task_id}/artifacts/{meta['artifact_id']}/download")
    assert status == 200
    assert payload["truncated"] is False
    assert payload["bytes"] == len(body)
    assert payload["text"] == body.decode("utf-8")     # 不再只有 4KB
    assert payload["sha256"] == hashlib.sha256(body).hexdigest()


def test_binary_download_returns_base64(api):
    app, task_id, base, bodies = api
    meta, body = bodies["bin"]
    status, payload = call(
        base, f"/tasks/{task_id}/artifacts/{meta['artifact_id']}/download")
    assert status == 200
    assert payload["text"] is None
    assert base64.b64decode(payload["base64"]) == body


def test_unknown_task_is_404(api):
    app, task_id, base, bodies = api
    meta, _ = bodies["text"]
    status, _ = call(
        base, f"/tasks/missing/artifacts/{meta['artifact_id']}/download")
    assert status == 404


def test_unknown_artifact_is_404(api):
    app, task_id, base, _bodies = api
    status, _ = call(base, f"/tasks/{task_id}/artifacts/nope/download")
    assert status == 404


def test_tampered_file_is_410(api, tmp_path):
    app, task_id, base, bodies = api
    meta, _ = bodies["text"]
    target = tmp_path / "artifacts" / meta["path"]
    target.write_bytes(b"tampered")
    status, _ = call(
        base, f"/tasks/{task_id}/artifacts/{meta['artifact_id']}/download")
    assert status == 410


def test_oversize_is_413(api):
    app, task_id, base, bodies = api
    status, payload = call(
        base, f"/tasks/{task_id}/artifacts/{bodies['oversize']}/download")
    assert status == 413
    assert payload["limit"] > 0
