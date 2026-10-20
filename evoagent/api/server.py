"""REST API（供 Android 客户端与管理端使用）。

纯标准库实现，零外部依赖。所有响应为 JSON。
任务执行默认后台运行（POST /tasks 立即返回 task_id），
Android 端通过轮询任务状态与事件流展示进度（PRD 89.2 任务详情）。
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from ..app import App
from ..core.budget import CostReporter
from ..core.log_layers import role_can_read
from ..core.task import RiskLevel, TaskStatus
from ..evolution.candidates import IllegalCandidateTransition
from ..evolution.controller import PUBLISH_RULES
from .admin import ADMIN_HTML
from .auth import ApiAuth
from .events_sync import BatchError, validate_batch

CLIENT_REQUEST_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
# 幂等提交的检查-创建-登记需要原子化（单进程 API 假设）
_submit_lock = threading.Lock()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, code: int, text: str, content_type: str) -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorize(self, method: str, path: str):
            res = ApiAuth(app.config.api_token,
                          app.config.api_admin_token).check(
                              self.headers.get("Authorization"), method, path)
            if not res.ok:
                self._json(res.status, {"error": res.error})
                return None
            return res

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        def log_message(self, fmt, *args):  # 安静一些
            pass

        def _query(self) -> Dict[str, str]:
            parsed = urlparse(self.path)
            return {k: v[0] for k, v in parse_qs(parsed.query).items()}

        # ---------- GET ----------
        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            qs = self._query()
            auth = self._authorize("GET", path)
            if auth is None:
                return
            if path in ("/admin", "/"):
                return self._text(200, ADMIN_HTML, "text/html; charset=utf-8")
            if path == "/metrics":
                if (qs.get("format") or "").lower() in ("prometheus", "text"):
                    return self._text(200, app.metrics.prometheus(),
                                      "text/plain; version=0.0.4; charset=utf-8")
                return self._json(200, app.metrics.snapshot())
            if path == "/alerts":
                snap = app.metrics.snapshot()
                policy = app.metrics.alert_policy
                return self._json(200, {
                    "alerts": snap.get("alerts", []),
                    "policy": {k: getattr(policy, k)
                               for k in policy.__dataclass_fields__}})
            if path == "/logs/layers":
                return self._json(200, app.logs.stats(auth.role))
            if path == "/logs/traces":
                if not role_can_read("execution_trace", auth.role):
                    return self._json(403, {"error": "需要 execution_trace 读取权限"})
                limit = min(1000, int(qs.get("limit") or 100))
                return self._json(200, {"traces": app.db.list_traces(
                    task_id=qs.get("task_id") or None, limit=limit)})
            if path == "/logs/evidence":
                if not role_can_read("diagnostic_evidence", auth.role):
                    return self._json(403, {"error": "需要 admin 权限"})
                limit = min(1000, int(qs.get("limit") or 100))
                rows = app.evidence.list(task_id=qs.get("task_id") or None,
                                         limit=limit)
                for row in rows:
                    row["exportable"] = app.evidence.exportable(row)
                return self._json(200, {"evidence": rows})
            if path == "/audit":
                limit = int(qs.get("limit") or 100)
                return self._json(200, {"audit": app.audit.list(limit=limit)})
            if path == "/costs":
                limit = int(qs.get("limit") or 500)
                return self._json(200, CostReporter(
                    app.db, app.evolution).report(limit=limit))
            if path == "/health":
                return self._json(200, app.health())
            if path == "/tasks":
                tasks = app.db.list_tasks()
                return self._json(200, {"tasks": [t.to_dict() for t in tasks]})
            if path.startswith("/tasks/"):
                parts = [p for p in path.split("/") if p]  # ['tasks', tid, ...]
                tid = parts[1] if len(parts) > 1 else ""
                if len(parts) >= 3 and parts[2] == "events":
                    events = app.bus.query(task_id=tid, limit=100)
                    return self._json(200, {"events": [e.to_dict() for e in events]})
                if len(parts) >= 3 and parts[2] == "replay":
                    if not role_can_read("execution_trace", auth.role):
                        return self._json(
                            403, {"error": "需要 execution_trace 读取权限"})
                    limit = min(5000, int(qs.get("limit") or 2000))
                    try:
                        out = app.replay.build(tid, max_items=limit)
                    except KeyError:
                        return self._json(404, {"error": "任务不存在"})
                    return self._json(200, out)
                t = app.get_task(tid)
                if not t:
                    return self._json(404, {"error": "任务不存在"})
                return self._json(200, t.to_dict())
            if path == "/experts":
                data = []
                for e in app.experts.all():
                    data.append({"expert_id": e.expert_id,
                                 "description": e.domain_description,
                                 "capabilities": e.capabilities,
                                 "applicable_intents": e.applicable_intents,
                                 "tools": e.tool_whitelist, "version": e.version})
                return self._json(200, {"experts": data})
            if path == "/version":
                m = app.release_manifest
                return self._json(200, {**m.to_dict(),
                                        "current": app.releases.current_release_id})
            if path == "/models":
                return self._json(200, {
                    "default_model_id": app.router.default_model_id,
                    "routes": app.router.routes(),
                    "models": [_model_summary(p) for p in
                               app.model_registry.all()]})
            if path == "/models/migrations":
                limit = min(200, int(qs.get("limit") or 50))
                return self._json(200, {
                    "migrations": [m.to_dict() for m in
                                   app.migrations.list(limit=limit)]})
            if path.startswith("/models/migrations/"):
                mid = path.split("/")[3]
                mig = app.migrations.get(mid)
                if mig is None:
                    return self._json(404, {"error": "迁移记录不存在"})
                return self._json(200, mig.to_dict())
            if path == "/evolution/candidates":
                limit = int(qs.get("limit") or 100)
                status = qs.get("status")
                cands = app.db.list_candidates(limit=limit, status=status)
                return self._json(200, {"candidates": [_candidate_summary(c)
                                                       for c in cands]})
            if path.startswith("/evolution/candidates/"):
                parts = [p for p in path.split("/") if p]
                cand = app.evolution.get_candidate(parts[2]) if len(parts) > 2 else None
                if cand is None:
                    return self._json(404, {"error": "候选不存在"})
                return self._json(200, cand.to_dict())
            if path == "/evolution/datasets":
                data = [{"dataset_id": d.dataset_id, "version": d.version,
                         "cases": len(d.cases), "source": d.source}
                        for d in app.datasets.values()]
                return self._json(200, {"datasets": data})
            if path == "/workflows":
                return self._json(200, {"workflows": app.db.list_workflow_defs()})
            # 抽取候选（Arch 16.2）：需排在 /workflows/{id} 之前
            if path == "/workflows/candidates":
                limit = min(500, int(qs.get("limit") or 50))
                return self._json(200, {"candidates": app.workflow_extractor.list(
                    status=qs.get("status") or None, limit=limit)})
            if path.startswith("/workflows/candidates/"):
                cid = path.split("/")[3]
                cand = app.workflow_extractor.get(cid)
                if cand is None:
                    return self._json(404, {"error": "工作流候选不存在"})
                return self._json(200, cand)
            if path.startswith("/workflows/"):
                wid = path.split("/")[2]
                data = app.db.get_workflow_def(wid)
                if data is None:
                    return self._json(404, {"error": "工作流不存在"})
                return self._json(200, {"workflow": data})
            if path == "/memory/stats":
                return self._json(200, app.memory.stats())
            if path == "/memory/evaluations":
                limit = min(100, int(qs.get("limit") or 20))
                return self._json(200, {
                    "evaluations": app.db.list_memory_eval_reports(
                        limit=limit)})
            if path == "/admin/backups":
                if auth.role != "admin":
                    return self._json(403, {"error": "该操作需要 admin 权限"})
                return self._json(200, {"backups": app.backups.list(),
                                        "directory": str(
                                            app.backups.backup_dir)})
            if path == "/releases":
                return self._json(200, {
                    "current": app.releases.current_release_id,
                    "manifests": [m.to_dict() for m in
                                  app.releases.manifests.values()],
                    "deployments": app.releases.deployments,
                    "rollbacks": app.releases.rollback_records})
            if path.startswith("/releases/"):
                rid = path.split("/")[2]
                m = app.releases.manifests.get(rid)
                if m is None:
                    return self._json(404, {"error": "发布不存在"})
                return self._json(200, {"manifest": m.to_dict(),
                                        "verify": app.releases.verify(rid)})
            if path == "/diagnostics":
                limit = int(qs.get("limit") or 50)
                status = qs.get("status")
                cases = app.db.list_diagnostic_cases(limit=limit,
                                                     status=status)
                return self._json(200, {"cases": [_case_summary(c)
                                                  for c in cases]})
            if path == "/diagnostics/trends":
                return self._json(200, app.diagnostics.trends(
                    limit=int(qs.get("limit") or 200)))
            if path == "/diagnostics/drift":
                return self._json(200, app.drift.detect())
            if path.startswith("/diagnostics/"):
                case_id = path.split("/")[2]
                case = app.db.get_diagnostic_case(case_id)
                if case is None:
                    return self._json(404, {"error": "诊断案例不存在"})
                return self._json(200, _case_detail(app, case))
            return self._json(404, {"error": "未知路径"})

        # ---------- POST ----------
        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            auth = self._authorize("POST", path)
            if auth is None:
                return
            body = self._body()
            if path == "/tasks":
                goal = body.get("goal", "").strip()
                if not goal:
                    return self._json(400, {"error": "缺少 goal"})
                risk = body.get("risk_level", "low")
                try:
                    rl = RiskLevel(risk)
                except ValueError:
                    return self._json(400, {"error": f"非法 risk_level: {risk}"})
                request_id = body.get("client_request_id")
                if request_id is not None and (
                        not isinstance(request_id, str)
                        or not CLIENT_REQUEST_RE.match(request_id)):
                    return self._json(
                        400, {"error": "非法 client_request_id"})
                with _submit_lock:
                    if request_id:
                        existing = app.db.get_request_task_id(request_id)
                        if existing:
                            return self._json(202, {
                                "task_id": existing, "status": "CREATED",
                                "deduplicated": True})
                    # 后台执行 + 立即返回（客户端轮询 /tasks/{id}）
                    t = app.controller.submit_task(
                        goal,
                        intent=body.get("intent"),
                        domain=body.get("domain"),
                        constraints=body.get("constraints") or [],
                        acceptance=body.get("acceptance_criteria") or [],
                        risk_level=rl,
                        budget_overrides=body.get("budget_policy"),
                        wait=False)
                    if request_id:
                        app.db.save_request(request_id, t.task_id)
                return self._json(202, {"task_id": t.task_id, "status": "CREATED"})
            if path == "/events/sync":
                return self._events_sync(body)
            if path == "/workflows/extract":
                return self._workflow_extract(body, auth.role)
            if path == "/memory/evaluate":
                report = app.run_memory_eval()
                app.audit.record(auth.role, "memory.evaluate",
                                 target=report["report_id"],
                                 detail={"recall": report.get("recall"),
                                         "precision": report.get("precision"),
                                         "case_count": report.get("case_count")})
                return self._json(200, report)
            if path == "/diagnostics/drift/scan":
                report = app.drift.detect(emit=True)
                app.audit.record(auth.role, "diagnostics.drift_scan",
                                 target="drift",
                                 outcome=report.get("status"),
                                 detail={"signal_count":
                                         report.get("signal_count"),
                                         "actions":
                                         report.get("recommended_actions")})
                return self._json(200, report)
            if path == "/models/migrations":
                return self._migration_create(body, auth.role)
            parts = path.lstrip("/").split("/")
            if (len(parts) == 4 and parts[0] == "models"
                    and parts[1] == "migrations"):
                return self._migration_action(parts[2], parts[3], body,
                                              auth.role)
            if (len(parts) == 4 and parts[0] == "workflows"
                    and parts[1] == "candidates"):
                return self._workflow_candidate_action(parts[2], parts[3],
                                                       body, auth.role)
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
                out = app.controller.cancel_task(parts[1])
                return self._json(200 if out.get("ok") else 409, out)
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "pause":
                out = app.controller.pause_task(parts[1])
                return self._json(200 if out.get("ok") else 409, out)
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "resume":
                try:
                    t = app.controller.resume_task(
                        parts[1], force=bool(body.get("force")), wait=False)
                except PermissionError as e:
                    return self._json(409, {"error": str(e),
                                            "requires_force": True})
                except ValueError as e:
                    return self._json(409, {"error": str(e)})
                if t is None:
                    return self._json(404, {"error": "任务不存在"})
                return self._json(202, {"task": t.to_dict(),
                                        "resuming": True})
            if path == "/evolution/candidates":
                return self._evolution_propose(body, auth.role)
            if (len(parts) == 4 and parts[0] == "evolution"
                    and parts[1] == "candidates"):
                return self._evolution_action(parts[2], parts[3], body,
                                              auth.role)
            if path == "/logs/purge":
                if auth.role != "admin":
                    return self._json(403, {"error": "该操作需要 admin 权限"})
                out = app.logs.purge()
                app.audit.record(auth.role, "logs.purge",
                                 target="logs", detail=out)
                return self._json(200, {"deleted": out})
            if path == "/logs/delete-task":
                if auth.role != "admin":
                    return self._json(403, {"error": "该操作需要 admin 权限"})
                task_id = (body.get("task_id") or "").strip()
                if not task_id:
                    return self._json(400, {"error": "缺少 task_id"})
                out = app.logs.delete_task(task_id)
                app.audit.record(auth.role, "logs.delete_task",
                                 target=task_id, detail=out)
                return self._json(200, {"task_id": task_id, "deleted": out})
            if path == "/releases/rollback":
                reason = (body.get("reason") or "manual rollback").strip()
                out = app.releases.rollback(reason)
                app.audit.record(auth.role, "release.rollback",
                                 target=app.releases.current_release_id or "",
                                 outcome="ok" if out.get("ok") else "failed",
                                 detail={"reason": reason})
                return self._json(200 if out.get("ok") else 409, out)
            if path == "/admin/backup":
                if auth.role != "admin":
                    return self._json(403, {"error": "该操作需要 admin 权限"})
                try:
                    out = app.backups.create(body.get("name") or None)
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                app.audit.record(auth.role, "admin.backup_create",
                                 target=out["name"],
                                 detail={"counts": out.get("counts"),
                                         "files": sorted(
                                             (out.get("files") or {}).keys())})
                return self._json(201, out)
            if path == "/admin/restore":
                if auth.role != "admin":
                    return self._json(403, {"error": "该操作需要 admin 权限"})
                ref = (body.get("name") or "").strip()
                if not ref:
                    return self._json(400, {"error": "缺少备份文件 name"})
                active = [t.task_id for t in app.db.list_tasks(limit=1000)
                          if not t.is_terminal()]
                if active:
                    return self._json(
                        409, {"error": "存在未结束任务，拒绝在线恢复",
                              "active_tasks": active[:20]})
                try:
                    out = app.backups.restore(ref)
                except FileNotFoundError as e:
                    return self._json(404, {"error": str(e)})
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                app.audit.record(auth.role, "admin.restore",
                                 target=out.get("name") or ref,
                                 detail={"restored": out.get("restored"),
                                         "safety_backup":
                                             out.get("safety_backup")})
                return self._json(200, out)
            if path == "/diagnostics/scan":
                return self._diagnostic_scan(body)
            if (len(parts) == 3 and parts[0] == "diagnostics"
                    and parts[2] == "experiments"):
                return self._diagnostic_experiment(parts[1], body)
            if (len(parts) == 3 and parts[0] == "diagnostics"
                    and parts[2] == "hypotheses"):
                return self._diagnostic_suggest(parts[1], body, auth.role)
            if (len(parts) == 3 and parts[0] == "diagnostics"
                    and parts[2] == "candidates"):
                return self._diagnostic_candidate(parts[1], body)
            return self._json(404, {"error": "未知路径"})

        # ---------- 成功任务 → 工作流候选（Arch 16.2）----------
        def _workflow_extract(self, body: Dict[str, Any], actor: str):
            raw_min = body.get("min_support")
            try:
                min_support = int(raw_min) if raw_min is not None else None
                out = app.workflow_extractor.extract(
                    min_support=min_support,
                    limit=int(body.get("limit") or 200), actor=actor)
            except (TypeError, ValueError) as e:
                return self._json(400, {"error": f"非法参数: {e}"})
            return self._json(200, out)

        def _workflow_candidate_action(self, candidate_id: str, action: str,
                                       body: Dict[str, Any], actor: str):
            if action not in ("approve", "reject", "publish"):
                return self._json(404, {"error": f"未知动作: {action}"})
            if actor != "admin":
                return self._json(403, {"error": "该操作需要 admin 权限"})
            extractor = app.workflow_extractor
            reason = (body.get("reason") or "").strip()
            try:
                if action == "approve":
                    out = extractor.approve(candidate_id, actor=actor,
                                            reason=reason)
                elif action == "reject":
                    out = extractor.reject(candidate_id, actor=actor,
                                           reason=reason)
                else:
                    args = body.get("args_by_step")
                    if args is not None and not isinstance(args, dict):
                        return self._json(400, {"error": "args_by_step 必须为对象"})
                    out = extractor.publish(candidate_id, actor=actor,
                                            reason=reason, args_by_step=args)
            except KeyError:
                return self._json(404, {"error": "工作流候选不存在"})
            except ValueError as e:
                return self._json(409, {"error": str(e)})
            return self._json(200, out)

        # ---------- 客户端事件同步 ----------
        def _events_sync(self, body: Dict[str, Any]):
            try:
                events, errors = validate_batch(body)
            except BatchError as exc:
                return self._json(400, {"error": str(exc)})
            accepted, duplicates = [], []
            for evt in events:
                if app.bus.ingest(evt):
                    accepted.append(evt.event_id)
                else:
                    duplicates.append(evt.event_id)
            payload = {"accepted": accepted, "duplicates": duplicates,
                       "rejected": errors}
            if not accepted and not duplicates:
                return self._json(400, payload)
            return self._json(200, payload)

        # ---------- 模型迁移（PRD 20 / 104 / 111）----------
        def _migration_create(self, body: Dict[str, Any], actor: str):
            old = (body.get("old_model_id") or "").strip()
            new = (body.get("new_model_id") or "").strip()
            if not old or not new:
                return self._json(400, {"error": "缺少 old_model_id 或 new_model_id"})
            try:
                mig = app.migrations.create(
                    old, new, dataset_id=body.get("dataset_id"))
            except KeyError as e:
                return self._json(404, {"error": str(e)})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            app.audit.record(actor, "model.migration_create",
                             target=mig.migration_id,
                             outcome=mig.status,
                             detail={"old": old, "new": new})
            return self._json(201 if mig.status != "blocked" else 409,
                              mig.to_dict())

        def _migration_action(self, migration_id: str, action: str,
                              body: Dict[str, Any], actor: str):
            gray = body.get("gray_percent", 100)
            try:
                if action == "regression":
                    out = app.migrations.run_regression(
                        migration_id,
                        dataset_id=body.get("dataset_id")).to_dict()
                elif action == "release":
                    out = app.migrations.prepare_release(migration_id).to_dict()
                elif action == "apply":
                    out = app.migrations.apply(
                        migration_id, actor=body.get("actor") or actor,
                        gray_percent=int(gray),
                        activate_default=body.get("activate_default"))
                elif action == "rollback":
                    out = app.migrations.rollback(
                        migration_id,
                        reason=(body.get("reason") or "").strip())
                else:
                    return self._json(404, {"error": f"未知动作: {action}"})
            except KeyError as e:
                return self._json(404, {"error": str(e)})
            except (TypeError, ValueError) as e:
                return self._json(400, {"error": str(e)})
            except RuntimeError as e:
                return self._json(409, {"error": str(e)})
            status = out.get("status")
            if isinstance(out.get("migration"), dict):
                status = out["migration"].get("status")
            app.audit.record(actor, f"model.migration_{action}",
                             target=migration_id, outcome=status or "ok",
                             detail={"action": action})
            return self._json(200, out)

        # ---------- 进化轨（PRD 46-54 / 80-82）----------
        def _evolution_propose(self, body: Dict[str, Any], actor: str = "operator"):
            required = ("baseline_version", "target_component",
                        "target_problem", "hypothesis")
            missing = [k for k in required if not (body.get(k) or "").strip()]
            if missing:
                return self._json(400, {"error": "缺少字段: " + ", ".join(missing)})
            cand = app.evolution.propose_candidate(
                baseline_version=body["baseline_version"],
                target_component=body["target_component"],
                target_problem=body["target_problem"],
                hypothesis=body["hypothesis"],
                change_scope=body.get("change_scope") or "local",
                evidence=body.get("evidence") or [],
                target_domain=body.get("target_domain") or "")
            if body.get("risk_level"):
                if body["risk_level"] not in PUBLISH_RULES:
                    return self._json(400, {"error": "risk_level 必须为 L0-L4"})
                cand.risk_level = body["risk_level"]
                app.db.save_candidate(cand)
            app.audit.record(actor, "evolution.propose",
                             target=cand.candidate_id,
                             detail={"component": cand.target_component,
                                     "risk_level": cand.risk_level})
            return self._json(201, {"candidate": cand.to_dict(),
                                    "publish_rule": PUBLISH_RULES.get(
                                        cand.risk_level, "strict_review")})

        def _evolution_action(self, candidate_id: str, action: str,
                              body: Dict[str, Any], actor: str = "operator"):
            cand = app.evolution.get_candidate(candidate_id)
            if cand is None:
                return self._json(404, {"error": "候选不存在"})
            try:
                if action == "evaluate":
                    out = app.evolution.evaluate_candidate(
                        candidate_id, dataset_id=body.get("dataset_id"),
                        baseline=bool(body.get("baseline", True)))
                    app.audit.record(actor, "evolution.evaluate",
                                     target=candidate_id,
                                     outcome="ok" if out.get("passed") else "failed",
                                     detail={"dataset_id": out.get("dataset_id")})
                    return self._json(200, out)
                if action == "approve":
                    out = app.evolution.approve(
                        cand, approver=body.get("approver") or actor)
                    app.audit.record(actor, "evolution.approve",
                                     target=candidate_id,
                                     detail={"approver": body.get("approver") or actor})
                    return self._json(200, out)
                if action == "deploy":
                    out = app.evolution.deploy_candidate(
                        cand, actor=body.get("actor") or actor,
                        gray_percent=int(body.get("gray_percent", 100)),
                        gate_passed=body.get("gate_passed"))
                    app.audit.record(actor, "evolution.deploy",
                                     target=candidate_id,
                                     outcome="ok" if out.get("ok") else "blocked",
                                     detail={"gray_percent": body.get("gray_percent", 100),
                                             "reason": out.get("reason", "")})
                    return self._json(200 if out.get("ok") else 409, out)
                if action == "monitor":
                    metrics = body.get("metrics")
                    if metrics is not None and not isinstance(metrics, dict):
                        return self._json(400, {"error": "metrics 必须为对象"})
                    # 未提供 metrics 时从真实运行任务自动采集（PRD 82）
                    out = app.evolution.monitor(
                        cand, metrics or None,
                        thresholds=body.get("thresholds"))
                    if out.get("decision") == "no_data_source":
                        return self._json(409, out)
                    app.audit.record(actor, "evolution.monitor",
                                     target=candidate_id,
                                     outcome=out.get("decision", "ok"),
                                     detail={"metrics": metrics,
                                             "reasons": out.get("reasons", [])})
                    return self._json(200, out)
                if action == "rollback":
                    reason = (body.get("reason") or "manual rollback").strip()
                    app.evolution.rollback(cand, reason)
                    rel = app.releases.rollback(reason)
                    app.audit.record(actor, "evolution.rollback",
                                     target=candidate_id,
                                     detail={"reason": reason})
                    return self._json(200, {"candidate": cand.to_dict(),
                                            "release": rel})
            except KeyError as e:
                return self._json(404, {"error": f"不存在: {e}"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except RuntimeError as e:
                return self._json(409, {"error": str(e)})
            except IllegalCandidateTransition as e:
                return self._json(409, {"error": str(e)})
            return self._json(404, {"error": f"未知动作: {action}"})

        # ---------- 诊断辅助 ----------
        def _diagnostic_scan(self, body: Dict[str, Any]):
            task_id = (body.get("task_id") or "").strip()
            if task_id:
                case = app.diagnostics.diagnose_task(task_id)
                if case is None:
                    return self._json(409, {"error": "任务不存在或非失败状态"})
                return self._json(200, _case_detail(app, case))
            created, skipped = [], 0
            for t in app.db.list_tasks(limit=200):
                if t.task_status != TaskStatus.FAILED:
                    continue
                case = app.diagnostics.diagnose_task(t.task_id)
                if case is None:
                    skipped += 1
                else:
                    created.append(_case_summary(case))
            return self._json(200, {"cases": created, "skipped": skipped})

        def _diagnostic_suggest(self, case_id: str, body: Dict[str, Any],
                                actor: str):
            try:
                out = app.diagnostics.suggest_hypotheses(
                    case_id, max_items=body.get("max_items"))
            except KeyError:
                return self._json(404, {"error": "诊断案例不存在"})
            except RuntimeError as e:
                return self._json(503, {"error": str(e)})
            app.audit.record(actor, "diagnostics.suggest_hypotheses",
                             target=case_id,
                             detail={"created": len(out.get("created") or []),
                                     "skipped": len(out.get("skipped") or []),
                                     "model_id": out.get("model_id")})
            return self._json(200, out)

        def _diagnostic_experiment(self, case_id: str, body: Dict[str, Any]):
            case = app.db.get_diagnostic_case(case_id)
            if case is None:
                return self._json(404, {"error": "诊断案例不存在"})
            method = (body.get("method") or "").strip()
            if not method:
                return self._json(400, {"error": "缺少 method"})
            hyps = app.db.list_hypotheses(case_id)
            hid = body.get("hypothesis_id")
            hypothesis = next((h for h in hyps if h.hypothesis_id == hid),
                              None) if hid else (hyps[0] if hyps else None)
            if hypothesis is None:
                return self._json(404, {"error": "假设不存在"})
            try:
                exp = app.diagnostics.run_experiment(
                    case, hypothesis, method, fault=body.get("fault"))
            except PermissionError as e:
                return self._json(429, {"error": str(e)})
            return self._json(200, {"experiment": exp.to_dict(),
                                    "hypothesis": hypothesis.to_dict()})

        def _diagnostic_candidate(self, case_id: str, body: Dict[str, Any]):
            case = app.db.get_diagnostic_case(case_id)
            if case is None:
                return self._json(404, {"error": "诊断案例不存在"})
            hid = body.get("hypothesis_id")
            if not hid:
                return self._json(400, {"error": "缺少 hypothesis_id"})
            try:
                cand = app.diagnostics.promote_to_candidate(
                    case, hid, change_scope=body.get("change_scope"),
                    risk_level=body.get("risk_level"))
            except KeyError:
                return self._json(404, {"error": "假设不存在"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(201, {"candidate_id": cand.candidate_id,
                                    "status": cand.status.value,
                                    "risk_level": cand.risk_level,
                                    "publish_rule": PUBLISH_RULES.get(
                                        cand.risk_level, "strict_review")})

    return Handler


def _model_summary(profile: Any) -> Dict[str, Any]:
    return {
        "model_id": profile.model_id, "provider": profile.provider,
        "api_version": profile.api_version,
        "context_limit": profile.context_limit,
        "supports_tool_call": profile.supports_tool_call,
        "supports_structured_output": profile.supports_structured_output,
        "supports_vision": profile.supports_vision,
        "rpm_limit": profile.rpm_limit,
        "price_per_1k_input": profile.price_per_1k_input,
        "price_per_1k_output": profile.price_per_1k_output,
        "quality_score": profile.quality_score,
        "avg_latency_s": profile.avg_latency_s,
        "available": profile.available,
        "compat_tested": profile.compat_tested,
        "last_verified_at": profile.last_verified_at,
        "known_limitations": profile.known_limitations,
    }


def _candidate_summary(cand: Any) -> Dict[str, Any]:
    return {"candidate_id": cand.candidate_id, "status": cand.status.value,
            "risk_level": cand.risk_level, "target_component": cand.target_component,
            "target_problem": cand.target_problem,
            "publish_suggestion": cand.publish_suggestion,
            "eval_dataset_id": cand.eval_dataset_id,
            "created_at": cand.created_at}


def _case_summary(case: Any) -> Dict[str, Any]:
    return {"case_id": case.case_id, "task_id": case.task_id,
            "status": case.status.value, "symptom": case.symptom,
            "candidate_id": case.candidate_id, "created_at": case.created_at}


def _case_detail(app: App, case: Any) -> Dict[str, Any]:
    return {"case": case.to_dict(),
            "hypotheses": [h.to_dict() for h in
                           app.db.list_hypotheses(case.case_id)],
            "experiments": [e.to_dict() for e in
                            app.db.list_experiments(case.case_id)]}


class ApiServer:
    def __init__(self, app: App, host: str = "127.0.0.1", port: int = 8300):
        self.httpd = ThreadingHTTPServer((host, port), make_handler(app))
        self.host, self.port = host, port

    def serve_forever(self) -> None:
        print(f"EvoAgent API: http://{self.host}:{self.port}  (GET /health)")
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.httpd.server_close()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def create_app(config_path: Optional[str] = None) -> App:
    return App(config_path=config_path)
