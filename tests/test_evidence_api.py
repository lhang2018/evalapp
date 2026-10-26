"""ALN-16-Q：聚合观测与受控导出/删除 API 接线测试。

覆盖：真实 HTTP 聚合查询；导出→审计→下载字节摘要匹配；删除 dry-run/执行
逐层计数与残留对账；跨主体/越权拒绝（不能从 body 伪造 principal）；
失败/拒绝路径明确（无 reason、摘要不一致、未知 task、非 admin）。
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
    task = Task(user_goal="16-Q 证据探针", task_status=TaskStatus.SUCCEEDED)
    app.db.save_task(task)
    app.bus.emit(event_type="probe_runtime", task_id=task.task_id,
                 component_id="test", payload={"step": 1})
    app.audit.record("tester", "probe_action", target=task.task_id,
                     detail={"note": "evidence"})
    app.db.save_side_effect("fx_probe", task.task_id, "run_probe",
                            "file_write", "key-1", "写文件", "planned")
    app.db._conn.execute(
        "INSERT INTO approvals (approval_id, task_id, op, target, scope,"
        " action_summary, status, granted_by, created_at)"
        " VALUES ('appr_probe', ?, 'file_write', 'f.txt', 'write',"
        " '写文件', 'pending', 'user', 0.0)", (task.task_id,))
    app.db._conn.commit()
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, task.task_id, f"http://127.0.0.1:{server.port}"
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


def test_aggregate_query_real_evidence(api):
    app, task_id, base = api
    status, payload = call(base, "GET", f"/evidence/tasks/{task_id}")
    assert status == 200
    assert payload["task_id"] == task_id
    assert payload["summary"]["event_count"] >= 4
    kinds = payload["summary"]["by_type"]
    assert "probe_runtime" in kinds
    assert payload["summary"]["by_privacy"]
    ids = {item["event_id"] for item in payload["events"]}
    assert ids
    assert payload["reconciliation"]["task_id"] == task_id
    # operator 无权看全局聚合
    status, _ = call(base, "GET", f"/evidence/tasks/{task_id}", token="op")
    assert status == 403
    status, _ = call(base, "GET", "/evidence/tasks/nope")
    assert status == 404


def test_export_audit_and_download_digest_match(api):
    app, task_id, base = api
    status, payload = call(base, "POST", "/evidence/export", {
        "task_id": task_id, "reason": "用户导出"})
    assert status == 200 and payload["ok"] is True
    assert payload["events_digest"] and payload["sha256"]
    assert payload["audit_persisted"] is True and payload["audit_id"]
    assert payload["skipped"]["privacy"] >= 0
    export_id = payload["export_id"]
    status, download = call(
        base, "GET", f"/evidence/exports/{export_id}/download")
    assert status == 200
    raw = base64.b64decode(download["bytes_b64"])
    assert hashlib.sha256(raw).hexdigest() == download["sha256"] == payload["sha256"]
    assert download["events_digest"] == payload["events_digest"]
    assert download["audit_match"] is True
    # 非法导出标识与不存在文件
    status, _ = call(base, "GET", "/evidence/exports/not-an-id/download")
    assert status == 400
    status, _ = call(base, "GET", "/evidence/exports/exp_0123456789ab/download")
    assert status == 404


def test_export_rejections(api):
    app, task_id, base = api
    status, _ = call(base, "POST", "/evidence/export",
                     {"task_id": task_id}, token="root")
    assert status == 400        # 缺 reason
    status, _ = call(base, "POST", "/evidence/export",
                     {"task_id": task_id, "reason": "x", "principal": "other"})
    assert status == 403        # 不能从 body 伪造 principal
    status, _ = call(base, "POST", "/evidence/export",
                     {"task_id": "nope", "reason": "x"})
    assert status == 404
    status, _ = call(base, "POST", "/evidence/export",
                     {"task_id": task_id, "reason": "x"}, token="")
    assert status == 401


def test_deletion_dry_run_then_execute_with_reconciliation(api):
    app, task_id, base = api
    status, dry = call(base, "POST", "/evidence/deletion",
                       {"task_id": task_id, "reason": "清理", "dry_run": True})
    assert status == 200 and dry["dry_run"] is True
    assert dry["plan"]["digest"] and dry["plan"]["event_ids"]
    assert dry["reconciliation_before"]["residuals"]["runtime_events"] >= 1
    status, bad = call(base, "POST", "/evidence/deletion", {
        "task_id": task_id, "reason": "清理", "dry_run": False,
        "plan_digest": "sha256:not-the-plan"})
    assert status == 409
    status, done = call(base, "POST", "/evidence/deletion", {
        "task_id": task_id, "reason": "清理", "dry_run": False,
        "plan_digest": dry["plan"]["digest"]})
    assert status == 200 and done["ok"] is True
    assert done["execution"]["counts"]
    assert done["reconciliation"]["fully_deleted"] is False
    assert "audit_log" in done["retained_layers"]
    assert "不宣称跨备份" in done["note"]
    # 执行器实际覆盖层归零；未覆盖的副作用/审批/审计层如实保留
    after = done["reconciliation"]["residuals"]
    assert after["runtime_events"] == 0
    assert set(done["retained_layers"]) >= {"audit_log"}
    assert done["reconciliation"]["fully_deleted"] is False


def test_deletion_rejections(api):
    app, task_id, base = api
    status, _ = call(base, "POST", "/evidence/deletion",
                     {"task_id": task_id, "reason": "x", "dry_run": True},
                     token="op")
    assert status == 403
    status, _ = call(base, "POST", "/evidence/deletion",
                     {"task_id": task_id, "principal": "other",
                      "reason": "x", "dry_run": True})
    assert status == 403
    status, _ = call(base, "POST", "/evidence/deletion",
                     {"task_id": "nope", "reason": "x", "dry_run": True})
    assert status == 404
    status, _ = call(base, "POST", "/evidence/deletion",
                     {"task_id": task_id, "reason": "x", "dry_run": True},
                     token="")
    assert status == 401
