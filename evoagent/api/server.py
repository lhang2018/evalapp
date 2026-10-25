"""REST API（供 Android 客户端与管理端使用）。

纯标准库实现，零外部依赖。所有响应为 JSON。
任务执行默认后台运行（POST /tasks 立即返回 task_id），
Android 端通过轮询任务状态与事件流展示进度（PRD 89.2 任务详情）。
"""

import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ..app import App
from ..core.budget import CostReporter
from ..core.log_layers import role_can_read
from ..core.task import ORIGIN_VALUES, RiskLevel, TaskStatus
from ..evolution.candidates import IllegalCandidateTransition
from ..memory.store import Credibility, MemoryKind, Scope
from ..evolution.controller import PUBLISH_RULES
from .admin import ADMIN_HTML
from .attachments import (
    AttachmentError,
    MAX_CHUNK_BYTES,
    digest,
    sanitize_file_name,
    validate_chunk,
    validate_init,
)
from .auth import ApiAuth
from . import routes
from . import approvals_api  # F-04 审批记录端点（注册表路由）
from . import action_security_api  # ALN-01/02任务授权与副作用对账
from . import goals_api  # ALN-13 Goal治理入口
from . import assets_api  # ALN-06 资产端点；由生产入口注册，不能依赖测试导入
from . import impact_api  # GAP 子步④ 能力影响分析端点（注册表路由）
from . import routing_eval_api  # F-53 路由质量评测端点（注册表路由）
from . import memory_context_api  # ALN-05-I task/run context provenance
from . import task_control_api  # 用户补充信息与独立运行历史
from . import capabilities_api  # ALN-24-B 能力六档状态与渐进授权投影
from .routes import RouteContext
from .diagnostics_import import (
    MAX_PACKAGE_BYTES,
    PackageError,
    inspect_package,
)
from .events_sync import BatchError, SYNC_PROTOCOL_VERSION, validate_batch

CLIENT_REQUEST_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
INSTALLATION_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
# ALN-13: new Goal-linked tasks require registration and a durable grant.
GOAL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{4,64}$")
BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
APPLY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# 幂等提交的检查-创建-登记需要原子化（单进程 API 假设）
_submit_lock = threading.Lock()


def _optional_id(body: Dict[str, Any], name: str,
                 pattern: re.Pattern) -> Optional[str]:
    value = body.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError(f"非法 {name}")
    return value


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: Any) -> None:
            # F-01 统一错误对象（契约 §10）：4xx/5xx 的裸 {"error": ...}
            # 统一补齐七字段；成功负载原样。显式语义键调用方优先。
            from ..core.errors import normalize_error_payload
            if code >= 400 and isinstance(payload, dict) and \
                    "error_code" not in payload:
                payload = normalize_error_payload(code, payload)
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

        def _bytes(self, code: int, body: bytes, content_type: str,
                   headers: Optional[Dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _authorize(self, method: str, path: str):
            res = ApiAuth(app.config.api_token,
                          app.config.api_admin_token).check(
                              self.headers.get("Authorization"), method, path)
            if not res.ok:
                self._drain_rejected_body()
                self._json(res.status, {"error": res.error})
                return None
            parts = path.strip("/").split("/")
            target_task = None
            if len(parts) >= 2 and parts[0] == "tasks":
                target_task = parts[1]
            elif len(parts) >= 2 and parts[0] == "runs":
                target_task = app.db.get_run_task_id(parts[1])
            if target_task is not None:
                task = app.db.get_task(target_task)
                if task is not None:
                    try:
                        app.memory_bindings.assert_principal(task, res.principal_id, admin=res.role == "admin")
                    except PermissionError as exc:
                        self._drain_rejected_body()
                        self._json(403, {"error": str(exc)})
                        return None
            # Global memory management is an administrator plane, not a tenant read API.
            if parts[0] == "memory" and res.role != "admin":
                self._drain_rejected_body()
                self._json(403, {"error": "全局记忆管理需要admin权限"})
                return None
            return res

        def _drain_rejected_body(self):
            # Closing a Windows socket with unread POST bytes can reset the
            # connection before the client receives its 401/403 response.
            # Discard a bounded body without parsing or executing it.
            self.close_connection = True
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if not 0 < length <= 65536:
                return
            deadline = time.monotonic() + 0.25
            original_timeout = self.connection.gettimeout()
            try:
                while length:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.connection.settimeout(remaining)
                    chunk = self.rfile.read1(length)
                    if not chunk:
                        break
                    length -= len(chunk)
            except OSError:
                pass
            finally:
                self.connection.settimeout(original_timeout)

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

        def _ctx(self, method: str, path: str, auth) -> RouteContext:
            """构造注册表路由上下文（协作基座 F-00.2）。

            body 传**惰性 getter** 而非字典：既有端点
            （如 POST /diagnostics/import）直接从 self.rfile 读原始字节，
            若此处急切解析会消费掉流，导致处理器读到空数据。
            """
            def respond(code, payload):
                # Domain guards can reject an authenticated request before
                # accessing ctx.body, just like the transport auth guard.
                if code in (401, 403) and not ctx.body_read:
                    self._drain_rejected_body()
                return self._json(code, payload)

            ctx = RouteContext(
                app=app, method=method, path=path,
                query=self._query(), body_getter=self._body, auth=auth,
                json=respond, text=self._text, role_can_read=role_can_read)
            return ctx

        def _dispatch(self, method: str, path: str, auth) -> bool:
            """先走注册表；未命中返回 False 让调用方继续既有链路。"""
            fn = {"GET": routes.dispatch_get,
                  "POST": routes.dispatch_post,
                  "PUT": routes.dispatch_put}.get(method)
            if fn is None:
                return False
            return fn(self._ctx(method, path, auth))

        # ---------- GET ----------
        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            qs = self._query()
            auth = self._authorize("GET", path)
            if auth is None:
                return
            if self._dispatch("GET", path, auth):
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
                traces = app.db.list_traces(task_id=qs.get("task_id") or None, limit=limit)
                if auth.role != "admin":
                    traces = [row for row in traces if app.memory_bindings.owns(row["task_id"], auth.principal_id)]
                return self._json(200, {"traces": traces})
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
                if auth.role != "admin":
                    tasks = [t for t in tasks if app.memory_bindings.owns(t.task_id, auth.principal_id)]
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
                if len(parts) >= 3 and parts[2] == "manifests":
                    return self._json(200, {"manifests": app.db.list_runtime_manifests(
                        tid, limit=min(100, int(qs.get("limit") or 20)))})
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
            if path.startswith("/artifacts/"):
                # 制品按不可变 release_id 提供分块下载；端侧必须自行验签，
                # 服务端不把本地文件路径或签名秘密下发给客户端。
                release_id = path.split("/", 2)[2]
                if (not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", release_id)
                        or release_id not in app.releases.manifests
                        or app.releases.artifact_dir is None):
                    return self._json(404, {"error": "制品不存在"})
                artifact = (Path(app.releases.artifact_dir)
                            / f"{release_id}.bundle")
                if not artifact.is_file():
                    return self._json(404, {"error": "制品不存在"})
                data = artifact.read_bytes()
                total = len(data)
                start, end = 0, total - 1
                match = re.fullmatch(
                    r"bytes=(\d*)-(\d*)",
                    (self.headers.get("Range") or "").strip())
                if match and (match.group(1) or match.group(2)):
                    if match.group(1):
                        start = int(match.group(1))
                        end = (int(match.group(2))
                               if match.group(2) else total - 1)
                    else:
                        start = max(0, total - int(match.group(2)))
                        end = total - 1
                    if start >= total or start > end:
                        return self._bytes(
                            416, b"", "application/octet-stream",
                            {"Content-Range": f"bytes */{total}"})
                    end = min(end, total - 1)
                    return self._bytes(
                        206, data[start:end + 1], "application/octet-stream",
                        {"Accept-Ranges": "bytes",
                         "Content-Range": f"bytes {start}-{end}/{total}"})
                return self._bytes(
                    200, data, "application/octet-stream",
                    {"Accept-Ranges": "bytes"})
            if path.startswith("/runs/"):
                parts = [p for p in path.split("/") if p]
                if len(parts) == 3 and parts[2] == "manifest":
                    manifest = app.db.get_runtime_manifest(
                        f"manifest_{parts[1]}")
                    if manifest is None:
                        return self._json(404, {"error": "运行清单不存在"})
                    try:
                        app.memory_bindings.assert_principal(app.db.get_task(manifest["task_id"]),
                            auth.principal_id, admin=auth.role == "admin")
                    except PermissionError as exc:
                        return self._json(403, {"error": str(exc)})
                    return self._json(200, manifest)
                return self._json(404, {"error": "未知路径"})
            if path == "/sync/candidates":
                iid = (qs.get("installation_id") or "").strip()
                if not INSTALLATION_RE.match(iid):
                    return self._json(400, {"error": "非法 installation_id"})
                try:
                    limit = int(qs.get("limit") or 20)
                except ValueError:
                    return self._json(400, {"error": "非法 limit"})
                try:
                    out = app.delivery.candidate_states(
                        installation_id=iid,
                        candidates=app.evolution.all_candidates(),
                        limit=limit)
                except KeyError:
                    return self._json(404, {"error": "安装实例不存在"})
                return self._json(200, out)
            if path == "/sync/installations":
                if auth.role != "admin":
                    return self._json(403, {"error": "该操作需要 admin 权限"})
                limit = min(500, int(qs.get("limit") or 100))
                return self._json(200, {"installations": app.db.list_installations(
                    limit=limit)})
            if path.startswith("/sync/installations/"):
                if auth.role != "admin":
                    return self._json(403, {"error": "该操作需要 admin 权限"})
                iid = path.split("/")[3]
                inst = app.db.get_installation(iid)
                if inst is None:
                    return self._json(404, {"error": "安装实例不存在"})
                limit = min(200, int(qs.get("deployments") or 50))
                return self._json(200, {"installation": inst,
                                        "deployments":
                                            app.db.list_device_deployments(
                                                iid, limit=limit)})
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
                limit = max(1, min(100, int(qs.get("runs") or 20)))
                return self._json(200, _workflow_center_detail(app, wid, data,
                                                               limit))
            if path == "/memory":
                try:
                    kind = MemoryKind(qs["kind"]) if qs.get("kind") else None
                    scope = Scope(qs["scope"]) if qs.get("scope") else None
                    status = (Credibility(qs["status"])
                              if qs.get("status") else None)
                    limit = max(1, min(500, int(qs.get("limit") or 100)))
                except ValueError:
                    return self._json(400, {"error": "非法的记忆过滤参数"})
                items = app.memory.list_items(kind=kind, scope=scope,
                                              status=status, limit=limit)
                return self._json(200, {
                    "items": [m.to_public_dict() for m in items],
                    "stats": app.memory.stats()})
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
            if path == "/diagnostics/imports":
                if auth.role != "admin":
                    return self._json(403, {"error": "该操作需要 admin 权限"})
                try:
                    limit = max(1, min(int(qs.get("limit") or 50), 200))
                except ValueError:
                    limit = 50
                return self._json(200, {"imports": [
                    _import_record(item)
                    for item in app.db.list_diagnostics_imports(limit)]})
            if path.startswith("/diagnostics/imports/"):
                if auth.role != "admin":
                    return self._json(403, {"error": "该操作需要 admin 权限"})
                import_id = path.split("/")[3]
                record = app.db.get_diagnostics_import(import_id)
                if record is None:
                    return self._json(404, {"error": "导入记录不存在"})
                return self._json(200, {
                    "import": _import_record(record),
                    "events": app.db.list_diagnostics_import_events(import_id)})
            if path.startswith("/diagnostics/"):
                case_id = path.split("/")[2]
                case = app.db.get_diagnostic_case(case_id)
                if case is None:
                    return self._json(404, {"error": "诊断案例不存在"})
                return self._json(200, _case_detail(app, case))
            if path.startswith("/attachments/"):
                parts = [p for p in path.split("/") if p]
                if len(parts) == 2:
                    return self._attachment_status(parts[1])
                if len(parts) == 3 and parts[2] == "content":
                    return self._attachment_content(parts[1], auth.role)
                return self._json(404, {"error": "未知路径"})
            return self._json(404, {"error": "未知路径"})

        # ---------- POST ----------
        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            auth = self._authorize("POST", path)
            if auth is None:
                return
            if self._dispatch("POST", path, auth):
                return
            if path == "/diagnostics/import":
                return self._diagnostics_import(auth.role)
            body = self._body()
            if path == "/attachments":
                return self._attachment_init(body, auth.role)
            parts = [p for p in path.split("/") if p]
            if (len(parts) == 3 and parts[0] == "attachments"
                    and parts[2] == "complete"):
                return self._attachment_complete(parts[1], auth.role)
            if path == "/tasks":
                if any(key in body for key in ("identity", "grant", "memory_identity", "memory_binding_ref",
                        "principal_id", "user_id", "project_id", "session_id", "task_id", "expert_id", "memory_grant")):
                    return self._json(403, {"error": "身份由服务端绑定，只能使用memory_context及已签发grant_ref"})
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
                try:
                    device_id = _optional_id(body, "device_id", DEVICE_ID_RE)
                    installation_id = _optional_id(
                        body, "installation_id", INSTALLATION_RE)
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                # 契约 §2：goal_id/origin（G-01 目标注册表前置）
                goal_id = body.get("goal_id")
                if goal_id is not None:
                    if not isinstance(goal_id, str) or not GOAL_ID_RE.match(goal_id):
                        return self._json(400, {"error": "非法 goal_id"})
                origin = body.get("origin")
                if origin is None:
                    origin = "user"
                if (not isinstance(origin, str) or origin not in ORIGIN_VALUES):
                    return self._json(400, {
                        "error": "非法 origin，须为 user|scheduled|derived|opportunity"})
                app_version = body.get("app_version")
                if app_version is not None and (
                        not isinstance(app_version, str)
                        or len(app_version) > 32):
                    return self._json(400, {"error": "非法 app_version"})
                parsed, error = _parse_task_model_fields(body)
                if error:
                    return self._json(400, {"error": error})
                with _submit_lock:
                    if request_id:
                        existing = app.db.get_request_task_id(request_id)
                        if existing:
                            try:
                                app.memory_bindings.check_replay(app.db.get_task(existing), auth.principal_id,
                                    body.get("memory_context"), body.get("memory_grant_ref"))
                            except PermissionError as exc:
                                return self._json(403, {"error": str(exc)})
                            return self._json(202, {
                                "task_id": existing, "status": "CREATED",
                                "deduplicated": True})
                    # 后台执行 + 立即返回（客户端轮询 /tasks/{id}）
                    try:
                        t = app.controller.submit_task(
                            goal,
                            intent=body.get("intent"),
                            domain=body.get("domain"),
                            constraints=body.get("constraints") or [],
                            acceptance=body.get("acceptance_criteria") or [],
                            risk_level=rl,
                            budget_overrides=body.get("budget_policy"),
                            authorization_scope=body.get("authorization_scope"),
                            wait=False,
                            device_id=device_id,
                            installation_id=installation_id,
                            app_version=app_version,
                            goal_id=goal_id,
                            origin=origin,
                            principal_id=auth.principal_id,
                            memory_context=body.get("memory_context"),
                            memory_grant_ref=body.get("memory_grant_ref"),
                            **parsed)
                    except PermissionError as exc:
                        return self._json(403, {"error": str(exc)})
                    except KeyError as exc:
                        return self._json(404, {"error": str(exc)})
                    except (ValueError, TypeError) as exc:
                        return self._json(400, {"error": str(exc)})
                    if request_id:
                        app.db.save_request(request_id, t.task_id)
                return self._json(202, {"task_id": t.task_id, "status": "CREATED"})
            if path == "/events/sync":
                return self._events_sync(body)
            if path == "/sync/reconcile":
                return self._sync_reconcile(body)
            if path == "/sync/apply-result":
                return self._sync_apply_result(body, auth.role)
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
                    # 副作用结果未知 → 契约 §10 强制先对账/人工确认
                    return self._json(409, {"error": str(e),
                                            "requires_force": True,
                                            "safe_to_repeat": "unknown",
                                            "required_action": "reconcile"})
                except ValueError as e:
                    # 本地守卫冲突（终态/状态机拒绝），无副作用
                    return self._json(409, {"error": str(e),
                                            "safe_to_repeat": "yes",
                                            "required_action": "none"})
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
            if path == "/evolution/sandbox/run":
                return self._sandbox_run(body, auth.role)
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
            if (len(parts) == 3 and parts[0] == "memory"
                    and parts[2] == "correct"):
                return self._memory_correct(parts[1], body, auth.role)
            return self._json(404, {"error": "未知路径"})

        def do_PUT(self):
            path = urlparse(self.path).path.rstrip("/")
            auth = self._authorize("PUT", path)
            if auth is None:
                return
            if self._dispatch("PUT", path, auth):
                return
            parts = [p for p in path.split("/") if p]
            if (len(parts) == 4 and parts[0] == "attachments"
                    and parts[2] == "chunks"):
                try:
                    index = int(parts[3])
                except ValueError:
                    return self._json(400, {"error": "分块序号必须是整数"})
                return self._attachment_chunk(parts[1], index, auth.role)
            return self._json(404, {"error": "未知路径"})

        def do_DELETE(self):
            path = urlparse(self.path).path.rstrip("/")
            auth = self._authorize("DELETE", path)
            if auth is None:
                return
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] == "memory":
                item = app.memory.get(parts[1])
                if item is None:
                    return self._json(404, {"error": "记忆不存在"})
                if not item.allowed:
                    return self._json(403, {"error": "无权删除该记忆"})
                app.memory.delete(parts[1])
                app.audit.record(auth.role, "memory.delete",
                                 target=parts[1])
                return self._json(200, {"ok": True,
                                        "memory_id": parts[1]})
            return self._json(404, {"error": "未知路径"})

        def _memory_correct(self, memory_id: str, body: Dict[str, Any],
                            actor: str):
            item = app.memory.get(memory_id)
            if item is None:
                return self._json(404, {"error": "记忆不存在"})
            if not item.allowed:
                return self._json(403, {"error": "无权修改该记忆"})
            content = body.get("content")
            if not isinstance(content, str) or not content.strip():
                return self._json(400, {"error": "content 必须为非空字符串"})
            content = content.strip()
            if len(content) > 4000:
                return self._json(400, {"error": "content 超过 4000 字符"})
            reason = str(body.get("reason") or "")[:500]
            updated = app.memory.correct(memory_id, content, reason=reason)
            app.audit.record(actor, "memory.correct", target=memory_id,
                             detail={"reason": reason})
            return self._json(200, {"item": updated.to_public_dict()})

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

        # ---------- 离线诊断包受控导入 ----------
        def _diagnostics_import(self, actor: str):
            if actor != "admin":
                return self._json(403, {"error": "该操作需要 admin 权限"})
            content_type = (self.headers.get("Content-Type") or "")
            content_type = content_type.split(";")[0].strip().lower()
            if content_type not in ("application/zip",
                                    "application/octet-stream"):
                return self._json(
                    415, {"error": "Content-Type 必须为 application/zip"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"error": "非法 Content-Length"})
            if length <= 0:
                return self._json(400, {"error": "导入包为空"})
            if length > MAX_PACKAGE_BYTES:
                return self._json(413, {"error": "导入包超过 20 MiB"})
            data = self.rfile.read(length)
            raw_name = (self.headers.get("X-File-Name") or "").strip()
            file_name = raw_name.replace("\\", "/").split("/")[-1][:128] or \
                "diagnostics.zip"
            try:
                parsed = inspect_package(data, app.db)
            except PackageError as exc:
                app.audit.record(actor, "diagnostics.offline_import",
                                 target=file_name, outcome="rejected",
                                 detail={"error": str(exc),
                                         "size_bytes": len(data)})
                return self._json(exc.status, {"error": str(exc)})

            existing = app.db.get_diagnostics_import_by_sha(parsed["sha256"])
            if existing:
                return self._json(200, {"import": _import_record(existing),
                                        "duplicate": True})
            import_id = "imp_" + uuid.uuid4().hex[:12]
            created = app.db.save_diagnostics_import(
                import_id, file_name, parsed["size_bytes"], parsed["sha256"],
                parsed["format_version"], parsed["exported_at"],
                len(parsed["files"]), len(parsed["events"]),
                parsed["warnings"], actor)
            if not created:
                existing = app.db.get_diagnostics_import_by_sha(
                    parsed["sha256"])
                return self._json(200, {
                    "import": _import_record(existing), "duplicate": True})
            for event in parsed["events"]:
                app.db.save_diagnostics_import_event(import_id, event)
            needs_review = sum(
                1 for event in parsed["events"]
                if event["review_status"] == "needs_review")
            app.audit.record(actor, "diagnostics.offline_import",
                             target=import_id,
                             detail={"file_name": file_name,
                                     "sha256": parsed["sha256"],
                                     "events": len(parsed["events"]),
                                     "needs_review": needs_review,
                                     "warnings": len(parsed["warnings"])})
            record = app.db.get_diagnostics_import(import_id)
            return self._json(201, {"import": _import_record(record),
                                    "duplicate": False,
                                    "needs_review": needs_review})

        # ---------- 诊断附件（补充 C §6）----------
        def _attachment_status(self, attachment_id: str):
            meta = app.db.get_attachment(attachment_id)
            if meta is None:
                return self._json(404, {"error": "附件不存在"})
            chunks = app.db.list_attachment_chunks(attachment_id)
            return self._json(200, _attachment_record(meta, chunks))

        def _attachment_init(self, body: Dict[str, Any], actor: str):
            try:
                parsed = validate_init(body)
            except AttachmentError as exc:
                app.audit.record(actor, "attachment.init",
                                 target=str(body.get("attachment_id"))[:64],
                                 outcome="rejected",
                                 detail={"error": str(exc)})
                return self._json(exc.status, {"error": str(exc)})
            existing = app.db.get_attachment(parsed.attachment_id)
            if existing:
                if (existing["sha256"] != parsed.sha256 or
                        int(existing["size_bytes"]) != parsed.size_bytes or
                        int(existing["chunk_size"]) != parsed.chunk_size):
                    return self._json(
                        409, {"error": "attachment_id 已被不同内容占用"})
                chunks = app.db.list_attachment_chunks(parsed.attachment_id)
                return self._json(
                    200, _attachment_record(existing, chunks))
            app.db.create_attachment(
                parsed.attachment_id, parsed.file_name, parsed.content_type,
                parsed.size_bytes, parsed.sha256, parsed.chunk_size,
                parsed.chunk_count, parsed.consent, actor)
            app.audit.record(actor, "attachment.init",
                             target=parsed.attachment_id,
                             detail={"file_name": parsed.file_name,
                                     "size_bytes": parsed.size_bytes,
                                     "sha256": parsed.sha256,
                                     "chunk_count": parsed.chunk_count})
            meta = app.db.get_attachment(parsed.attachment_id)
            return self._json(201, _attachment_record(meta, []))

        def _attachment_chunk(self, attachment_id: str, index: int,
                              actor: str):
            meta = app.db.get_attachment(attachment_id)
            if meta is None:
                return self._json(404, {"error": "附件不存在"})
            if meta["status"] == "complete":
                return self._json(409, {"error": "附件已上传完成"})
            if not 0 <= index < int(meta["chunk_count"]):
                return self._json(
                    400, {"error": f"分块序号越界: {index}"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"error": "非法 Content-Length"})
            if length <= 0:
                return self._json(400, {"error": "分块为空"})
            if length > MAX_CHUNK_BYTES:
                return self._json(413, {"error": "分块超过 4 MiB"})
            data = self.rfile.read(length)
            try:
                validate_chunk(meta, index, data)
            except AttachmentError as exc:
                return self._json(exc.status, {"error": str(exc)})
            progress = app.db.save_attachment_chunk(
                attachment_id, index, data, digest(data))
            meta = app.db.get_attachment(attachment_id)
            return self._json(200, {
                "attachment": _attachment_record(meta, []),
                "received_bytes": progress["received_bytes"],
                "received_chunks": progress["received_chunks"],
                "chunk_sha256": digest(data),
            })

        def _attachment_complete(self, attachment_id: str, actor: str):
            meta = app.db.get_attachment(attachment_id)
            if meta is None:
                return self._json(404, {"error": "附件不存在"})
            chunks = app.db.list_attachment_chunks(attachment_id)
            if meta["status"] == "complete":
                return self._json(200, {
                    "attachment": _attachment_record(meta, chunks),
                    "verified": True, "duplicate": True})
            indexes = [int(c["chunk_index"]) for c in chunks]
            expected = list(range(int(meta["chunk_count"])))
            if indexes != expected:
                return self._json(409, {
                    "error": "分块不完整",
                    "received_chunks": indexes})
            data = app.db.assemble_attachment(attachment_id)
            actual = digest(data)
            if len(data) != int(meta["size_bytes"]) or \
                    actual != meta["sha256"]:
                # 无法定位损坏分块，整批丢弃；客户端应重传全部得分块
                app.db.delete_attachment_chunks(attachment_id)
                app.db.mark_attachment(attachment_id, "uploading",
                                       "摘要不一致")
                app.audit.record(actor, "attachment.complete",
                                 target=attachment_id, outcome="rejected",
                                 detail={"expected": meta["sha256"],
                                         "actual": actual,
                                         "size_bytes": len(data)})
                return self._json(422, {
                    "error": "附件摘要不一致",
                    "expected_sha256": meta["sha256"],
                    "actual_sha256": actual,
                    "size_bytes": len(data)})
            app.db.mark_attachment(attachment_id, "complete", None,
                                   completed_at=time.time())
            meta = app.db.get_attachment(attachment_id)
            app.audit.record(actor, "attachment.complete",
                             target=attachment_id,
                             detail={"size_bytes": len(data),
                                     "sha256": actual})
            return self._json(200, {
                "attachment": _attachment_record(meta, chunks),
                "verified": True, "duplicate": False})

        def _attachment_content(self, attachment_id: str, actor: str):
            if actor != "admin":
                return self._json(403, {"error": "该操作需要 admin 权限"})
            meta = app.db.get_attachment(attachment_id)
            if meta is None:
                return self._json(404, {"error": "附件不存在"})
            if meta["status"] != "complete":
                return self._json(409, {"error": "附件尚未上传完成"})
            data = app.db.assemble_attachment(attachment_id)
            app.audit.record(actor, "attachment.content_read",
                             target=attachment_id,
                             detail={"size_bytes": len(data),
                                     "sha256": meta["sha256"]})
            ascii_name = re.sub(
                r"[^A-Za-z0-9._-]", "_",
                sanitize_file_name(meta["file_name"])) or "attachment.bin"
            return self._bytes(
                200, data, meta["content_type"] or "application/octet-stream",
                {"Content-Disposition": f'attachment; filename="{ascii_name}"',
                 "X-Attachment-SHA256": meta["sha256"]})

        # ---------- 客户端事件同步 ----------
        def _events_sync(self, body: Dict[str, Any]):
            try:
                batch_id = _optional_id(body, "batch_id", BATCH_ID_RE)
                installation_id = _optional_id(
                    body, "installation_id", INSTALLATION_RE)
                client_id = body.get("client_id") or "unknown"
                if not isinstance(client_id, str) or len(client_id) > 128:
                    raise ValueError("非法 client_id")
                events, errors = validate_batch(body)
            except BatchError as exc:
                return self._json(400, {"error": str(exc)})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})

            # 同一 batch_id 重传返回原回执，而不是把已入库事件重新计数
            if batch_id:
                existing = app.db.get_sync_batch(batch_id)
                if existing is not None:
                    return self._json(*_receipt_response(existing, True))

            accepted, duplicates = [], []
            for evt in events:
                if app.bus.ingest(evt):
                    accepted.append(evt.event_id)
                else:
                    duplicates.append(evt.event_id)
            confirmed = accepted + duplicates
            payload = {
                "accepted": accepted, "duplicates": duplicates,
                "rejected": errors,
                # 部分成功时明确列出已确认与未确认事件
                "confirmed": confirmed,
                "unconfirmed": [item["event_id"] for item in errors],
                "protocol_version": SYNC_PROTOCOL_VERSION,
            }
            receipt_id = f"rcpt_{uuid.uuid4().hex[:16]}"
            received_at = time.time()
            payload["receipt"] = {
                "receipt_id": receipt_id, "batch_id": batch_id,
                "replayed": False, "received_at": received_at,
                "protocol_version": SYNC_PROTOCOL_VERSION,
                "confirmed_count": len(confirmed),
            }
            if batch_id:
                stored = {k: v for k, v in payload.items()
                          if k != "receipt"}
                inserted = app.db.save_sync_batch(
                    batch_id, receipt_id, client_id, installation_id,
                    len(events), len(accepted), len(duplicates), len(errors),
                    stored)
                if not inserted:
                    existing = app.db.get_sync_batch(batch_id)
                    if existing is not None:
                        return self._json(*_receipt_response(existing, True))
            status = 200 if (accepted or duplicates) else 400
            return self._json(status, payload)

        # ---------- 版本对账 / 应用结果（双端协同 §5）----------
        def _sync_reconcile(self, body: Dict[str, Any]):
            try:
                installation_id = _optional_id(
                    body, "installation_id", INSTALLATION_RE)
                if not installation_id:
                    raise ValueError("缺少 installation_id")
                device_id = _optional_id(
                    body, "device_id", DEVICE_ID_RE) or ""
                platform = body.get("platform") or "android"
                if not isinstance(platform, str):
                    raise ValueError("platform 必须是字符串")
                app_version = body.get("app_version")
                if not isinstance(app_version, str) or not app_version.strip():
                    raise ValueError("缺少 app_version")
                current = body.get("current_release_id")
                if current is not None and (
                        not isinstance(current, str) or len(current) > 128):
                    raise ValueError("非法 current_release_id")
                tools = body.get("tool_contracts") or []
                if not isinstance(tools, list) or len(tools) > 64 or any(
                        not isinstance(t, str) or len(t) > 120 for t in tools):
                    raise ValueError("非法 tool_contracts")
                out = app.delivery.reconcile(
                    installation_id=installation_id, device_id=device_id,
                    platform=platform, app_version=app_version,
                    current_release_id=current or None, tool_contracts=tools)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, out)

        def _sync_apply_result(self, body: Dict[str, Any], actor: str):
            try:
                installation_id = _optional_id(
                    body, "installation_id", INSTALLATION_RE)
                if not installation_id:
                    raise ValueError("缺少 installation_id")
                action = body.get("action")
                if not isinstance(action, str) or not action:
                    raise ValueError("缺少 action")
                release_id = body.get("release_id")
                to_release_id = body.get("to_release_id")
                for name, value in (("release_id", release_id),
                                    ("to_release_id", to_release_id)):
                    if value is not None and (
                            not isinstance(value, str) or len(value) > 128):
                        raise ValueError(f"非法 {name}")
                outcome = body.get("outcome") or "ok"
                reason = body.get("reason") or ""
                if not isinstance(outcome, str) or not isinstance(reason, str):
                    raise ValueError("outcome/reason 必须是字符串")
                apply_id = _optional_id(body, "apply_id", APPLY_ID_RE)
                out = app.delivery.record_apply(
                    installation_id=installation_id, action=action,
                    release_id=release_id, outcome=outcome,
                    reason=reason[:500], to_release_id=to_release_id,
                    apply_id=apply_id)
            except KeyError as exc:
                return self._json(404, {"error": str(exc)})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            if not out.get("ok"):
                return self._json(409, out)
            if not out.get("duplicate"):
                app.audit.record(
                    actor, "sync.apply_result", target=installation_id,
                    outcome=action,
                    detail={"release_id": release_id,
                            "current_release_id":
                                (out.get("installation") or {}).get(
                                    "current_release_id"),
                            "reason": reason[:200]})
            return self._json(200, out)

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
                                     outcome="ok" if out.get("ok") else "blocked",
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

        # ---------- 候选隔离执行（Arch 18.3）----------
        def _sandbox_run(self, body: Dict[str, Any], actor: str):
            code = body.get("code")
            path = body.get("path")
            if not code and not path:
                return self._json(400, {"error": "缺少 code 或 path"})
            if code is not None and not isinstance(code, str):
                return self._json(400, {"error": "code 必须是字符串"})
            timeout = body.get("timeout", 5)
            try:
                if path:
                    result = app.sandbox.run_file(str(path), timeout=timeout)
                else:
                    result = app.sandbox.run_code(code, timeout=timeout)
            except FileNotFoundError as e:
                return self._json(404, {"error": str(e)})
            except (OSError, UnicodeDecodeError) as e:
                return self._json(400, {"error": str(e)})
            app.audit.record(actor, "evolution.sandbox_run",
                             target=str(path or "inline"),
                             detail={"status": result.status,
                                     "exit_code": result.exit_code,
                                     "violations": result.violations[:5]})
            return self._json(200, result.to_dict())

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


def _attachment_record(meta: Dict[str, Any],
                       chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "attachment_id": meta["attachment_id"],
        "file_name": meta["file_name"],
        "content_type": meta["content_type"],
        "size_bytes": int(meta["size_bytes"]),
        "sha256": meta["sha256"],
        "chunk_size": int(meta["chunk_size"]),
        "chunk_count": int(meta["chunk_count"]),
        "consent": bool(meta["consent"]),
        "status": meta["status"],
        "received_bytes": int(meta["received_bytes"] or 0),
        "received_chunks": [int(c["chunk_index"]) for c in chunks],
        "created_at": meta["created_at"],
        "updated_at": meta["updated_at"],
        "completed_at": meta["completed_at"],
        "last_error": meta["last_error"],
    }


def _import_record(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if record is None:
        return None
    out = dict(record)
    try:
        out["warnings"] = json.loads(out.get("warnings") or "[]")
    except (TypeError, json.JSONDecodeError):
        out["warnings"] = []
    return out


def _receipt_response(stored: Dict[str, Any], replayed: bool):
    """重放已登记批次时返回原始回执内容（含原始 400/200 语义）。"""
    payload = dict(stored.get("data") or {})
    payload["receipt"] = {"receipt_id": stored.get("receipt_id"),
                          "batch_id": stored.get("batch_id"),
                          "replayed": replayed,
                          "received_at": stored.get("received_at"),
                          "protocol_version":
                              payload.get("protocol_version") or
                              SYNC_PROTOCOL_VERSION,
                          "confirmed_count":
                              len(payload.get("confirmed") or [])}
    status = 200 if (payload.get("accepted") or payload.get("duplicates")) \
        else 400
    return status, payload


_TASK_COMPLEXITIES = {"", "simple", "moderate", "complex"}


def _string_field(body: Dict[str, Any], name: str, max_len: int) -> str:
    value = body.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串")
    value = value.strip()
    if len(value) > max_len:
        raise ValueError(f"{name} 超过 {max_len} 字符")
    return value


def _string_list_field(body: Dict[str, Any], name: str,
                       max_items: int, max_len: int) -> List[str]:
    value = body.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} 必须是数组")
    if len(value) > max_items:
        raise ValueError(f"{name} 最多 {max_items} 项")
    out: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{name} 的每一项必须是字符串")
        item = item.strip()
        if not item:
            continue
        if len(item) > max_len:
            raise ValueError(f"{name} 单项超过 {max_len} 字符")
        out.append(item)
    return out


def _parse_task_model_fields(body: Dict[str, Any]):
    """校验 PRD 7.3 任务解析结果的可选输入；返回 (kwargs, error)。"""
    try:
        complexity = _string_field(body, "complexity", 20)
        if complexity not in _TASK_COMPLEXITIES:
            raise ValueError("complexity 必须是 simple/moderate/complex")
        parsed: Dict[str, Any] = {
            "target_object": _string_field(body, "target_object", 1000),
            "expected_output": _string_field(body, "expected_output", 2000),
            "complexity": complexity,
            "required_tools": _string_list_field(body, "required_tools", 32, 120),
            "required_permissions": _string_list_field(
                body, "required_permissions", 32, 120),
            "open_questions": _string_list_field(
                body, "open_questions", 20, 500),
        }
        for name in ("requires_network", "workflow_suitable"):
            value = body.get(name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name} 必须是布尔值")
            parsed[name] = value
        return parsed, ""
    except ValueError as e:
        return {}, str(e)


def _workflow_center_detail(app: App, workflow_id: str,
                            data: Dict[str, Any],
                            run_limit: int) -> Dict[str, Any]:
    """PRD 89.3：工作流定义 + 版本列表 + 历史运行与产物。"""
    versions = [
        {"version": w["version"], "status": w["status"],
         "updated_at": w["updated_at"]}
        for w in app.db.list_workflow_defs()
        if w["workflow_id"] == workflow_id
    ]
    runs: List[Dict[str, Any]] = []
    for task in app.db.list_tasks(limit=1000):
        if task.workflow_ref != workflow_id:
            continue
        cp = app.db.get_latest_checkpoint(task.task_id) or {}
        state = cp.get("task_state") or {}
        runs.append({
            "task_id": task.task_id, "status": task.task_status.value,
            "user_goal": task.user_goal, "created_at": task.created_at,
            "updated_at": task.updated_at, "result": task.result,
            "failure_reason": task.failure_reason,
            "workflow_version": cp.get("workflow_version"),
            "completed_nodes": list(cp.get("completed_nodes") or []),
            "pending_nodes": list(cp.get("pending_nodes") or []),
            "artifacts": state.get("workflow_outputs") or {},
        })
        if len(runs) >= run_limit:
            break
    return {"workflow": data, "versions": versions, "runs": runs}


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
        self.host, self.port = self.httpd.server_address[:2]

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
