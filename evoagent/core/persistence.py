"""SQLite 持久化：任务、运行、检查点（PRD 91 核心数据实体子集）。"""

import json
import sqlite3
import threading
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

    def close(self) -> None:
        self._conn.close()
