# -*- coding: utf-8 -*-
"""ALN-16-Q 缺口修复：导出分块下载 + 保留期清理。

覆盖：整文件下载保持原语义；offset/limit 分块拼接后与整文件 sha256 一致；
非法 offset/limit 400；清理 dry-run 只列出、执行后文件删除且写审计；
operator 清理 403。
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


@pytest.fixture
def api(tmp_path):
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "op"
    app.config.api_admin_token = "root"
    task = Task(user_goal="16-Q 导出分块探针", task_status=TaskStatus.SUCCEEDED)
    app.db.save_task(task)
    app.bus.emit(event_type="probe_export", task_id=task.task_id,
                 component_id="test", payload={"step": 1})
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, task.task_id, f"http://127.0.0.1:{server.port}"
    finally:
        server.shutdown()
        app.db.close()


def call(base, method, path, payload=None, token="root"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _export(api):
    app, task_id, base = api
    status, payload = call(base, "POST", "/evidence/export",
                           {"task_id": task_id, "reason": "分块下载测试"})
    assert status == 200, payload
    return payload


def test_full_download_still_works(api):
    app, task_id, base = api
    exported = _export(api)
    status, payload = call(
        base, "GET", exported["download"])
    assert status == 200
    raw = base64.b64decode(payload["bytes_b64"])
    assert hashlib.sha256(raw).hexdigest() == payload["sha256"]
    assert payload["audit_match"] is True


def test_chunked_download_reassembles_to_full(api):
    app, task_id, base = api
    exported = _export(api)
    _, full = call(base, "GET", exported["download"])
    whole = base64.b64decode(full["bytes_b64"])

    collected, offset, guard = b"", 0, 0
    while True:
        guard += 1
        assert guard < 1000
        status, payload = call(
            base, "GET", f'{exported["download"]}?offset={offset}&limit=64')
        assert status == 200, payload
        piece = base64.b64decode(payload["bytes_b64"])
        assert hashlib.sha256(piece).hexdigest() == payload["chunk_sha256"]
        assert payload["sha256"] == full["sha256"]
        collected += piece
        if payload["eof"]:
            break
        assert payload["next_offset"] > offset
        offset = payload["next_offset"]

    assert collected == whole
    assert hashlib.sha256(collected).hexdigest() == full["sha256"]


def test_invalid_chunk_params_are_400(api):
    app, task_id, base = api
    exported = _export(api)
    for query in ("?offset=-1", "?limit=0", "?offset=abc"):
        status, _ = call(base, "GET", exported["download"] + query)
        assert status == 400, query


def test_oversized_limit_is_clamped_to_max_chunk(api):
    """终审补证：超出 MAX_CHUNK_BYTES 的 limit 必须被钳制，不得原样生效。

    没有这条断言时，导出上限只是一段无人守护的死代码——重构可静默移除。
    断言放在参数解析层：HTTP 层的 chunk_bytes 恒为文件实际大小（夹具仅数 KB），
    无法区分"已钳制"与"未钳制"，故直接检验 _chunk_params 的钳制结果。
    """
    from evoagent.api.evidence_api import MAX_CHUNK_BYTES, _chunk_params

    class _Ctx:
        def __init__(self, query):
            self.query = query

        def json(self, code, body):  # 正常路径不应触发
            raise AssertionError(f"不应返回错误：{code} {body}")

    offset, limit, error = _chunk_params(_Ctx({"limit": "999999999"}))
    assert error is None
    assert offset == 0
    assert limit == MAX_CHUNK_BYTES

    # 边界：恰好等于上限不被改写
    offset2, limit2, error2 = _chunk_params(_Ctx({"limit": str(MAX_CHUNK_BYTES)}))
    assert error2 is None
    assert limit2 == MAX_CHUNK_BYTES

    # 小于上限时原样通过
    offset3, limit3, error3 = _chunk_params(_Ctx({"limit": "1024"}))
    assert error3 is None
    assert limit3 == 1024


def test_cleanup_dry_run_then_execute(api):
    app, task_id, base = api
    exported = _export(api)
    status, dry = call(base, "POST", "/evidence/exports/cleanup",
                       {"retention_seconds": 0})
    assert status == 200
    assert dry["dry_run"] is True
    assert exported["export_id"] in dry["expired"]
    # dry-run 不删除
    assert call(base, "GET", exported["download"])[0] == 200

    status, done = call(base, "POST", "/evidence/exports/cleanup",
                        {"retention_seconds": 0, "dry_run": False})
    assert status == 200
    assert done["dry_run"] is False
    assert exported["export_id"] in done["expired"]
    assert call(base, "GET", exported["download"])[0] == 404
    with app.db._lock:
        rows = app.db._conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='export_cleanup'"
        ).fetchone()[0]
    assert rows >= 1


def test_cleanup_requires_admin(api):
    app, task_id, base = api
    status, _ = call(base, "POST", "/evidence/exports/cleanup",
                     {"retention_seconds": 0}, token="op")
    assert status == 403
