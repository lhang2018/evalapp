"""SQLite 持久化：任务、运行、检查点（PRD 91 核心数据实体子集）。"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .task import Task


class Database:
    def __init__(self, path: str = ".evoagent/evoagent.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            c = self._conn
            # 开发期：不兼容旧库直接重建（生产需正式迁移）
            cols = [r[1] for r in c.execute("PRAGMA table_info(tasks)")]
            if cols and "created_at" not in cols:
                c.execute("DROP TABLE tasks")
            elif cols and "goal_id" not in cols:
                # 旧库迁移：补 goal_id 列（G-01 目标关联）
                c.execute("ALTER TABLE tasks ADD COLUMN goal_id TEXT")
            c.execute("""CREATE TABLE IF NOT EXISTS tasks(
                task_id TEXT PRIMARY KEY, status TEXT, intent TEXT, domain TEXT,
                risk_level TEXT, user_goal TEXT, created_at REAL, data TEXT,
                goal_id TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_goal"
                      " ON tasks(goal_id)")
            c.execute("""CREATE TABLE IF NOT EXISTS task_runs(
                run_id TEXT PRIMARY KEY, task_id TEXT, started_at REAL,
                finished_at REAL, outcome TEXT, version_manifest TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS task_checkpoints(
                checkpoint_id TEXT PRIMARY KEY, task_id TEXT,
                created_at REAL, snapshot TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS workflow_runs(
                workflow_run_id TEXT PRIMARY KEY, workflow_id TEXT, task_id TEXT,
                status TEXT, node_states TEXT, updated_at REAL)""")
            # 客户端幂等提交（PRD 74）：同一 client_request_id 只创建一个任务
            c.execute("""CREATE TABLE IF NOT EXISTS client_requests(
                client_request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                created_at REAL)""")
            # 双端协同（Arch 双端 §3/§5/§8）：设备安装实例、运行清单、同步回执
            c.execute("""CREATE TABLE IF NOT EXISTS device_installations(
                installation_id TEXT PRIMARY KEY, device_id TEXT, platform TEXT,
                app_version TEXT, current_release_id TEXT, target_release_id TEXT,
                status TEXT, created_at REAL, last_seen_at REAL, data TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS device_deployments(
                deployment_id TEXT PRIMARY KEY, installation_id TEXT,
                release_id TEXT, action TEXT, outcome TEXT, created_at REAL,
                data TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_device_deployments_installation
                ON device_deployments(installation_id, created_at)""")
            c.execute("""CREATE TABLE IF NOT EXISTS sync_batches(
                batch_id TEXT PRIMARY KEY, receipt_id TEXT, client_id TEXT,
                installation_id TEXT, received_at REAL, event_count INTEGER,
                accepted INTEGER, duplicates INTEGER, rejected INTEGER,
                data TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS runtime_manifests(
                manifest_id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT,
                device_id TEXT, installation_id TEXT, created_at REAL,
                data TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_runtime_manifests_task
                ON runtime_manifests(task_id, created_at)""")
            # 候选状态同步：记录每个安装实例已看到的候选阶段（双端协同 §2/§8）
            c.execute("""CREATE TABLE IF NOT EXISTS device_candidate_states(
                installation_id TEXT, candidate_id TEXT, status TEXT,
                release_id TEXT, first_seen_at REAL, updated_at REAL, data TEXT,
                PRIMARY KEY(installation_id, candidate_id))""")
            # 诊断案例（PRD 41-45 / Architecture 9.2-9.3）
            c.execute("""CREATE TABLE IF NOT EXISTS diagnostic_cases(
                case_id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, status TEXT,
                created_at REAL, data TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_diagnostic_cases_task
                ON diagnostic_cases(task_id)""")
            c.execute("""CREATE TABLE IF NOT EXISTS diagnostic_hypotheses(
                hypothesis_id TEXT PRIMARY KEY, case_id TEXT, category TEXT,
                confidence REAL, status TEXT, data TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_diagnostic_hypotheses_case
                ON diagnostic_hypotheses(case_id)""")
            c.execute("""CREATE TABLE IF NOT EXISTS diagnostic_experiments(
                experiment_id TEXT PRIMARY KEY, case_id TEXT, hypothesis_id TEXT,
                method TEXT, result TEXT, cost REAL, started_at REAL,
                finished_at REAL, data TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_diagnostic_experiments_case
                ON diagnostic_experiments(case_id)""")
            # 进化轨状态（PRD 46-54 / Arch 10）：候选与发布必须可恢复，不能只存内存
            c.execute("""CREATE TABLE IF NOT EXISTS evolution_candidates(
                candidate_id TEXT PRIMARY KEY, status TEXT, risk_level TEXT,
                created_at REAL, data TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_evolution_candidates_status
                ON evolution_candidates(status)""")
            c.execute("""CREATE TABLE IF NOT EXISTS release_manifests(
                release_id TEXT PRIMARY KEY, version TEXT, created_at REAL,
                data TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS release_events(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT,
                release_id TEXT, created_at REAL, data TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_release_events_release
                ON release_events(release_id)""")
            c.execute("""CREATE TABLE IF NOT EXISTS tool_executions(
                idem_key TEXT PRIMARY KEY, tool_id TEXT, created_at REAL,
                result TEXT)""")
            # 日/月预算账本（PRD 55-58）：scope:period 为键
            c.execute("""CREATE TABLE IF NOT EXISTS budget_ledger(
                ledger_key TEXT PRIMARY KEY, model_calls INTEGER DEFAULT 0,
                tool_calls INTEGER DEFAULT 0, cost REAL DEFAULT 0,
                seconds REAL DEFAULT 0, updated_at REAL)""")
            # 工作流资产库（PRD 13.4 / Arch 16）
            c.execute("""CREATE TABLE IF NOT EXISTS workflow_defs(
                workflow_id TEXT, version TEXT, status TEXT, updated_at REAL,
                data TEXT, PRIMARY KEY(workflow_id, version))""")
            # 从成功任务抽取的工作流候选（Arch 16.2）：单次成功不足以固化
            c.execute("""CREATE TABLE IF NOT EXISTS workflow_candidates(
                candidate_id TEXT PRIMARY KEY, intent TEXT, signature TEXT,
                status TEXT, support INTEGER, sample_count INTEGER,
                created_at REAL, updated_at REAL, data TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_workflow_candidates_status
                ON workflow_candidates(status)""")
            c.execute("""CREATE TABLE IF NOT EXISTS eval_reports(
                report_id TEXT PRIMARY KEY, dataset_id TEXT, candidate_id TEXT,
                created_at REAL, data TEXT)""")
            # 安全控制面审计（PRD 84 / Arch 18）
            c.execute("""CREATE TABLE IF NOT EXISTS audit_log(
                audit_id TEXT PRIMARY KEY, at REAL, actor TEXT, action TEXT,
                target TEXT, outcome TEXT, detail TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_audit_log_at
                ON audit_log(at)""")
            # 日志分层（PRD 38-39 / Arch 9.2）：执行轨迹与诊断证据
            c.execute("""CREATE TABLE IF NOT EXISTS execution_traces(
                trace_id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, step INTEGER,
                at REAL, kind TEXT, summary TEXT, payload TEXT,
                privacy_classification TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_execution_traces_task
                ON execution_traces(task_id, step)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_execution_traces_at
                ON execution_traces(at)""")
            c.execute("""CREATE TABLE IF NOT EXISTS diagnostic_evidence(
                evidence_id TEXT PRIMARY KEY, task_id TEXT, kind TEXT, ref TEXT,
                summary TEXT, privacy_classification TEXT, authorized INTEGER,
                created_at REAL)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_diagnostic_evidence_task
                ON diagnostic_evidence(task_id)""")
            # 模型迁移记录（PRD 20 / Arch 7.4）：兼容性、回归、发布与启用状态
            c.execute("""CREATE TABLE IF NOT EXISTS model_migrations(
                migration_id TEXT PRIMARY KEY, old_model_id TEXT,
                new_model_id TEXT, status TEXT, created_at REAL,
                updated_at REAL, data TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_model_migrations_status
                ON model_migrations(status)""")
            # 记忆质量评测报告（PRD 28）
            c.execute("""CREATE TABLE IF NOT EXISTS memory_eval_reports(
                report_id TEXT PRIMARY KEY, dataset_id TEXT, version TEXT,
                created_at REAL, data TEXT)""")
            # 离线诊断包受控导入（补充 C §8）：隔离区，不与线上事件混用
            c.execute("""CREATE TABLE IF NOT EXISTS diagnostics_imports(
                import_id TEXT PRIMARY KEY, source TEXT, status TEXT,
                file_name TEXT, size_bytes INTEGER, sha256 TEXT UNIQUE,
                format_version TEXT, exported_at REAL, entry_count INTEGER,
                event_count INTEGER, warnings TEXT, actor TEXT,
                imported_at REAL)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_diagnostics_imports_at
                ON diagnostics_imports(imported_at)""")
            c.execute("""CREATE TABLE IF NOT EXISTS diagnostics_import_events(
                import_id TEXT, event_id TEXT, task_id TEXT, run_id TEXT,
                runtime_manifest_id TEXT, event_type TEXT, timestamp REAL,
                sequence_no INTEGER, severity TEXT, payload TEXT,
                review_status TEXT, conflict_reason TEXT,
                PRIMARY KEY(import_id, event_id))""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_import_events_task
                ON diagnostics_import_events(task_id)""")
            # 诊断附件（补充 C §6）：独立上传任务，数据库只存引用/摘要/分块
            c.execute("""CREATE TABLE IF NOT EXISTS attachments(
                attachment_id TEXT PRIMARY KEY, file_name TEXT,
                content_type TEXT, size_bytes INTEGER, sha256 TEXT,
                chunk_size INTEGER, chunk_count INTEGER, consent INTEGER,
                status TEXT, received_bytes INTEGER DEFAULT 0, actor TEXT,
                created_at REAL, updated_at REAL, completed_at REAL,
                last_error TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS attachment_chunks(
                attachment_id TEXT, chunk_index INTEGER, size_bytes INTEGER,
                sha256 TEXT, data BLOB, created_at REAL,
                PRIMARY KEY(attachment_id, chunk_index))""")
            c.commit()

    # ---- tasks ----
    def save_task(self, task: Task) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks"
                " (task_id, status, intent, domain, risk_level, user_goal,"
                "  created_at, data, goal_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (task.task_id, task.task_status.value, task.intent, task.domain,
                 task.risk_level.value, task.user_goal, task.created_at,
                 json.dumps(task.to_dict(), ensure_ascii=False), task.goal_id))
            self._conn.commit()

    def get_task(self, task_id: str) -> Optional[Task]:
        row = self._conn.execute(
            "SELECT data FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        return Task.from_dict(json.loads(row["data"]))

    def list_tasks(self, limit: int = 50) -> List[Task]:
        rows = self._conn.execute(
            "SELECT data FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [Task.from_dict(json.loads(r["data"])) for r in rows]

    def list_tasks_by_goal(self, goal_id: str, limit: int = 200) -> List[Task]:
        """按长期目标拉任务（G-01 目标注册表前置；索引列 goal_id）。"""
        rows = self._conn.execute(
            "SELECT data FROM tasks WHERE goal_id=?"
            " ORDER BY created_at ASC LIMIT ?", (goal_id, limit)).fetchall()
        return [Task.from_dict(json.loads(r["data"])) for r in rows]

    # ---- runs ----
    def save_run(self, run_id: str, task_id: str, started_at: float,
                 finished_at: Optional[float], outcome: str,
                 version_manifest: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO task_runs VALUES (?,?,?,?,?,?)",
                (run_id, task_id, started_at, finished_at, outcome,
                 json.dumps(version_manifest, ensure_ascii=False)))
            self._conn.commit()

    def list_runs(self, task_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT run_id, task_id, started_at, finished_at, outcome,"
            " version_manifest FROM task_runs WHERE task_id=?"
            " ORDER BY started_at ASC LIMIT ?", (task_id, limit)).fetchall()
        out = []
        for r in rows:
            try:
                manifest = json.loads(r["version_manifest"])
            except (TypeError, ValueError):
                manifest = {}
            out.append({"run_id": r["run_id"], "task_id": r["task_id"],
                        "started_at": r["started_at"],
                        "finished_at": r["finished_at"],
                        "outcome": r["outcome"],
                        "version_manifest": manifest})
        return out

    # ---- checkpoints ----
    def save_checkpoint(self, checkpoint_id: str, task_id: str,
                        created_at: float, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO task_checkpoints VALUES (?,?,?,?)",
                (checkpoint_id, task_id, created_at,
                 json.dumps(snapshot, ensure_ascii=False, default=str)))
            self._conn.commit()

    def get_latest_checkpoint(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT snapshot FROM task_checkpoints WHERE task_id=?"
            " ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["snapshot"])

    # ---- client idempotency ----
    def get_request_task_id(self, client_request_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT task_id FROM client_requests WHERE client_request_id=?",
            (client_request_id,)).fetchone()
        return row["task_id"] if row else None

    def save_request(self, client_request_id: str, task_id: str) -> bool:
        """INSERT OR IGNORE；返回是否首次登记。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO client_requests VALUES (?,?,?)",
                (client_request_id, task_id, time.time()))
            self._conn.commit()
            return cur.rowcount > 0

    # ---- device installations / deployments（双端协同 §3/§5/§8）----
    def save_installation(self, installation_id: str, device_id: str,
                          platform: str, app_version: str,
                          current_release_id: Optional[str],
                          target_release_id: Optional[str], status: str,
                          data: Dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_installations VALUES"
                " (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(installation_id) DO UPDATE SET"
                " device_id=excluded.device_id, platform=excluded.platform,"
                " app_version=excluded.app_version,"
                " current_release_id=excluded.current_release_id,"
                " target_release_id=excluded.target_release_id,"
                " status=excluded.status, last_seen_at=excluded.last_seen_at,"
                " data=excluded.data",
                (installation_id, device_id, platform, app_version,
                 current_release_id, target_release_id, status, now, now,
                 json.dumps(data, ensure_ascii=False, default=str)))
            self._conn.commit()

    def get_installation(self,
                         installation_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT installation_id, device_id, platform, app_version,"
            " current_release_id, target_release_id, status, created_at,"
            " last_seen_at, data FROM device_installations"
            " WHERE installation_id=?", (installation_id,)).fetchone()
        if not row:
            return None
        return {"installation_id": row["installation_id"],
                "device_id": row["device_id"],
                "platform": row["platform"],
                "app_version": row["app_version"],
                "current_release_id": row["current_release_id"],
                "target_release_id": row["target_release_id"],
                "status": row["status"],
                "created_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "data": json.loads(row["data"] or "{}")}

    def list_installations(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT installation_id FROM device_installations"
            " ORDER BY last_seen_at DESC LIMIT ?", (limit,)).fetchall()
        return [self.get_installation(r["installation_id"]) for r in rows]

    def save_device_deployment(self, deployment_id: str,
                               installation_id: str, release_id: str,
                               action: str, outcome: str,
                               data: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO device_deployments VALUES"
                " (?,?,?,?,?,?,?)",
                (deployment_id, installation_id, release_id, action, outcome,
                 time.time(), json.dumps(data, ensure_ascii=False,
                                         default=str)))
            self._conn.commit()

    def get_device_deployment(self,
                              deployment_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT deployment_id, installation_id, release_id, action,"
            " outcome, created_at, data FROM device_deployments"
            " WHERE deployment_id=?", (deployment_id,)).fetchone()
        if not row:
            return None
        return {"deployment_id": row["deployment_id"],
                "installation_id": row["installation_id"],
                "release_id": row["release_id"], "action": row["action"],
                "outcome": row["outcome"], "created_at": row["created_at"],
                "data": json.loads(row["data"] or "{}")}

    def list_device_deployments(self, installation_id: str,
                                limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT deployment_id, installation_id, release_id, action,"
            " outcome, created_at, data FROM device_deployments"
            " WHERE installation_id=? ORDER BY created_at DESC LIMIT ?",
            (installation_id, limit)).fetchall()
        return [{"deployment_id": r["deployment_id"],
                 "installation_id": r["installation_id"],
                 "release_id": r["release_id"], "action": r["action"],
                 "outcome": r["outcome"], "created_at": r["created_at"],
                 "data": json.loads(r["data"] or "{}")} for r in rows]

    # ---- 候选状态同步（双端协同 §2/§8）----
    def record_device_candidate_state(self, installation_id: str,
                                      candidate_id: str, status: str,
                                      release_id: str,
                                      data: Dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_candidate_states VALUES"
                " (?,?,?,?,?,?,?)"
                " ON CONFLICT(installation_id, candidate_id) DO UPDATE SET"
                " status=excluded.status, release_id=excluded.release_id,"
                " updated_at=excluded.updated_at, data=excluded.data",
                (installation_id, candidate_id, status, release_id, now, now,
                 json.dumps(data, ensure_ascii=False, default=str)))
            self._conn.commit()

    def list_device_candidate_states(self, installation_id: str,
                                     limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT installation_id, candidate_id, status, release_id,"
            " first_seen_at, updated_at, data FROM device_candidate_states"
            " WHERE installation_id=? ORDER BY updated_at DESC LIMIT ?",
            (installation_id, limit)).fetchall()
        return [{"installation_id": r["installation_id"],
                 "candidate_id": r["candidate_id"],
                 "status": r["status"], "release_id": r["release_id"],
                 "first_seen_at": r["first_seen_at"],
                 "updated_at": r["updated_at"],
                 "data": json.loads(r["data"] or "{}")} for r in rows]

    # ---- sync receipts（双端协同 §3.3）----
    def get_sync_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT batch_id, receipt_id, client_id, installation_id,"
            " received_at, event_count, accepted, duplicates, rejected, data"
            " FROM sync_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            return None
        return {"batch_id": row["batch_id"],
                "receipt_id": row["receipt_id"],
                "client_id": row["client_id"],
                "installation_id": row["installation_id"],
                "received_at": row["received_at"],
                "event_count": row["event_count"],
                "accepted": row["accepted"],
                "duplicates": row["duplicates"],
                "rejected": row["rejected"],
                "data": json.loads(row["data"] or "{}")}

    def save_sync_batch(self, batch_id: str, receipt_id: str,
                        client_id: str, installation_id: Optional[str],
                        event_count: int, accepted: int, duplicates: int,
                        rejected: int, data: Dict[str, Any]) -> bool:
        """INSERT OR IGNORE；只有首个到达的批次生成回执。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO sync_batches VALUES"
                " (?,?,?,?,?,?,?,?,?,?)",
                (batch_id, receipt_id, client_id, installation_id,
                 time.time(), event_count, accepted, duplicates, rejected,
                 json.dumps(data, ensure_ascii=False, default=str)))
            self._conn.commit()
            return cur.rowcount > 0

    # ---- runtime manifests（双端协同 §3.2）----
    def save_runtime_manifest(self, manifest_id: str, task_id: str,
                              run_id: str, device_id: Optional[str],
                              installation_id: Optional[str],
                              data: Dict[str, Any],
                              created_at: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runtime_manifests VALUES"
                " (?,?,?,?,?,?,?)",
                (manifest_id, task_id, run_id, device_id, installation_id,
                 time.time() if created_at is None else created_at,
                 json.dumps(data, ensure_ascii=False, default=str)))
            self._conn.commit()

    def get_runtime_manifest(self,
                             manifest_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT manifest_id, task_id, run_id, device_id, installation_id,"
            " created_at, data FROM runtime_manifests WHERE manifest_id=?",
            (manifest_id,)).fetchone()
        if not row:
            return None
        return {"manifest_id": row["manifest_id"],
                "task_id": row["task_id"], "run_id": row["run_id"],
                "device_id": row["device_id"],
                "installation_id": row["installation_id"],
                "created_at": row["created_at"],
                "manifest": json.loads(row["data"] or "{}")}

    def list_runtime_manifests(self, task_id: str,
                               limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT manifest_id FROM runtime_manifests WHERE task_id=?"
            " ORDER BY created_at DESC LIMIT ?",
            (task_id, limit)).fetchall()
        return [self.get_runtime_manifest(r["manifest_id"]) for r in rows]

    # ---- offline diagnostics import（补充 C §8）----
    def get_diagnostics_import(self,
                               import_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM diagnostics_imports WHERE import_id=?",
            (import_id,)).fetchone()
        return dict(row) if row else None

    def get_diagnostics_import_by_sha(
            self, sha256: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM diagnostics_imports WHERE sha256=?",
            (sha256,)).fetchone()
        return dict(row) if row else None

    def list_diagnostics_imports(
            self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM diagnostics_imports"
            " ORDER BY imported_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def save_diagnostics_import(self, import_id: str, file_name: str,
                                size_bytes: int, sha256: str,
                                format_version: str, exported_at: float,
                                entry_count: int, event_count: int,
                                warnings: List[str], actor: str,
                                source: str = "offline_import",
                                status: str = "quarantined") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO diagnostics_imports VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (import_id, source, status, file_name, size_bytes, sha256,
                 format_version, exported_at, entry_count, event_count,
                 json.dumps(warnings, ensure_ascii=False), actor, time.time()))
            self._conn.commit()
            return cur.rowcount > 0

    def save_diagnostics_import_event(
            self, import_id: str, event: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO diagnostics_import_events VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?)",
                (import_id, event["event_id"], event.get("task_id"),
                 event.get("run_id"), event.get("runtime_manifest_id"),
                 event["event_type"], event["timestamp"],
                 event.get("sequence_no"), event.get("severity", "info"),
                 json.dumps(event.get("payload") or {}, ensure_ascii=False,
                            default=str),
                 event.get("review_status", "accepted"),
                 event.get("conflict_reason") or ""))
            self._conn.commit()

    def list_diagnostics_import_events(
            self, import_id: str, limit: int = 5000) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM diagnostics_import_events WHERE import_id=?"
            " ORDER BY sequence_no IS NULL, sequence_no ASC, timestamp ASC"
            " LIMIT ?", (import_id, limit)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except Exception:  # noqa: BLE001
                item["payload"] = {}
            out.append(item)
        return out

    # ---- attachments（补充 C §6）----
    def get_attachment(self,
                       attachment_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM attachments WHERE attachment_id=?",
            (attachment_id,)).fetchone()
        return dict(row) if row else None

    def create_attachment(self, attachment_id: str, file_name: str,
                          content_type: str, size_bytes: int, sha256: str,
                          chunk_size: int, chunk_count: int, consent: bool,
                          actor: str) -> bool:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO attachments VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attachment_id, file_name, content_type, size_bytes, sha256,
                 chunk_size, chunk_count, 1 if consent else 0, "uploading", 0,
                 actor, now, now, None, None))
            self._conn.commit()
            return cur.rowcount > 0

    def save_attachment_chunk(self, attachment_id: str, index: int,
                              data: bytes, sha256: str) -> Dict[str, Any]:
        """写入/覆盖一个分块；返回最新接收字节与分块进度。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO attachment_chunks VALUES"
                " (?,?,?,?,?,?)",
                (attachment_id, index, len(data), sha256, data, now))
            total = self._conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM attachment_chunks"
                " WHERE attachment_id=?", (attachment_id,)).fetchone()[0]
            self._conn.execute(
                "UPDATE attachments SET received_bytes=?, updated_at=?"
                " WHERE attachment_id=?", (int(total), now, attachment_id))
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT chunk_index FROM attachment_chunks"
                " WHERE attachment_id=? ORDER BY chunk_index",
                (attachment_id,)).fetchall()
        return {"received_bytes": int(total),
                "received_chunks": [int(r[0]) for r in rows]}

    def list_attachment_chunks(self,
                               attachment_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT chunk_index, size_bytes FROM attachment_chunks"
            " WHERE attachment_id=? ORDER BY chunk_index",
            (attachment_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_attachment_chunks(self, attachment_id: str) -> int:
        """摘要校验失败后丢弃已收分块，客户端必须整体重传。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM attachment_chunks WHERE attachment_id=?",
                (attachment_id,))
            self._conn.execute(
                "UPDATE attachments SET received_bytes=0, updated_at=?"
                " WHERE attachment_id=?", (time.time(), attachment_id))
            self._conn.commit()
            return int(cur.rowcount)

    def assemble_attachment(self, attachment_id: str) -> bytes:
        rows = self._conn.execute(
            "SELECT data FROM attachment_chunks WHERE attachment_id=?"
            " ORDER BY chunk_index", (attachment_id,)).fetchall()
        return b"".join(bytes(r[0]) for r in rows)

    def mark_attachment(self, attachment_id: str, status: str,
                        error: Optional[str] = None,
                        completed_at: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE attachments SET status=?, last_error=?,"
                " completed_at=?, updated_at=? WHERE attachment_id=?",
                (status, error, completed_at, time.time(), attachment_id))
            self._conn.commit()

    # ---- diagnostics（PRD 41-45）----
    def save_diagnostic_case(self, case: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO diagnostic_cases VALUES (?,?,?,?,?,?)",
                (case.case_id, case.task_id, case.run_id, case.status.value,
                 case.created_at,
                 json.dumps(case.to_dict(), ensure_ascii=False, default=str)))
            self._conn.commit()

    def get_diagnostic_case(self, case_id: str) -> Optional[Any]:
        from ..evolution.diagnostics import DiagnosticCase
        row = self._conn.execute(
            "SELECT data FROM diagnostic_cases WHERE case_id=?",
            (case_id,)).fetchone()
        return DiagnosticCase.from_dict(json.loads(row["data"])) if row else None

    def get_case_by_task(self, task_id: str) -> Optional[Any]:
        from ..evolution.diagnostics import DiagnosticCase
        row = self._conn.execute(
            "SELECT data FROM diagnostic_cases WHERE task_id=?"
            " ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
        return DiagnosticCase.from_dict(json.loads(row["data"])) if row else None

    def list_diagnostic_cases(self, limit: int = 50,
                              status: Optional[str] = None) -> List[Any]:
        from ..evolution.diagnostics import DiagnosticCase
        if status:
            rows = self._conn.execute(
                "SELECT data FROM diagnostic_cases WHERE status=?"
                " ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT data FROM diagnostic_cases ORDER BY created_at DESC"
                " LIMIT ?", (limit,)).fetchall()
        return [DiagnosticCase.from_dict(json.loads(r["data"])) for r in rows]

    def save_hypothesis(self, h: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO diagnostic_hypotheses VALUES (?,?,?,?,?,?)",
                (h.hypothesis_id, h.case_id, h.category.value, h.confidence,
                 h.status.value,
                 json.dumps(h.to_dict(), ensure_ascii=False, default=str)))
            self._conn.commit()

    def list_hypotheses(self, case_id: str) -> List[Any]:
        from ..evolution.diagnostics import Hypothesis
        rows = self._conn.execute(
            "SELECT data FROM diagnostic_hypotheses WHERE case_id=?"
            " ORDER BY confidence DESC, rowid ASC", (case_id,)).fetchall()
        return [Hypothesis.from_dict(json.loads(r["data"])) for r in rows]

    def save_experiment(self, exp: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO diagnostic_experiments VALUES"
                " (?,?,?,?,?,?,?,?,?)",
                (exp.experiment_id, exp.case_id, exp.hypothesis_id, exp.method,
                 exp.result, exp.cost, exp.started_at, exp.finished_at,
                 json.dumps(exp.to_dict(), ensure_ascii=False, default=str)))
            self._conn.commit()

    def list_experiments(self, case_id: str) -> List[Any]:
        from ..evolution.diagnostics import DiagnosticExperiment
        rows = self._conn.execute(
            "SELECT data FROM diagnostic_experiments WHERE case_id=?"
            " ORDER BY started_at ASC", (case_id,)).fetchall()
        return [DiagnosticExperiment.from_dict(json.loads(r["data"]))
                for r in rows]

    def count_experiments_since(self, since: float) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM diagnostic_experiments"
            " WHERE started_at >= ?", (since,)).fetchone()
        return int(row["n"]) if row else 0

    # ---- evolution candidates（PRD 46-54）----
    def save_candidate(self, cand: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO evolution_candidates VALUES (?,?,?,?,?)",
                (cand.candidate_id, cand.status.value, cand.risk_level,
                 cand.created_at,
                 json.dumps(cand.to_dict(), ensure_ascii=False, default=str)))
            self._conn.commit()

    def get_candidate(self, candidate_id: str) -> Optional[Any]:
        from ..evolution.candidates import EvolutionCandidate
        row = self._conn.execute(
            "SELECT data FROM evolution_candidates WHERE candidate_id=?",
            (candidate_id,)).fetchone()
        return EvolutionCandidate.from_dict(json.loads(row["data"])) if row else None

    def list_candidates(self, limit: int = 100,
                        status: Optional[str] = None) -> List[Any]:
        from ..evolution.candidates import EvolutionCandidate
        if status:
            rows = self._conn.execute(
                "SELECT data FROM evolution_candidates WHERE status=?"
                " ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT data FROM evolution_candidates ORDER BY created_at DESC"
                " LIMIT ?", (limit,)).fetchall()
        return [EvolutionCandidate.from_dict(json.loads(r["data"])) for r in rows]

    # ---- releases（PRD 59-63）----
    def save_release_manifest(self, m: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO release_manifests VALUES (?,?,?,?)",
                (m.release_id, m.version, m.created_at,
                 json.dumps(m.to_dict(), ensure_ascii=False, default=str)))
            self._conn.commit()

    def get_release_manifest(self, release_id: str) -> Optional[Any]:
        from ..release.manager import ReleaseManifest
        row = self._conn.execute(
            "SELECT data FROM release_manifests WHERE release_id=?",
            (release_id,)).fetchone()
        return ReleaseManifest.from_dict(json.loads(row["data"])) if row else None

    def list_release_manifests(self, limit: int = 100) -> List[Any]:
        from ..release.manager import ReleaseManifest
        rows = self._conn.execute(
            "SELECT data FROM release_manifests ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [ReleaseManifest.from_dict(json.loads(r["data"])) for r in rows]

    def append_release_event(self, kind: str, release_id: str,
                             data: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO release_events(kind, release_id, created_at, data)"
                " VALUES (?,?,?,?)",
                (kind, release_id, time.time(),
                 json.dumps(data, ensure_ascii=False, default=str)))
            self._conn.commit()

    def list_release_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT kind, release_id, created_at, data FROM release_events"
            " ORDER BY event_id DESC LIMIT ?", (limit,)).fetchall()
        return [{"kind": r["kind"], "release_id": r["release_id"],
                 "created_at": r["created_at"], "data": json.loads(r["data"])}
                for r in rows]

    # ---- tool idempotency journal ----
    def get_tool_execution(self, idem_key: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT result FROM tool_executions WHERE idem_key=?",
            (idem_key,)).fetchone()
        return json.loads(row["result"]) if row else None

    def save_tool_execution(self, idem_key: str, tool_id: str,
                            result: Dict[str, Any]) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO tool_executions VALUES (?,?,?,?)",
                (idem_key, tool_id, time.time(),
                 json.dumps(result, ensure_ascii=False, default=str)))
            self._conn.commit()
            return cur.rowcount > 0

    # ---- budget ledger ----
    def record_budget(self, ledger_key: str, model_calls: int = 0,
                      tool_calls: int = 0, cost: float = 0.0,
                      seconds: float = 0.0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO budget_ledger VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(ledger_key) DO UPDATE SET"
                " model_calls=model_calls+excluded.model_calls,"
                " tool_calls=tool_calls+excluded.tool_calls,"
                " cost=cost+excluded.cost,"
                " seconds=seconds+excluded.seconds,"
                " updated_at=excluded.updated_at",
                (ledger_key, model_calls, tool_calls, cost, seconds,
                 time.time()))
            self._conn.commit()

    def budget_usage(self, ledger_key: str) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT model_calls, tool_calls, cost, seconds FROM budget_ledger"
            " WHERE ledger_key=?", (ledger_key,)).fetchone()
        if not row:
            return {"model_calls": 0, "tool_calls": 0, "cost": 0.0,
                    "seconds": 0.0}
        return {"model_calls": int(row["model_calls"]),
                "tool_calls": int(row["tool_calls"]),
                "cost": float(row["cost"]), "seconds": float(row["seconds"])}

    # ---- workflow asset library ----
    def save_workflow_def(self, workflow_id: str, version: str,
                          status: str, data: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO workflow_defs VALUES (?,?,?,?,?)",
                (workflow_id, version, status, time.time(),
                 json.dumps(data, ensure_ascii=False, default=str)))
            self._conn.commit()

    def list_workflow_defs(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT workflow_id, version, status, updated_at, data"
            " FROM workflow_defs ORDER BY updated_at DESC").fetchall()
        return [{"workflow_id": r["workflow_id"], "version": r["version"],
                 "status": r["status"], "updated_at": r["updated_at"],
                 "definition": json.loads(r["data"])} for r in rows]

    def get_workflow_def(self, workflow_id: str,
                         version: Optional[str] = None
                         ) -> Optional[Dict[str, Any]]:
        if version:
            row = self._conn.execute(
                "SELECT data FROM workflow_defs WHERE workflow_id=? AND version=?",
                (workflow_id, version)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT data FROM workflow_defs WHERE workflow_id=?"
                " ORDER BY updated_at DESC LIMIT 1", (workflow_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    # ---- workflow extraction candidates (Arch 16.2) ----
    def save_workflow_candidate(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO workflow_candidates VALUES"
                " (?,?,?,?,?,?,?,?,?)",
                (data["candidate_id"], data.get("intent", ""),
                 json.dumps(data.get("signature") or [], ensure_ascii=False),
                 data.get("status", "candidate"), int(data.get("support", 0)),
                 int(data.get("sample_count", 0)),
                 float(data.get("created_at") or time.time()),
                 float(data.get("updated_at") or time.time()),
                 json.dumps(data, ensure_ascii=False, default=str)))
            self._conn.commit()

    def list_workflow_candidates(self, status: Optional[str] = None,
                                 limit: int = 50) -> List[Dict[str, Any]]:
        if status:
            rows = self._conn.execute(
                "SELECT data FROM workflow_candidates WHERE status=?"
                " ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT data FROM workflow_candidates"
                " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def get_workflow_candidate(self,
                               candidate_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT data FROM workflow_candidates WHERE candidate_id=?",
            (candidate_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    # ---- evaluation reports ----
    def save_eval_report(self, report_id: str, dataset_id: str,
                         candidate_id: Optional[str],
                         data: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO eval_reports VALUES (?,?,?,?,?)",
                (report_id, dataset_id, candidate_id, time.time(),
                 json.dumps(data, ensure_ascii=False, default=str)))
            self._conn.commit()

    def list_eval_reports(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT report_id, dataset_id, candidate_id, created_at, data"
            " FROM eval_reports ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"report_id": r["report_id"], "dataset_id": r["dataset_id"],
                 "candidate_id": r["candidate_id"],
                 "created_at": r["created_at"],
                 "data": json.loads(r["data"])} for r in rows]

    # ---- 模型迁移（PRD 20）----
    def save_model_migration(self, migration_id: str, old_model_id: str,
                             new_model_id: str, status: str,
                             data: Dict[str, Any]) -> None:
        with self._lock:
            now = time.time()
            self._conn.execute(
                "INSERT OR REPLACE INTO model_migrations VALUES (?,?,?,?,?,?,?)",
                (migration_id, old_model_id, new_model_id, status,
                 float(data.get("created_at") or now), now,
                 json.dumps(data, ensure_ascii=False, default=str)))
            self._conn.commit()

    def get_model_migration(self, migration_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT data FROM model_migrations WHERE migration_id=?",
            (migration_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def list_model_migrations(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT data FROM model_migrations"
            " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    # ---- 记忆评测报告（PRD 28）----
    def save_memory_eval_report(self, report_id: str, dataset_id: str,
                                version: str, data: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO memory_eval_reports VALUES (?,?,?,?,?)",
                (report_id, dataset_id, version, time.time(),
                 json.dumps(data, ensure_ascii=False, default=str)))
            self._conn.commit()

    def list_memory_eval_reports(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT report_id, dataset_id, version, created_at, data"
            " FROM memory_eval_reports ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"report_id": r["report_id"], "dataset_id": r["dataset_id"],
                 "version": r["version"], "created_at": r["created_at"],
                 "data": json.loads(r["data"])} for r in rows]

    # ---- audit ----
    def save_audit(self, audit_id: str, actor: str, action: str,
                   target: str, outcome: str, detail: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO audit_log VALUES (?,?,?,?,?,?,?)",
                (audit_id, time.time(), actor, action, target, outcome,
                 json.dumps(detail, ensure_ascii=False, default=str)))
            self._conn.commit()

    def list_audit(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT audit_id, at, actor, action, target, outcome, detail"
            " FROM audit_log ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
        return [{"audit_id": r["audit_id"], "at": r["at"],
                 "actor": r["actor"], "action": r["action"],
                 "target": r["target"], "outcome": r["outcome"],
                 "detail": json.loads(r["detail"])} for r in rows]

    # ---- 日志分层：执行轨迹 ----
    def save_trace(self, trace_id: str, task_id: Optional[str],
                   run_id: Optional[str], step: int, at: float, kind: str,
                   summary: str, payload: str, privacy: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO execution_traces VALUES"
                " (?,?,?,?,?,?,?,?,?)",
                (trace_id, task_id, run_id, step, at, kind, summary,
                 payload, privacy))
            self._conn.commit()

    def max_trace_step(self, task_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(step), 0) FROM execution_traces"
            " WHERE task_id=?", (task_id,)).fetchone()
        return int(row[0]) if row else 0

    def list_traces(self, task_id: Optional[str] = None, limit: int = 200,
                    ascending: bool = True) -> List[Dict[str, Any]]:
        sql = ("SELECT trace_id, task_id, run_id, step, at, kind, summary,"
               " payload, privacy_classification FROM execution_traces")
        args: list = []
        if task_id:
            sql += " WHERE task_id=?"
            args.append(task_id)
        sql += " ORDER BY at " + ("ASC" if ascending else "DESC") + " LIMIT ?"
        args.append(limit)
        rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"]) if r["payload"] else {}
            except Exception:  # noqa: BLE001
                payload = {}
            out.append({"trace_id": r["trace_id"], "task_id": r["task_id"],
                        "run_id": r["run_id"], "step": r["step"],
                        "at": r["at"], "kind": r["kind"],
                        "summary": r["summary"], "payload": payload,
                        "privacy_classification":
                            r["privacy_classification"]})
        return out

    def count_traces(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM execution_traces").fetchone()[0])

    def delete_traces(self, task_id: Optional[str] = None,
                      before: Optional[float] = None) -> int:
        sql, args = "DELETE FROM execution_traces", []
        clauses = []
        if task_id:
            clauses.append("task_id=?")
            args.append(task_id)
        if before is not None:
            clauses.append("at<?")
            args.append(before)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return int(cur.rowcount)

    # ---- 日志分层：诊断证据 ----
    def save_evidence(self, evidence_id: str, task_id: Optional[str],
                      kind: str, ref: str, summary: str,
                      privacy: str = "standard", authorized: bool = False,
                      created_at: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO diagnostic_evidence VALUES"
                " (?,?,?,?,?,?,?,?)",
                (evidence_id, task_id, kind, ref, summary, privacy,
                 1 if authorized else 0,
                 time.time() if created_at is None else created_at))
            self._conn.commit()

    def list_evidence(self, task_id: Optional[str] = None,
                      limit: int = 200) -> List[Dict[str, Any]]:
        sql = ("SELECT evidence_id, task_id, kind, ref, summary,"
               " privacy_classification, authorized, created_at"
               " FROM diagnostic_evidence")
        args: list = []
        if task_id:
            sql += " WHERE task_id=?"
            args.append(task_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rows = self._conn.execute(sql, args).fetchall()
        return [{"evidence_id": r["evidence_id"], "task_id": r["task_id"],
                 "kind": r["kind"], "ref": r["ref"],
                 "summary": r["summary"],
                 "privacy_classification": r["privacy_classification"],
                 "authorized": bool(r["authorized"]),
                 "created_at": r["created_at"]} for r in rows]

    def count_evidence(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM diagnostic_evidence").fetchone()[0])

    def delete_evidence(self, task_id: Optional[str] = None,
                        before: Optional[float] = None) -> int:
        sql, args = "DELETE FROM diagnostic_evidence", []
        clauses = []
        if task_id:
            clauses.append("task_id=?")
            args.append(task_id)
        if before is not None:
            clauses.append("created_at<?")
            args.append(before)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return int(cur.rowcount)

    def delete_diagnostics_for_task(self, task_id: str) -> Dict[str, int]:
        """删除任务关联的诊断案例/假设/实验（隐私删除，PRD 39）。"""
        with self._lock:
            case_ids = [r[0] for r in self._conn.execute(
                "SELECT case_id FROM diagnostic_cases WHERE task_id=?",
                (task_id,))]
            counts = {"cases": 0, "hypotheses": 0, "experiments": 0}
            for cid in case_ids:
                counts["hypotheses"] += int(self._conn.execute(
                    "DELETE FROM diagnostic_hypotheses WHERE case_id=?",
                    (cid,)).rowcount)
                counts["experiments"] += int(self._conn.execute(
                    "DELETE FROM diagnostic_experiments WHERE case_id=?",
                    (cid,)).rowcount)
            counts["cases"] = int(self._conn.execute(
                "DELETE FROM diagnostic_cases WHERE task_id=?",
                (task_id,)).rowcount)
            self._conn.commit()
            return counts

    def count_audit(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM audit_log").fetchone()[0])

    def purge_audit_before(self, before: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM audit_log WHERE at<?", (before,))
            self._conn.commit()
            return int(cur.rowcount)

    def close(self) -> None:
        self._conn.close()
