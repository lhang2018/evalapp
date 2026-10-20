"""REST API（供 Android 客户端与管理端使用）。

纯标准库实现，零外部依赖。所有响应为 JSON。
任务执行默认后台运行（POST /tasks 立即返回 task_id），
Android 端通过轮询任务状态与事件流展示进度（PRD 89.2 任务详情）。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from ..app import App
from ..core.task import RiskLevel, TaskStatus
from ..evolution.controller import PUBLISH_RULES


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def log_message(self, fmt, *args):  # 安静一些
            pass

        def _query(self) -> Dict[str, str]:
            parsed = urlparse(self.path)
            return {k: v[0] for k, v in parse_qs(parsed.query).items()}

        # ---------- GET ----------
        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            qs = self._query()
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
            if path == "/diagnostics":
                limit = int(qs.get("limit") or 50)
                status = qs.get("status")
                cases = app.db.list_diagnostic_cases(limit=limit,
                                                     status=status)
                return self._json(200, {"cases": [_case_summary(c)
                                                  for c in cases]})
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
                return self._json(202, {"task_id": t.task_id, "status": "CREATED"})
            parts = path.lstrip("/").split("/")
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
                out = app.controller.cancel_task(parts[1])
                return self._json(200 if out.get("ok") else 409, out)
            if path == "/diagnostics/scan":
                return self._diagnostic_scan(body)
            if (len(parts) == 3 and parts[0] == "diagnostics"
                    and parts[2] == "experiments"):
                return self._diagnostic_experiment(parts[1], body)
            if (len(parts) == 3 and parts[0] == "diagnostics"
                    and parts[2] == "candidates"):
                return self._diagnostic_candidate(parts[1], body)
            return self._json(404, {"error": "未知路径"})

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
                exp = app.diagnostics.run_experiment(case, hypothesis, method)
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

    def shutdown(self) -> None:
        self.httpd.shutdown()


def create_app(config_path: Optional[str] = None) -> App:
    return App(config_path=config_path)
