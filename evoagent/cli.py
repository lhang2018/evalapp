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
from .evolution.controller import PUBLISH_RULES
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


def cmd_candidates(candidate_id: str) -> str:
    app = _app()
    if candidate_id:
        cand = app.evolution.get_candidate(candidate_id)
        if cand is None:
            return json.dumps({"error": "候选不存在"}, ensure_ascii=False)
        print(json.dumps(cand.to_dict(), ensure_ascii=False, indent=1))
        return ""
    out = [{"candidate_id": c.candidate_id, "status": c.status.value,
            "risk": c.risk_level, "target": c.target_component,
            "problem": c.target_problem} for c in app.evolution.all_candidates()]
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return ""


def cmd_propose(component: str, problem: str, hypothesis: str,
                risk: str, baseline: str) -> str:
    app = _app()
    cand = app.evolution.propose_candidate(
        baseline_version=baseline, target_component=component,
        target_problem=problem, hypothesis=hypothesis)
    cand.risk_level = risk
    app.db.save_candidate(cand)
    out = {"candidate_id": cand.candidate_id, "status": cand.status.value,
           "risk_level": risk,
           "publish_rule": PUBLISH_RULES.get(risk, "strict_review")}
    print(json.dumps(out, ensure_ascii=False))
    return ""


def cmd_evaluate(candidate_id: str, dataset_id: str) -> str:
    app = _app()
    try:
        out = app.evolution.evaluate_candidate(candidate_id,
                                               dataset_id=dataset_id or None)
    except (KeyError, ValueError, RuntimeError) as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return ""


def cmd_approve(candidate_id: str, approver: str) -> str:
    app = _app()
    cand = app.evolution.get_candidate(candidate_id)
    if cand is None:
        return json.dumps({"error": "候选不存在"}, ensure_ascii=False)
    try:
        out = app.evolution.approve(cand, approver=approver)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    print(json.dumps(out, ensure_ascii=False))
    return ""


def cmd_deploy(candidate_id: str, actor: str, gray: int) -> str:
    app = _app()
    cand = app.evolution.get_candidate(candidate_id)
    if cand is None:
        return json.dumps({"error": "候选不存在"}, ensure_ascii=False)
    out = app.evolution.deploy_candidate(cand, actor=actor,
                                         gray_percent=gray)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return ""


def cmd_monitor(candidate_id: str, success_rate: float,
                error_rate: float, cost_ratio: float) -> str:
    app = _app()
    cand = app.evolution.get_candidate(candidate_id)
    if cand is None:
        return json.dumps({"error": "候选不存在"}, ensure_ascii=False)
    out = app.evolution.monitor(cand, {
        "success_rate": success_rate, "error_rate": error_rate,
        "cost_ratio": cost_ratio})
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return ""


def cmd_workflows(workflow_id: str) -> str:
    app = _app()
    if workflow_id:
        data = app.db.get_workflow_def(workflow_id)
        if data is None:
            return json.dumps({"error": "工作流不存在"}, ensure_ascii=False)
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return ""
    rows = [{"workflow_id": r["workflow_id"], "version": r["version"],
             "status": r["status"], "updated_at": r["updated_at"]}
            for r in app.db.list_workflow_defs()]
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return ""


def cmd_budget() -> str:
    app = _app()
    out = {
        "pools": app.ledger.report(),
        "limits": {
            "runtime_daily_cost": app.config.budget.runtime_daily_cost,
            "runtime_daily_model_calls": app.config.budget.runtime_daily_model_calls,
            "max_concurrent_tasks": app.config.budget.max_concurrent_tasks,
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return ""


def cmd_releases() -> str:
    app = _app()
    out = {"current": app.releases.current_release_id,
           "manifests": [m.to_dict() for m in
                         app.releases.manifests.values()],
           "deployments": app.releases.deployments,
           "rollbacks": app.releases.rollback_records}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return ""


def cmd_release_rollback(reason: str) -> str:
    app = _app()
    out = app.releases.rollback(reason)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return ""


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
    # 进化轨（PRD 46-54 / 80-82）
    cd = sub.add_parser("candidates")
    cd.add_argument("candidate_id", nargs="?")
    pr = sub.add_parser("propose")
    pr.add_argument("--component", required=True)
    pr.add_argument("--problem", required=True)
    pr.add_argument("--hypothesis", required=True)
    pr.add_argument("--risk", default="L2", choices=["L0", "L1", "L2", "L3", "L4"])
    pr.add_argument("--baseline", default="v0.1.0")
    ev = sub.add_parser("evaluate")
    ev.add_argument("candidate_id")
    ev.add_argument("--dataset", default="")
    ap = sub.add_parser("approve")
    ap.add_argument("candidate_id")
    ap.add_argument("--approver", default="operator")
    dp = sub.add_parser("deploy")
    dp.add_argument("candidate_id")
    dp.add_argument("--actor", default="operator")
    dp.add_argument("--gray", type=int, default=100)
    mo = sub.add_parser("monitor")
    mo.add_argument("candidate_id")
    mo.add_argument("--success-rate", type=float, default=1.0)
    mo.add_argument("--error-rate", type=float, default=0.0)
    mo.add_argument("--cost-ratio", type=float, default=1.0)
    wfs = sub.add_parser("workflows")
    wfs.add_argument("workflow_id", nargs="?")
    sub.add_parser("budget")
    sub.add_parser("releases")
    rr = sub.add_parser("release-rollback")
    rr.add_argument("--reason", default="manual rollback")
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
    elif args.cmd == "candidates":
        cmd_candidates(args.candidate_id)
    elif args.cmd == "propose":
        cmd_propose(args.component, args.problem, args.hypothesis,
                    args.risk, args.baseline)
    elif args.cmd == "evaluate":
        cmd_evaluate(args.candidate_id, args.dataset)
    elif args.cmd == "approve":
        cmd_approve(args.candidate_id, args.approver)
    elif args.cmd == "deploy":
        cmd_deploy(args.candidate_id, args.actor, args.gray)
    elif args.cmd == "monitor":
        cmd_monitor(args.candidate_id, args.success_rate,
                    args.error_rate, args.cost_ratio)
    elif args.cmd == "workflows":
        cmd_workflows(args.workflow_id)
    elif args.cmd == "budget":
        cmd_budget()
    elif args.cmd == "releases":
        cmd_releases()
    elif args.cmd == "release-rollback":
        cmd_release_rollback(args.reason)
    elif args.cmd == "serve":
        cmd_serve(args.host, args.port)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
