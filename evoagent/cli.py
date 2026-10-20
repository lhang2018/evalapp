"""EvoAgent CLI。

用法：
  python -m evoagent.cli demo              # Mock 模型全链路演示（无需 API Key）
  python -m evoagent.cli task "目标文本" --intent general
  python -m evoagent.cli workflow          # 漫剧工作流演示
  python -m evoagent.cli serve             # 启动 REST API（供 Android 用）
  python -m evoagent.cli status [task_id]
  python -m evoagent.cli pause <task_id>   # 暂停运行中的任务
  python -m evoagent.cli resume <task_id> [--force]  # 恢复/重跑
"""

import argparse
import json
import os
from pathlib import Path

from .app import App
from .evolution.probes import PROBES as PROBE_NAMES


def _app(demo_confirm: bool = False) -> App:
    if demo_confirm:
        # 演示专用：本地写文件等低危操作由“模拟用户”确认；删除/支付等仍默认拒绝
        cb = lambda op, args: op in ("file_write", "workflow_node_confirm")  # noqa: E731
        return App(confirm_callback=cb)
    return App()


def cmd_demo() -> str:
    app = _app(demo_confirm=True)
    print("=== EvoAgent Demo（Mock 模型全链路）===")
    scenarios = [
        ("查询现在几点了", {"intent": "time_query",
                            "acceptance": [{"type": "tool_succeeded"}]}),
        ("计算 2024 + 2000 + 500", {"intent": "computation",
                                     "acceptance": [{"type": "tool_goal_reached"}]}),
        ("写一条笔记到 notes/demo.txt content=今天天气很好", {"intent": "note_taking"}),
    ]
    out = []
    for goal, kw in scenarios:
        task = app.controller.submit_task(goal, wait=True, **kw)
        out.append({"goal": goal, "intent": task.intent,
                    "status": task.task_status.value,
                    "result": (task.result or "")[:120],
                    "failure": task.failure_reason})
        print(f"[{task.task_status.value}] {goal}")
        if task.result:
            print(f"   -> {task.result[:160]}")
        if task.failure_reason:
            print(f"   -> 失败: {task.failure_reason}")
    # 记忆沉淀展示
    mems = app.memory.search("写笔记", domains=["note_taking"], limit=2)
    print(f"记忆条数: {len(mems)}")
    return json.dumps(out, ensure_ascii=False, indent=1)


def cmd_task(goal: str, intent: str, risk: str, wait: bool) -> str:
    from .core.task import RiskLevel
    app = _app()
    t = app.controller.submit_task(goal, intent=intent,
                                   risk_level=RiskLevel(risk), wait=wait)
    if not wait:
        print(f"已后台提交: {t.task_id}；用 status 查询进度")
        return t.task_id
    return json.dumps(t.to_dict(), ensure_ascii=False, indent=1)


def cmd_workflow() -> str:
    app = _app(demo_confirm=True)
    from .workflows.builtin import comic_drama_workflow
    from .core.task import Task, RiskLevel
    t = Task(user_goal="生成一段 30 秒 AI 漫剧", intent="comic_drama",
             risk_level=RiskLevel.MEDIUM)
    t.workflow_ref = "comic_drama_demo"
    t.acceptance_criteria = [{"type": "tool_succeeded", "tool": "mock_compose"}]
    task = app.controller.execute(t)
    print(f"工作流任务: [{task.task_status.value}]")
    for ev in task.result_evidence:
        print(json.dumps(ev, ensure_ascii=False, indent=1)[:800])
    return json.dumps({"status": task.task_status.value,
                       "result": task.result}, ensure_ascii=False)


def cmd_status(task_id: str) -> str:
    app = _app()
    if task_id:
        t = app.get_task(task_id)
        if not t:
            return json.dumps({"error": "任务不存在"}, ensure_ascii=False)
        events = app.bus.query(task_id=task_id, limit=20)
        return json.dumps({"task": t.to_dict(),
                           "events": [e.to_dict() for e in events]},
                          ensure_ascii=False, indent=1)
    app2 = _app()
    tasks = app2.db.list_tasks(limit=10)
    return json.dumps([{"id": t.task_id, "goal": t.user_goal[:40],
                        "status": t.task_status.value} for t in tasks],
                      ensure_ascii=False, indent=1)


def cmd_diagnose(task_id: str, probe: str) -> str:
    app = _app()
    if not task_id:
        cases = app.db.list_diagnostic_cases(limit=20)
        out = [{"case_id": c.case_id, "task_id": c.task_id,
                "status": c.status.value, "symptom": c.symptom}
               for c in cases]
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return ""
    case = app.diagnostics.diagnose_task(task_id)
    if case is None:
        return json.dumps({"error": "任务不存在或非失败状态"}, ensure_ascii=False)
    hypotheses = app.db.list_hypotheses(case.case_id)
    experiments = app.db.list_experiments(case.case_id)
    if probe:
        hypothesis = next((h for h in hypotheses if probe in h.suggested_probes),
                          hypotheses[0] if hypotheses else None)
        if hypothesis is None:
            return json.dumps({"error": "案例没有可探查的假设"},
                              ensure_ascii=False)
        exp = app.diagnostics.run_experiment(case, hypothesis, probe)
        experiments = app.db.list_experiments(case.case_id)
        hypotheses = app.db.list_hypotheses(case.case_id)
        print(f"探针 {probe}: {exp.result}")
    out = {"case": case.to_dict(),
           "hypotheses": [h.to_dict() for h in hypotheses],
           "experiments": [e.to_dict() for e in experiments]}
    print(f"诊断案例 {case.case_id} [{case.status.value}] 假设 {len(hypotheses)} 条")
    for h in hypotheses:
        print(f"  [{h.category.value}] {h.component} "
              f"conf={h.confidence} {h.status.value}: {h.statement}")
    return json.dumps(out, ensure_ascii=False, indent=1)


def cmd_pause(task_id: str) -> str:
    app = _app()
    out = app.controller.pause_task(task_id)
    print(json.dumps(out, ensure_ascii=False))
    return ""


def cmd_resume(task_id: str, force: bool) -> str:
    app = _app()
    try:
        t = app.controller.resume_task(task_id, force=force)
    except (PermissionError, ValueError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return ""
    if t is None:
        print(json.dumps({"error": "任务不存在"}, ensure_ascii=False))
        return ""
    print(f"恢复结果: [{t.task_status.value}] {t.task_id}")
    return json.dumps(t.to_dict(), ensure_ascii=False, indent=1)


def cmd_serve(host: str, port: int) -> str:
    app = _app()
    from .api.server import ApiServer
    srv = ApiServer(app, host=host, port=port)
    print(f"就绪: http://{host}:{port}  试试 GET /health 或 /tasks")
    srv.serve_forever()
    return "server stopped"


def main() -> None:
    p = argparse.ArgumentParser("evoagent")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("demo")
    t = sub.add_parser("task")
    t.add_argument("goal")
    t.add_argument("--intent", default="general")
    t.add_argument("--risk", default="low")
    t.add_argument("--no-wait", action="store_true")
    sub.add_parser("workflow")
    s = sub.add_parser("status")
    s.add_argument("task_id", nargs="?")
    pz = sub.add_parser("pause")
    pz.add_argument("task_id")
    rs = sub.add_parser("resume")
    rs.add_argument("task_id")
    rs.add_argument("--force", action="store_true")
    d = sub.add_parser("diagnose")
    d.add_argument("task_id", nargs="?")
    d.add_argument("--probe", default="",
                   help="运行固定探针: " + ", ".join(PROBE_NAMES))
    v = sub.add_parser("serve")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8300)
    args = p.parse_args()
    if args.cmd == "demo":
        cmd_demo()
    elif args.cmd == "task":
        cmd_task(args.goal, args.intent, args.risk, not args.no_wait)
    elif args.cmd == "workflow":
        cmd_workflow()
    elif args.cmd == "status":
        cmd_status(args.task_id)
    elif args.cmd == "pause":
        cmd_pause(args.task_id)
    elif args.cmd == "resume":
        cmd_resume(args.task_id, args.force)
    elif args.cmd == "diagnose":
        cmd_diagnose(args.task_id, args.probe)
    elif args.cmd == "serve":
        cmd_serve(args.host, args.port)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
