"""Durable ownership and fencing; lease expiry never proves an effect did not occur."""
import contextvars
import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .task import Task, TaskStatus


class LeaseLost(RuntimeError):
    pass


class LeaseBusy(ValueError):
    pass


class RecoveryBlocked(ValueError):
    pass


@dataclass
class LeaseToken:
    db: object
    task_id: str
    run_id: str
    owner_id: str
    generation: int


CURRENT_LEASE = contextvars.ContextVar("execution_lease", default=None)


def assert_connection_lease(conn, db):
    token = CURRENT_LEASE.get()
    if token is None or Path(token.db.path).resolve() != Path(db.path).resolve():
        return
    row = conn.execute("SELECT * FROM run_leases WHERE task_id=?", (token.task_id,)).fetchone()
    if (not row or not row["active"] or row["run_id"] != token.run_id or
            row["owner_id"] != token.owner_id or row["generation"] != token.generation or
            row["expires_at"] <= time.time()):
        raise LeaseLost("运行所有权已失效，禁止旧执行者提交")
    task = conn.execute("SELECT status FROM tasks WHERE task_id=?", (token.task_id,)).fetchone()
    if task and task["status"] == TaskStatus.CANCELLED.value:
        raise LeaseLost("任务已取消")


def assert_current_lease():
    token = CURRENT_LEASE.get()
    if token:
        with token.db._lock:
            assert_connection_lease(token.db._conn, token.db)


def guard_write(db):
    """Called under Database._lock; keep validation and subsequent write atomic."""
    token = CURRENT_LEASE.get()
    if token is None or Path(token.db.path).resolve() != Path(db.path).resolve():
        return
    db._conn.execute("BEGIN IMMEDIATE")
    try:
        assert_connection_lease(db._conn, db)
    except BaseException:
        db._conn.rollback()
        raise


class RunOwnership:
    def __init__(self, db, ttl=15.0):
        self.db, self.ttl = db, ttl
        self.owner_id = "worker_" + uuid.uuid4().hex

    def claim(self, conn, task, *, recovery=False):
        """Participates in the same transaction as task/run creation."""
        now = time.time()
        old = conn.execute("SELECT * FROM run_leases WHERE task_id=?", (task.task_id,)).fetchone()
        if old and old["active"] and old["expires_at"] > now:
            raise LeaseBusy("运行仍有有效租约，不能接管")
        if recovery:
            self._check_recovery(conn, task, old, old_run=task.parent_run_id)
            conn.execute("UPDATE task_runs SET finished_at=?,outcome='INTERRUPTED'"
                         " WHERE run_id=? AND finished_at IS NULL", (now, task.parent_run_id))
        generation = old["generation"] + 1 if old else 1
        conn.execute("INSERT OR REPLACE INTO run_leases VALUES (?,?,?,?,?,1)",
                     (task.task_id, task.current_run_id, self.owner_id, generation, now + self.ttl))
        task.runtime_manifest["run_lease"] = {"owner_id": self.owner_id, "generation": generation}
        return LeaseToken(self.db, task.task_id, task.current_run_id, self.owner_id, generation)

    def _check_recovery(self, conn, task, lease, old_run=None):
        old_run = old_run or task.current_run_id
        if not lease or not lease["active"] or lease["run_id"] != old_run:
            raise LeaseBusy("缺少可核验的失联运行所有权记录")
        if lease["expires_at"] > time.time():
            raise LeaseBusy("运行仍有有效租约，不能接管")
        run = conn.execute("SELECT finished_at FROM task_runs WHERE run_id=?", (old_run,)).fetchone()
        if not run or run["finished_at"] is not None:
            raise LeaseBusy("原运行已经结束")
        cp = conn.execute("SELECT snapshot FROM task_checkpoints WHERE task_id=?"
                          " ORDER BY created_at DESC,rowid DESC LIMIT 1", (task.task_id,)).fetchone()
        if not cp or not json.loads(cp["snapshot"]).get("budget_state", {}).get("usage"):
            raise RecoveryBlocked("缺少包含累计预算的恢复检查点")
        pending = conn.execute("SELECT effect_id FROM task_side_effects WHERE task_id=?"
                               " AND status IN ('planned','submitted','unknown') LIMIT 1",
                               (task.task_id,)).fetchone()
        if pending:
            raise RecoveryBlocked("失联运行存在未决副作用，必须先对账")

    def inspect_recovery(self, task):
        with self.db._lock:
            lease = self.db._conn.execute("SELECT * FROM run_leases WHERE task_id=?", (task.task_id,)).fetchone()
            self._check_recovery(self.db._conn, task, lease)

    def interrupt(self, task_id, reason):
        """Leave blocked recovery suspended without creating a speculative child run."""
        with self.db._lock:
            conn = self.db._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT data FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if not row:
                    raise KeyError("任务不存在")
                task = Task.from_dict(json.loads(row["data"]))
                lease = conn.execute("SELECT * FROM run_leases WHERE task_id=?", (task_id,)).fetchone()
                if (not lease or not lease["active"] or lease["run_id"] != task.current_run_id or
                        lease["expires_at"] > time.time()):
                    raise LeaseBusy("原运行不满足中断准备条件")
                conn.execute("UPDATE run_leases SET active=0 WHERE task_id=?", (task_id,))
                conn.execute("UPDATE task_runs SET finished_at=?,outcome=?"
                             " WHERE run_id=? AND finished_at IS NULL", (
                                 time.time(), task.task_status.value if task.is_terminal() else "INTERRUPTED",
                                 task.current_run_id))
                conn.execute("UPDATE task_side_effects SET status='unknown',updated_at=?"
                             " WHERE task_id=? AND status IN ('planned','submitted')", (time.time(), task_id))
                if task.task_status != TaskStatus.WAITING_USER and not task.is_terminal():
                    task.task_status = TaskStatus.SUSPENDED
                task.runtime_manifest["interruption_reason"] = reason
                if "检查点" in reason:
                    task.runtime_manifest["recovery_blocked"] = reason
                task.updated_at = time.time()
                conn.execute("UPDATE tasks SET status=?,data=? WHERE task_id=?", (
                    task.task_status.value, json.dumps(task.to_dict(), ensure_ascii=False), task_id))
                conn.commit()
                return task
            except BaseException:
                conn.rollback()
                raise

    def start_heartbeat(self, token):
        stop = threading.Event()

        def renew():
            while not stop.wait(max(0.02, self.ttl / 3)):
                try:
                    with self.db._lock:
                        self.db._conn.execute("BEGIN IMMEDIATE")
                        now = time.time()
                        cursor = self.db._conn.execute(
                            "UPDATE run_leases SET expires_at=? WHERE task_id=? AND owner_id=?"
                            " AND generation=? AND run_id=? AND active=1 AND expires_at>?",
                            (now + self.ttl, token.task_id, token.owner_id, token.generation, token.run_id, now))
                        self.db._conn.commit()
                    if not cursor.rowcount:
                        return
                except Exception:
                    with self.db._lock:
                        self.db._conn.rollback()
                    return  # loss of renewal fails closed at subsequent execution/write boundaries

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        return stop, thread

    def release(self, token):
        with self.db._lock:
            conn = self.db._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM run_leases WHERE task_id=?", (token.task_id,)).fetchone()
                if row and row["owner_id"] == token.owner_id and row["generation"] == token.generation:
                    task = conn.execute("SELECT status FROM tasks WHERE task_id=?", (token.task_id,)).fetchone()
                    if task and task["status"] == TaskStatus.CANCELLED.value:
                        conn.execute("UPDATE task_runs SET finished_at=?,outcome='CANCELLED'"
                                     " WHERE run_id=? AND finished_at IS NULL", (time.time(), token.run_id))
                    finished = conn.execute("SELECT finished_at FROM task_runs WHERE run_id=?", (token.run_id,)).fetchone()
                    if finished and finished["finished_at"] is not None:
                        conn.execute("UPDATE run_leases SET active=0 WHERE task_id=?", (token.task_id,))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
