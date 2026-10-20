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
            c.execute("""CREATE TABLE IF NOT EXISTS tasks(
                task_id TEXT PRIMARY KEY, status TEXT, intent TEXT, domain TEXT,
                risk_level TEXT, user_goal TEXT, created_at REAL, data TEXT)""")
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
            c.commit()

    # ---- tasks ----
    def save_task(self, task: Task) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (task.task_id, task.task_status.value, task.intent, task.domain,
                 task.risk_level.value, task.user_goal, task.created_at,
                 json.dumps(task.to_dict(), ensure_ascii=False)))
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

    def close(self) -> None:
        self._conn.close()
