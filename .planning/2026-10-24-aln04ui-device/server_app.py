#!/usr/bin/env python3
"""ALN-04-UI 真机四路径演示服务端。

在 .evoagent-update 数据目录上注册一个含 requires_confirmation 节点的
审批流夹具，创建三个 WAITING_USER 任务（批准/拒绝/过期各一），并以
EVOAGENT_ADMIN_TOKEN 启动 REST。仅本地演示环境使用。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evoagent.app import App
from evoagent.api.server import ApiServer
from evoagent.core.task import Task
from evoagent.tools.contract import ToolContract
from evoagent.workflows.models import WorkflowDef, WorkflowNode

DATA_DIR = ".evoagent-update"
LONG_TARGET = ("长参数滚动查看验证：" + "审批绑定全文应可滚动阅读。" * 12)


def register_flow(app: App) -> None:
    calls = []

    def runner(args):
        calls.append(dict(args))
        from evoagent.tools.contract import ToolResult
        return ToolResult(ok=True, data="审批后动作已执行", submitted=True,
                          executed=True, contract_valid=True,
                          goal_reached=True, independently_verified=True,
                          evidence={"approval_flow": True})

    app.tools.register(
        ToolContract(tool_id="approval_probe",
                     description="04-UI 真机审批演示工具",
                     input_schema={"type": "object"},
                     side_effect_level="none"),
        runner)
    wf = WorkflowDef(workflow_id="approval-demo", nodes=[
        WorkflowNode(node_id="probe", tool="approval_probe",
                     args={"target": LONG_TARGET},
                     requires_confirmation=True)])
    app._workflows[wf.workflow_id] = wf
    app._approval_calls = calls


def wait_task(app: App, goal: str) -> Task:
    task = app.controller.execute(
        Task(user_goal=goal, workflow_ref="approval-demo"),
        intent_hint="general")
    assert task.task_status.value == "SUSPENDED", task.task_status
    assert task.waiting_approvals, "审批任务必须处于等待状态"
    return task


def main() -> int:
    app = App(data_dir=DATA_DIR)
    register_flow(app)
    tasks = {
        "approve": wait_task(app, "审批演示：批准路径"),
        "reject": wait_task(app, "审批演示：拒绝路径"),
        "expire": wait_task(app, "审批演示：过期路径"),
    }
    # 过期路径：把 expire 任务的审批有效期改到过去（与
    # tests/test_async_approvals.py::test_expired_pending_request_cannot_be_approved 同法）
    aid = tasks["expire"].waiting_approvals[0]
    with app.db._lock:
        app.db._conn.execute(
            "UPDATE approvals SET expires_at=? WHERE approval_id=?",
            (time.time() - 1, aid))
        app.db._conn.commit()
    print("waiting tasks:", {k: (v.task_id, v.waiting_approvals)
                             for k, v in tasks.items()}, flush=True)
    srv = ApiServer(app, host="0.0.0.0", port=8300)
    print("ready on 8300 (admin token enabled)", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
