# -*- coding: utf-8 -*-
"""ALN-07-C 缺口：WAITING_USER(reconciliation) 人工裁决消费。

覆盖：只允许对未决目标裁决、记录不改状态不重放、幂等、改判需 override、
持久化、以及 HTTP 端点（admin 写/operator 读、非法/冲突拒绝）。
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.reconciliation_decisions import (
    ReconciliationDecisionConflict,
    ReconciliationDecisionError,
    ReconciliationDecisions,
    pending,
)
from evoagent.core.task import Task, TaskStatus

SUMMARY = {"decision": "await_user", "undeclared": ["fx1"],
           "uncertain": ["fx2"], "ambiguous_effects": ["fx3"],
           "unexecuted": ["step1"]}


def make_task(app, *, phase="reconciliation", status=TaskStatus.WAITING_USER):
    task = Task(user_goal="07-C 裁决探针", task_status=status)
    task.waiting_request = {
        "request_id": "recon_" + task.task_id,
        "phase": phase,
        "question": "请确认账本",
        "answered": False,
        "summary": SUMMARY,
    }
    app.db.save_task(task)
    return task


@pytest.fixture
def ctx(tmp_path):
    app = App(data_dir=str(tmp_path))
    task = make_task(app)
    yield app, task
    app.db.close()


def test_pending_lists_effect_and_step_targets(ctx):
    app, task = ctx
    info = pending(task)
    assert info["reconcilable"] is True
    kinds = {t["target_id"]: t["target_kind"] for t in info["targets"]}
    assert kinds == {"fx1": "effect", "fx2": "effect", "fx3": "effect",
                     "step1": "step"}


def test_record_partial_then_complete_never_replays(ctx):
    app, task = ctx
    store = ReconciliationDecisions(app.db)
    first = store.record(task, [{"target_id": "fx1", "decision": "executed"},
                                {"target_id": "step1", "decision": "retry"}],
                         actor="admin", reason="核对账本")
    assert first["auto_resumed"] is False
    assert first["resume_ready"] is False
    assert set(first["unresolved_remaining"]) == {"fx2", "fx3"}
    # 记录裁决不改变任务状态
    assert app.db.get_task(task.task_id).task_status == TaskStatus.WAITING_USER

    done = store.record(task, [{"target_id": "fx2", "decision": "not_executed"},
                               {"target_id": "fx3", "decision": "not_executed"}],
                        actor="admin", reason="核对账本")
    assert done["resume_ready"] is True
    assert done["unresolved_remaining"] == []
    assert done["auto_resumed"] is False


def test_idempotent_and_conflict_requires_override(ctx):
    app, task = ctx
    store = ReconciliationDecisions(app.db)
    store.record(task, [{"target_id": "fx1", "decision": "executed"}],
                 actor="admin", reason="r")
    again = store.record(task, [{"target_id": "fx1", "decision": "executed"}],
                         actor="admin", reason="r")
    assert again["already"] == ["fx1"]
    assert again["recorded"] == []

    with pytest.raises(ReconciliationDecisionConflict):
        store.record(task, [{"target_id": "fx1", "decision": "not_executed"}],
                     actor="admin", reason="r")
    changed = store.record(task, [{"target_id": "fx1", "decision": "not_executed"}],
                           actor="admin", reason="改判", override=True)
    assert changed["overridden"] == ["fx1"]
    row = next(r for r in store.for_task(task.task_id) if r["target_id"] == "fx1")
    assert row["decision"] == "not_executed"


def test_invalid_inputs_rejected(ctx):
    app, task = ctx
    store = ReconciliationDecisions(app.db)
    with pytest.raises(ReconciliationDecisionError):
        store.record(task, [{"target_id": "nope", "decision": "executed"}],
                     actor="a", reason="r")
    with pytest.raises(ReconciliationDecisionError):
        store.record(task, [{"target_id": "fx1", "decision": "maybe"}],
                     actor="a", reason="r")
    with pytest.raises(ReconciliationDecisionError):
        store.record(task, [], actor="a", reason="r")
    with pytest.raises(ReconciliationDecisionError):
        store.record(task, [{"target_id": "fx1", "decision": "executed"}],
                     actor="a", reason="  ")
    with pytest.raises(ReconciliationDecisionError):
        store.record(task, [{"target_id": "fx1", "decision": "executed"},
                            {"target_id": "fx1", "decision": "retry"}],
                     actor="a", reason="r")


def test_non_reconciliation_task_rejected(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        running = make_task(app, phase="reconciliation",
                            status=TaskStatus.RUNNING)
        with pytest.raises(ReconciliationDecisionError):
            ReconciliationDecisions(app.db).record(
                running, [{"target_id": "fx1", "decision": "executed"}],
                actor="a", reason="r")
        other = make_task(app, phase="approval")
        with pytest.raises(ReconciliationDecisionError):
            ReconciliationDecisions(app.db).record(
                other, [{"target_id": "fx1", "decision": "executed"}],
                actor="a", reason="r")
    finally:
        app.db.close()


def test_decisions_persist_across_instances(ctx):
    app, task = ctx
    ReconciliationDecisions(app.db).record(
        task, [{"target_id": "fx2", "decision": "retry"}], actor="a", reason="r")
    rows = ReconciliationDecisions(app.db).for_task(task.task_id)
    assert [r["target_id"] for r in rows] == ["fx2"]


def test_http_endpoints(tmp_path):
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "op"
    app.config.api_admin_token = "root"
    task = make_task(app)
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.port}"

    def call(method, path, payload=None, token="root"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(base + path, data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    try:
        status, view = call("GET", f"/tasks/{task.task_id}/reconciliation")
        assert status == 200
        assert view["reconcilable"] is True
        assert len(view["targets"]) == 4

        # operator 受既有"任务归属当前服务主体"规则限制 → 403（平台既有语义）
        status, _ = call("GET", f"/tasks/{task.task_id}/reconciliation",
                         token="op")
        assert status == 403

        status, result = call(
            "POST", f"/tasks/{task.task_id}/reconciliation/decisions",
            {"decisions": [{"target_id": "fx1", "decision": "executed"}],
             "reason": "核对账本"})
        assert status == 200, result
        assert result["recorded"] == ["fx1"]

        status, _ = call(
            "POST", f"/tasks/{task.task_id}/reconciliation/decisions",
            {"decisions": [{"target_id": "fx1", "decision": "executed"}],
             "reason": "r"}, token="op")
        assert status == 403

        status, _ = call(
            "POST", f"/tasks/{task.task_id}/reconciliation/decisions",
            {"decisions": [{"target_id": "fx1", "decision": "not_executed"}],
             "reason": "r"})
        assert status == 409

        status, _ = call(
            "POST", f"/tasks/{task.task_id}/reconciliation/decisions",
            {"decisions": [{"target_id": "nope", "decision": "executed"}],
             "reason": "r"})
        assert status == 400
    finally:
        server.shutdown()
        app.db.close()
