"""EvoAgent CLI。

用法：
  python -m evoagent.cli demo              # Mock 模型全链路演示（无需 API Key）
  python -m evoagent.cli task "目标文本" --intent general
  python -m evoagent.cli workflow          # 漫剧工作流演示
  python -m evoagent.cli serve             # 启动 REST API（供 Android 用）
  python -m evoagent.cli status [task_id]
  python -m evoagent.cli pause <task_id>   # 暂停运行中的任务
  python -m evoagent.cli resume <task_id> [--force]  # 恢复/重跑
  python -m evoagent.cli migrate <old> <new> [--apply]  # 模型迁移执行链
  python -m evoagent.cli memory-eval       # 记忆质量评测
  python -m evoagent.cli drift             # 环境漂移检测
  python -m evoagent.cli workflow-extract  # 成功任务 → 工作流候选
  python -m evoagent.cli workflow-publish <candidate_id> --args '{...}'
"""

import argparse
import json
import os
from pathlib import Path
from typing import Optional

from .app import App
from .evolution.controller import PUBLISH_RULES
from .evolution.probes import PROBES as PROBE_NAMES


def _fail(message: str) -> str:
    print(json.dumps({"error": message}, ensure_ascii=False))
    return ""


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
    print(json.dumps(t.to_dict(), ensure_ascii=False, indent=1))
    return ""


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
            return _fail("任务不存在")
        events = app.bus.query(task_id=task_id, limit=20)
        print(json.dumps({"task": t.to_dict(),
                          "events": [e.to_dict() for e in events]},
                         ensure_ascii=False, indent=1))
        return ""
    tasks = app.db.list_tasks(limit=10)
    print(json.dumps([{"id": t.task_id, "goal": t.user_goal[:40],
                       "status": t.task_status.value} for t in tasks],
                     ensure_ascii=False, indent=1))
    return ""


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
        return _fail("任务不存在或非失败状态")
    hypotheses = app.db.list_hypotheses(case.case_id)
    experiments = app.db.list_experiments(case.case_id)
    if probe:
        hypothesis = next((h for h in hypotheses if probe in h.suggested_probes),
                          hypotheses[0] if hypotheses else None)
        if hypothesis is None:
            return _fail("案例没有可探查的假设")
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


def cmd_diagnose_assist(case_id: str, limit: int) -> str:
    app = _app()
    try:
        out = app.diagnostics.suggest_hypotheses(case_id, max_items=limit)
    except KeyError:
        return _fail("诊断案例不存在")
    except RuntimeError as e:
        return _fail(str(e))
    print(f"模型辅助假设（advisory）case={case_id} "
          f"created={len(out['created'])} skipped={len(out['skipped'])}")
    for h in out["created"]:
        print(f"  [{h['category']}] {h['component']} "
              f"conf={h['confidence']} source={h['source']}: {h['statement']}")
    for s in out["skipped"]:
        print(f"  skipped: {s}")
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
            return _fail("候选不存在")
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
        return _fail(str(e))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return ""


def cmd_approve(candidate_id: str, approver: str) -> str:
    app = _app()
    cand = app.evolution.get_candidate(candidate_id)
    if cand is None:
        return _fail("候选不存在")
    try:
        out = app.evolution.approve(cand, approver=approver)
    except Exception as e:  # noqa: BLE001
        return _fail(str(e))
    print(json.dumps(out, ensure_ascii=False))
    return ""


def cmd_deploy(candidate_id: str, actor: str, gray: int) -> str:
    app = _app()
    cand = app.evolution.get_candidate(candidate_id)
    if cand is None:
        return _fail("候选不存在")
    out = app.evolution.deploy_candidate(cand, actor=actor,
                                         gray_percent=gray)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return ""


def cmd_monitor(candidate_id: str, success_rate: Optional[float] = None,
                error_rate: Optional[float] = None,
                cost_ratio: Optional[float] = None) -> str:
    app = _app()
    cand = app.evolution.get_candidate(candidate_id)
    if cand is None:
        return _fail("候选不存在")
    metrics = None
    if any(v is not None for v in (success_rate, error_rate, cost_ratio)):
        metrics = {}
        if success_rate is not None:
            metrics["success_rate"] = success_rate
        if error_rate is not None:
            metrics["error_rate"] = error_rate
        if cost_ratio is not None:
            metrics["cost_ratio"] = cost_ratio
    out = app.evolution.monitor(cand, metrics)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return ""


def cmd_workflows(workflow_id: str) -> str:
    app = _app()
    if workflow_id:
        data = app.db.get_workflow_def(workflow_id)
        if data is None:
            return _fail("工作流不存在")
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return ""
    rows = [{"workflow_id": r["workflow_id"], "version": r["version"],
             "status": r["status"], "updated_at": r["updated_at"]}
            for r in app.db.list_workflow_defs()]
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return ""


def cmd_workflow_extract(min_support: Optional[int], limit: int) -> str:
    app = _app()
    out = app.workflow_extractor.extract(min_support=min_support, limit=limit)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return ""


def cmd_workflow_candidates(status: str, limit: int) -> str:
    app = _app()
    rows = app.workflow_extractor.list(status=status or None, limit=limit)
    print(json.dumps(rows, ensure_ascii=False, indent=1, default=str))
    return ""


def _load_step_args(raw: str):
    """解析 --args；空串表示不绑定，返回 (None, "")。"""
    if not raw.strip():
        return None, ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"--args 不是合法 JSON: {e}"
    if not isinstance(data, dict):
        return None, "--args 必须是 JSON 对象"
    return data, ""


def cmd_workflow_action(action: str, candidate_id: str, reason: str,
                        args_json: str = "") -> str:
    app = _app()
    bindings, error = _load_step_args(args_json)
    if error:
        return _fail(error)
    extractor = app.workflow_extractor
    try:
        if action == "evaluate":
            out = extractor.evaluate(candidate_id, bindings)
        elif action == "approve":
            out = extractor.approve(candidate_id, reason=reason)
        elif action == "reject":
            out = extractor.reject(candidate_id, reason=reason)
        else:
            out = extractor.publish(candidate_id, reason=reason,
                                    args_by_step=bindings)
    except KeyError:
        return _fail("工作流候选不存在")
    except ValueError as e:
        return _fail(str(e))
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return ""


def cmd_budget() -> str:
    app = _app()
    cfg = app.config.budget
    out = {
        "pools": app.ledger.report(),
        "limits": {
            "task_max_model_calls": cfg.task_max_model_calls,
            "task_max_tool_calls": cfg.task_max_tool_calls,
            "task_max_cost": cfg.task_max_cost,
            "task_max_seconds": cfg.task_max_seconds,
            "task_max_cost_per_model_call": cfg.task_max_cost_per_model_call,
            "task_max_tool_calls_per_tool": cfg.task_max_tool_calls_per_tool,
            "workflow_max_model_calls": cfg.workflow_max_model_calls,
            "workflow_max_tool_calls": cfg.workflow_max_tool_calls,
            "workflow_max_cost": cfg.workflow_max_cost,
            "workflow_max_seconds": cfg.workflow_max_seconds,
            "candidate_max_model_calls": cfg.candidate_max_model_calls,
            "candidate_max_tool_calls": cfg.candidate_max_tool_calls,
            "candidate_max_cost": cfg.candidate_max_cost,
            "candidate_max_seconds": cfg.candidate_max_seconds,
            "evolution_daily_cost": cfg.evolution_daily_cost,
            "evolution_daily_model_calls": cfg.evolution_daily_model_calls,
            "evolution_monthly_cost": cfg.evolution_monthly_cost,
            "evolution_monthly_model_calls": cfg.evolution_monthly_model_calls,
            "runtime_daily_cost": cfg.runtime_daily_cost,
            "runtime_daily_model_calls": cfg.runtime_daily_model_calls,
            "provider_daily_cost": cfg.provider_daily_cost,
            "provider_daily_model_calls": cfg.provider_daily_model_calls,
            "expert_daily_cost": cfg.expert_daily_cost,
            "expert_daily_model_calls": cfg.expert_daily_model_calls,
            "max_concurrent_tasks": cfg.max_concurrent_tasks,
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return ""


def cmd_costs() -> str:
    app = _app()
    from .core.budget import CostReporter
    print(json.dumps(CostReporter(app.db, app.evolution).report(),
                     ensure_ascii=False, indent=1, default=str))
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


def cmd_logs(layer: str, task_id: str, limit: int, purge: bool,
             delete_task: str) -> str:
    app = _app()
    if purge:
        out = app.logs.purge()
        app.audit.record("operator", "logs.purge", target="logs",
                         detail=out)
        print(json.dumps({"deleted": out}, ensure_ascii=False, indent=1))
        return ""
    if delete_task:
        out = app.logs.delete_task(delete_task)
        app.audit.record("operator", "logs.delete_task",
                         target=delete_task, detail=out)
        print(json.dumps({"task_id": delete_task, "deleted": out},
                         ensure_ascii=False, indent=1))
        return ""
    if layer == "execution_trace":
        rows = app.db.list_traces(task_id=task_id or None, limit=limit)
        print(json.dumps(rows, ensure_ascii=False, indent=1, default=str))
        return ""
    if layer == "diagnostic_evidence":
        rows = app.evidence.list(task_id=task_id or None, limit=limit)
        for row in rows:
            row["exportable"] = app.evidence.exportable(row)
        print(json.dumps(rows, ensure_ascii=False, indent=1, default=str))
        return ""
    print(json.dumps({"layers": app.logs.stats("admin")["layers"],
                      "counts": app.logs.counts()},
                     ensure_ascii=False, indent=1))
    return ""


def cmd_alerts() -> str:
    app = _app()
    snap = app.metrics.snapshot()
    print(json.dumps({"alerts": snap.get("alerts", []),
                      "drift": (snap.get("drift") or {}).get("status")},
                     ensure_ascii=False, indent=1))
    return ""


def cmd_migrate(old: str, new: str, dataset_id: str, apply_now: bool,
                gray: int) -> str:
    """模型迁移执行链：兼容性 → 回归对比 → 发布组合 →（可选）受控启用。"""
    app = _app()
    try:
        mig = app.migrations.create(old, new, dataset_id=dataset_id or None)
        print(f"迁移 {mig.migration_id}: {mig.status}")
        if mig.status == "blocked":
            print(json.dumps(mig.to_dict(), ensure_ascii=False, indent=1))
            return ""
        mig = app.migrations.run_regression(
            mig.migration_id, dataset_id=dataset_id or None)
        comparison = mig.regression.get("comparison", {})
        print(f"回归对比: passed={comparison.get('passed')} status={mig.status}")
        if mig.status == "blocked":
            print(json.dumps(mig.to_dict(), ensure_ascii=False, indent=1))
            return ""
        mig = app.migrations.prepare_release(mig.migration_id)
        rollback = next((s for s in mig.steps
                         if s.get("step") == "rollback"), {})
        print(f"发布组合: {mig.release_id}（回滚目标 "
              f"{rollback.get('detail', {}).get('target')}）")
        if apply_now:
            out = app.migrations.apply(mig.migration_id, actor="cli",
                                       gray_percent=gray)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return ""
        print(json.dumps(mig.to_dict(), ensure_ascii=False, indent=1))
    except (KeyError, ValueError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
    return ""


def cmd_migrations(migration_id: str) -> str:
    app = _app()
    if migration_id:
        mig = app.migrations.get(migration_id)
        if mig is None:
            print(json.dumps({"error": "迁移记录不存在"},
                             ensure_ascii=False))
            return ""
        print(json.dumps(mig.to_dict(), ensure_ascii=False, indent=1))
        return ""
    rows = [{"migration_id": m.migration_id, "status": m.status,
             "old_model_id": m.old_model_id,
             "new_model_id": m.new_model_id,
             "release_id": m.release_id,
             "blocked_reasons": m.blocked_reasons,
             "created_at": m.created_at} for m in app.migrations.list()]
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return ""


def cmd_memory_eval(persist: bool) -> str:
    app = _app()
    report = app.run_memory_eval(persist=persist)
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return ""


def cmd_drift(window: float, emit: bool) -> str:
    app = _app()
    if window:
        app.drift.policy.window_seconds = window
    report = app.drift.detect(emit=emit)
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return ""


def cmd_replay(task_id: str, limit: int) -> str:
    app = _app()
    try:
        out = app.replay.build(task_id, max_items=limit)
    except KeyError:
        return _fail("任务不存在")
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return ""


def cmd_backup(action: str, ref: str, name: str) -> str:
    app = _app()
    try:
        if action == "create":
            out = app.backups.create(name or ref or None)
        elif action == "verify":
            out = app.backups.verify(ref)
        elif action == "restore":
            active = [t.task_id for t in app.db.list_tasks(limit=1000)
                      if not t.is_terminal()]
            if active:
                print(json.dumps({"error": "存在未结束任务，拒绝恢复",
                                  "active_tasks": active[:20]},
                                 ensure_ascii=False))
                return ""
            out = app.backups.restore(ref)
        else:
            out = {"backups": app.backups.list(),
                   "directory": str(app.backups.backup_dir)}
    except (FileNotFoundError, ValueError) as e:
        return _fail(str(e))
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
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
    da = sub.add_parser("diagnose-assist")
    da.add_argument("case_id")
    da.add_argument("--limit", type=int, default=3,
                    help="最多生成条数（上限 5）")
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
    mo.add_argument("--success-rate", type=float, default=None)
    mo.add_argument("--error-rate", type=float, default=None)
    mo.add_argument("--cost-ratio", type=float, default=None)
    wfs = sub.add_parser("workflows")
    wfs.add_argument("workflow_id", nargs="?")
    # 成功任务 → 工作流候选（Arch 16.2）
    wex = sub.add_parser("workflow-extract")
    wex.add_argument("--min-support", type=int, default=None)
    wex.add_argument("--limit", type=int, default=200)
    wcl = sub.add_parser("workflow-candidates")
    wcl.add_argument("--status", default="")
    wcl.add_argument("--limit", type=int, default=50)
    for name in ("workflow-evaluate", "workflow-approve",
                 "workflow-reject", "workflow-publish"):
        wp = sub.add_parser(name)
        wp.add_argument("candidate_id")
        wp.add_argument("--reason", default="")
        if name in ("workflow-evaluate", "workflow-publish"):
            wp.add_argument("--args", default="",
                            help="节点参数绑定 JSON，如 "
                                 "'{\"step_1\":{\"expression\":\"3+4\"}}'")
    sub.add_parser("budget")
    sub.add_parser("costs")
    lg = sub.add_parser("logs")
    lg.add_argument("--layer", default="")
    lg.add_argument("--task", default="")
    lg.add_argument("--limit", type=int, default=50)
    lg.add_argument("--purge", action="store_true")
    lg.add_argument("--delete-task", default="")
    sub.add_parser("alerts")
    sub.add_parser("releases")
    rr = sub.add_parser("release-rollback")
    rr.add_argument("--reason", default="manual rollback")
    # 模型迁移 / 记忆评测 / 漂移检测（PRD 20 / 28 / 44）
    mg = sub.add_parser("migrate")
    mg.add_argument("old_model_id")
    mg.add_argument("new_model_id")
    mg.add_argument("--dataset", default="")
    mg.add_argument("--apply", action="store_true")
    mg.add_argument("--gray", type=int, default=100)
    mgs = sub.add_parser("migrations")
    mgs.add_argument("migration_id", nargs="?")
    me = sub.add_parser("memory-eval")
    me.add_argument("--no-persist", action="store_true")
    dr = sub.add_parser("drift")
    dr.add_argument("--window", type=float, default=0)
    dr.add_argument("--emit", action="store_true")
    rp = sub.add_parser("replay")
    rp.add_argument("task_id")
    rp.add_argument("--limit", type=int, default=2000)
    bk = sub.add_parser("backup")
    bk.add_argument("action", nargs="?", default="create",
                    choices=["create", "verify", "restore", "list"])
    bk.add_argument("ref", nargs="?", default="")
    bk.add_argument("--name", default="")
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
    elif args.cmd == "diagnose-assist":
        cmd_diagnose_assist(args.case_id, args.limit)
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
    elif args.cmd == "workflow-extract":
        cmd_workflow_extract(args.min_support, args.limit)
    elif args.cmd == "workflow-candidates":
        cmd_workflow_candidates(args.status, args.limit)
    elif args.cmd == "workflow-evaluate":
        cmd_workflow_action("evaluate", args.candidate_id, args.reason,
                            args.args)
    elif args.cmd == "workflow-approve":
        cmd_workflow_action("approve", args.candidate_id, args.reason)
    elif args.cmd == "workflow-reject":
        cmd_workflow_action("reject", args.candidate_id, args.reason)
    elif args.cmd == "workflow-publish":
        cmd_workflow_action("publish", args.candidate_id, args.reason,
                            args.args)
    elif args.cmd == "budget":
        cmd_budget()
    elif args.cmd == "costs":
        cmd_costs()
    elif args.cmd == "logs":
        cmd_logs(args.layer, args.task, args.limit, args.purge,
                 args.delete_task)
    elif args.cmd == "alerts":
        cmd_alerts()
    elif args.cmd == "releases":
        cmd_releases()
    elif args.cmd == "release-rollback":
        cmd_release_rollback(args.reason)
    elif args.cmd == "migrate":
        cmd_migrate(args.old_model_id, args.new_model_id,
                    args.dataset, args.apply, args.gray)
    elif args.cmd == "migrations":
        cmd_migrations(args.migration_id)
    elif args.cmd == "memory-eval":
        cmd_memory_eval(not args.no_persist)
    elif args.cmd == "drift":
        cmd_drift(args.window, args.emit)
    elif args.cmd == "replay":
        cmd_replay(args.task_id, args.limit)
    elif args.cmd == "backup":
        cmd_backup(args.action, args.ref, args.name)
    elif args.cmd == "serve":
        cmd_serve(args.host, args.port)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
