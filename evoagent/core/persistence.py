"""SQLite 持久化：任务、运行、检查点（PRD 91 核心数据实体子集）。"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import schema
from .schema.migrations import apply_migrations
from .task import Task, TaskStatus
from .leases import guard_write, assert_connection_lease


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
            # 结构变更走版本化迁移（协作基座 F-00.4 / S-12）
            apply_migrations(c)
            # 建表语句按域拆分在 core/schema/（协作基座 F-00.1）：
            # 新增表只需新建域模块并注册，不再改动本文件。
            for stmt in schema.all_statements():
                c.execute(stmt)
            c.commit()

    # ---- tasks ----
    def save_task(self, task: Task) -> None:
        with self._lock:
            guard_write(self)
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
    def claim_run(self, task: Task, expected_run_id, expected_status,
                  started_at: float, *, ownership=None, recovery=False):
        """Compare and claim a task attempt in one SQLite transaction."""
        allowed = (TaskStatus.RUNNING, TaskStatus.SUSPENDED) if recovery else (TaskStatus.CREATED, TaskStatus.SUSPENDED)
        if expected_status not in allowed:
            raise ValueError("任务状态不允许开始运行")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                assert_connection_lease(self._conn, self)
                row = self._conn.execute(
                    "SELECT data FROM tasks WHERE task_id=?", (task.task_id,)).fetchone()
                if row:
                    stored = Task.from_dict(json.loads(row["data"]))
                    if (stored.current_run_id != expected_run_id or
                            stored.task_status != expected_status):
                        raise ValueError("任务状态已改变，请刷新后重试")
                token = ownership.claim(self._conn, task, recovery=recovery) if ownership else None
                if expected_run_id:
                    previous = self._conn.execute(
                        "SELECT finished_at FROM task_runs WHERE run_id=?",
                        (expected_run_id,)).fetchone()
                    if previous and previous["finished_at"] is None:
                        raise ValueError("上一次运行尚未结束")
                self._conn.execute(
                    "INSERT INTO task_runs"
                    " (run_id,task_id,started_at,outcome,version_manifest,parent_run_id)"
                    " VALUES (?,?,?,?,?,?)",
                    (task.current_run_id, task.task_id, started_at, "RUNNING",
                     json.dumps(task.version_manifest, ensure_ascii=False), task.parent_run_id))
                self._conn.execute(
                    "INSERT OR REPLACE INTO tasks"
                    " (task_id,status,intent,domain,risk_level,user_goal,created_at,data,goal_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (task.task_id, task.task_status.value, task.intent, task.domain,
                     task.risk_level.value, task.user_goal, task.created_at,
                     json.dumps(task.to_dict(), ensure_ascii=False), task.goal_id))
                self._conn.commit()
                return token
            except BaseException:
                self._conn.rollback()
                raise

    def record_user_answer(self, task_id: str, request_id: str,
                            answer: str):
        """Atomically consume a specific question, including cross-connection retries."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT data FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if row is None:
                    raise KeyError("任务不存在")
                task = Task.from_dict(json.loads(row["data"]))
                for old in task.user_answers:
                    if old["request_id"] == request_id:
                        if old["answer"] != answer:
                            raise ValueError("该问题已回答，不能替换为不同答案")
                        self._conn.commit()
                        return task, False
                request = task.waiting_request or {}
                if task.task_status != TaskStatus.WAITING_USER or request.get("request_id") != request_id:
                    raise ValueError("任务未等待此问题的答案")
                task.user_answers.append({"request_id": request_id,
                                          "answer": answer, "at": time.time()})
                request["answered"] = True
                task.task_status = TaskStatus.SUSPENDED
                task.updated_at = time.time()
                self._conn.execute(
                    "UPDATE tasks SET status=?,data=? WHERE task_id=?",
                    (task.task_status.value, json.dumps(task.to_dict(), ensure_ascii=False), task_id))
                self._conn.commit()
                return task, True
            except BaseException:
                self._conn.rollback()
                raise

    def save_run(self, run_id: str, task_id: str, started_at: float,
                 finished_at: Optional[float], outcome: str,
                 version_manifest: Dict[str, Any],
                 parent_run_id: Optional[str] = None) -> None:
        with self._lock:
            guard_write(self)
            self._conn.execute(
                "INSERT INTO task_runs"
                " (run_id,task_id,started_at,finished_at,outcome,version_manifest,parent_run_id)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET"
                " finished_at=excluded.finished_at,outcome=excluded.outcome,"
                " version_manifest=excluded.version_manifest"
                " WHERE task_runs.task_id=excluded.task_id",
                (run_id, task_id, started_at, finished_at, outcome,
                 json.dumps(version_manifest, ensure_ascii=False), parent_run_id))
            self._conn.commit()

    def list_runs(self, task_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT run_id, task_id, started_at, finished_at, outcome,"
            " version_manifest, parent_run_id FROM task_runs WHERE task_id=?"
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
                        "parent_run_id": r["parent_run_id"],
                        "version_manifest": manifest})
        return out

    # ---- checkpoints ----
    def save_checkpoint(self, checkpoint_id: str, task_id: str,
                        created_at: float, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            guard_write(self)
            self._conn.execute(
                "INSERT OR REPLACE INTO task_checkpoints VALUES (?,?,?,?)",
                (checkpoint_id, task_id, created_at,
                 json.dumps(snapshot, ensure_ascii=False, default=str)))
            self._conn.commit()

    def get_latest_checkpoint(self, task_id: str) -> Optional[Dict[str, Any]]:
        # created_at 同精度写入可能打平（Windows time.time() 分辨率粗），
        # rowid DESC 决胜：后插必后取（F-13）
        row = self._conn.execute(
            "SELECT snapshot FROM task_checkpoints WHERE task_id=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (task_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["snapshot"])

    # ---- side effects（契约 09 卷 §8，F-03；建表见 core/schema/effects.py）----
    def save_side_effect(self, effect_id: str, task_id: str, run_id: str,
                         tool_id: str, idempotency_key: Optional[str],
                         effect_summary: str, status: str) -> str:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO task_side_effects (effect_id, task_id, run_id,"
                " tool_id, idempotency_key, effect_summary, status,"
                " external_receipt_ref, last_reconciled_at,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (effect_id, task_id, run_id, tool_id, idempotency_key,
                 effect_summary, status, None, None, now, now))
            self._conn.commit()
        return effect_id

    def claim_side_effect(self, effect_id: str, task_id: str, run_id: str,
                          tool_id: str, idempotency_key: str,
                          summary: str) -> bool:
        """Atomic operation claim; duplicate claims never leave an open transaction."""
        now = time.time()
        with self._lock:
            guard_write(self)
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO task_side_effects"
                " (effect_id,task_id,run_id,tool_id,idempotency_key,"
                "effect_summary,status,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,'planned',?,?)",
                (effect_id, task_id, run_id, tool_id, idempotency_key,
                 summary, now, now))
            self._conn.commit()
        return cursor.rowcount == 1

    def update_side_effect_status(self, effect_id: str, status: str, *,
                                  receipt_ref: Optional[str] = None,
                                  reconciled_at: Optional[float] = None
                                  ) -> bool:
        parts = ["status=?", "updated_at=?"]
        args: List[Any] = [status, time.time()]
        if receipt_ref is not None:
            parts.append("external_receipt_ref=?")
            args.append(receipt_ref)
        if reconciled_at is not None:
            parts.append("last_reconciled_at=?")
            args.append(reconciled_at)
        args.append(effect_id)
        with self._lock:
            guard_write(self)
            cur = self._conn.execute(
                f"UPDATE task_side_effects SET {', '.join(parts)}"
                " WHERE effect_id=?", args)
            self._conn.commit()
        return cur.rowcount > 0

    def get_side_effect(self, effect_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM task_side_effects WHERE effect_id=?",
            (effect_id,)).fetchone()
        return dict(row) if row else None

    def list_side_effects_by_task(self, task_id: str,
                                  statuses: Optional[List[str]] = None
                                  ) -> List[Dict[str, Any]]:
        """按任务列副作用；statuses 给定时过滤，按创建时间正序。"""
        if statuses:
            q = ",".join("?" for _ in statuses)
            rows = self._conn.execute(
                "SELECT * FROM task_side_effects WHERE task_id=? AND"
                f" status IN ({q}) ORDER BY created_at",
                (task_id, *statuses)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM task_side_effects WHERE task_id=?"
                " ORDER BY created_at", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- approvals（A-01 五项绑定，F-04；建表见 core/schema/approvals.py）----
    def save_approval(self, approval_id: str, *, task_id: str, op: str,
                      target: str, scope: str, action_summary: str,
                      status: str, granted_by: str,
                      expires_at: Optional[float]) -> str:
        with self._lock:
            guard_write(self)
            self._conn.execute(
                "INSERT INTO approvals (approval_id,task_id,op,target,scope,"
                "action_summary,status,granted_by,created_at,expires_at,"
                "consumed_at,revoked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (approval_id, task_id, op, target, scope, action_summary,
                 status, granted_by, time.time(), expires_at,
                 None, None))  # created_at, expires_at, consumed_at, revoked_at
            self._conn.commit()
        return approval_id

    def consume_approval(self, approval_id: str) -> bool:
        """一次性：把 approved 标记为 used，仅当仍为 approved。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                assert_connection_lease(self._conn, self)
                now = time.time()
                cur = self._conn.execute(
                    "UPDATE approvals SET status='used', consumed_at=?"
                    " WHERE approval_id=? AND status='approved'"
                    " AND (expires_at IS NULL OR expires_at>?)", (now, approval_id, now))
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return cur.rowcount > 0

    def authorize_bound_action(self, approval_id: str, task_id: str,
                               op: str, scope: str, target: str,
                               digest: str, binding_json: str,
                               run_id: str, ttl_seconds: float):
        """Reuse pending requests or consume one exact approved action atomically."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                assert_connection_lease(self._conn, self)
                now = time.time()
                row = self._conn.execute(
                    "SELECT * FROM approvals WHERE task_id=? AND op=? AND scope=?"
                    " AND action_digest=? AND binding_json=?"
                    " AND status IN ('pending','approved','denied')"
                    " AND (expires_at IS NULL OR expires_at>?)"
                    " ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (task_id, op, scope, digest, binding_json, now)).fetchone()
                if row:
                    identity, status = row["approval_id"], row["status"]
                    if status == "approved":
                        self._conn.execute(
                            "UPDATE approvals SET status='used',consumed_at=?"
                            " WHERE approval_id=?", (now, identity))
                        status = "used"
                else:
                    identity, status = approval_id, "pending"
                    self._conn.execute(
                        "INSERT INTO approvals (approval_id,task_id,op,target,scope,"
                        "action_summary,status,granted_by,created_at,expires_at,"
                        "action_digest,binding_json,requested_run_id)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (identity, task_id, op, target[:300], scope,
                         (op + " " + binding_json)[:500], status, "", now,
                         now + ttl_seconds, digest, binding_json, run_id))
                self._conn.commit()
                return identity, status
            except BaseException:
                self._conn.rollback()
                raise

    def decide_task_approval(self, approval_id: str, approved: bool, actor: str):
        """Decide only a request belonging to the task's current suspended boundary."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT * FROM approvals WHERE approval_id=?",
                                         (approval_id,)).fetchone()
                if row is None:
                    raise KeyError("审批不存在")
                task_row = self._conn.execute("SELECT data FROM tasks WHERE task_id=?",
                                               (row["task_id"],)).fetchone()
                if not task_row:
                    raise ValueError("审批没有可恢复的任务")
                task = Task.from_dict(json.loads(task_row["data"]))
                if row["status"] in ("pending", "approved") and row["expires_at"] is not None and row["expires_at"] <= time.time():
                    raise ValueError("审批已过期，请恢复任务以生成新请求")
                if (approved and row["status"] in ("approved", "used")) or (
                        not approved and row["status"] == "denied"):
                    ready = (row["status"] == "approved" and
                        task.task_status == TaskStatus.SUSPENDED and
                        approval_id in task.waiting_approvals and
                        self._task_approvals_ready(task.waiting_approvals))
                    self._conn.commit()
                    return task, False, ready
                if row["status"] != "pending":
                    raise ValueError("审批已经决定或失效")
                if task.task_status != TaskStatus.SUSPENDED or approval_id not in task.waiting_approvals:
                    raise ValueError("任务未在此审批边界等待")
                self._conn.execute("UPDATE approvals SET status=?,granted_by=? WHERE approval_id=?",
                    ("approved" if approved else "denied", actor, approval_id))
                ready = False
                if not approved:
                    from .state_machine import transition
                    task.task_status = transition(task.task_status, TaskStatus.CANCELLED)
                    task.result = "用户拒绝审批，任务已取消"
                    task.waiting_approvals = []
                    task.updated_at = time.time()
                    self._conn.execute("UPDATE tasks SET status=?,data=? WHERE task_id=?",
                        (task.task_status.value, json.dumps(task.to_dict(), ensure_ascii=False), task.task_id))
                    self._conn.execute(
                        "UPDATE approvals SET status='revoked',revoked_at=? WHERE task_id=?"
                        " AND status IN ('pending','approved')", (time.time(), task.task_id))
                else:
                    ready = self._task_approvals_ready(task.waiting_approvals)
                self._conn.commit()
                return task, True, ready
            except BaseException:
                self._conn.rollback()
                raise

    def _task_approvals_ready(self, identities):
        states = [self._conn.execute(
            "SELECT status,expires_at FROM approvals WHERE approval_id=?", (identity,)).fetchone()
            for identity in identities]
        return bool(states) and all(row and row["status"] == "approved" and
            (row["expires_at"] is None or row["expires_at"] > time.time()) for row in states)

    def revoke_approval(self, approval_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status='revoked', revoked_at=?"
                " WHERE approval_id=? AND status='approved'",
                (time.time(), approval_id))
            self._conn.commit()
        return cur.rowcount > 0

    def revoke_task_approvals(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE approvals SET status='revoked',revoked_at=?"
                " WHERE task_id=? AND status IN ('pending','approved')",
                (time.time(), task_id))
            self._conn.commit()

    def expire_approvals(self, now: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status='expired'"
                " WHERE status IN ('pending','approved') AND expires_at IS NOT NULL"
                " AND expires_at <= ?", (now,))
            self._conn.commit()
        return cur.rowcount

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?",
            (approval_id,)).fetchone()
        return dict(row) if row else None

    def list_approvals_by_task(self, task_id: str, limit: int = 100
                               ) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE task_id=?"
            " ORDER BY created_at DESC LIMIT ?", (task_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def list_approvals_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

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
            encoded = json.dumps(data, ensure_ascii=False, default=str,
                                 sort_keys=True)
            guard_write(self)
            existing = self._conn.execute(
                "SELECT data FROM runtime_manifests WHERE manifest_id=?",
                (manifest_id,)).fetchone()
            if existing:
                if json.loads(existing["data"]) != data:
                    self._conn.rollback()
                    raise ValueError("不可变运行清单禁止覆盖")
                self._conn.commit()
                return
            self._conn.execute(
                "INSERT INTO runtime_manifests VALUES"
                " (?,?,?,?,?,?,?)",
                (manifest_id, task_id, run_id, device_id, installation_id,
                 time.time() if created_at is None else created_at,
                 encoded))
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
            guard_write(self)
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO tool_executions VALUES (?,?,?,?)",
                (idem_key, tool_id, time.time(),
                 json.dumps(result, ensure_ascii=False, default=str)))
            self._conn.commit()
            return cur.rowcount > 0

    # ---- budget ledger ----
    def save_execution_budget(self, task_id, run_id, state):
        with self._lock:
            guard_write(self)
            self._conn.execute("INSERT OR REPLACE INTO execution_budget_state VALUES (?,?,?)",
                               (task_id, run_id, json.dumps(state, ensure_ascii=False)))
            self._conn.commit()

    def get_execution_budget(self, task_id):
        row = self._conn.execute("SELECT state FROM execution_budget_state WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row["state"]) if row else None

    def record_budget(self, ledger_key: str, model_calls: int = 0,
                      tool_calls: int = 0, cost: float = 0.0,
                      seconds: float = 0.0) -> None:
        with self._lock:
            guard_write(self)
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
    def save_routing_report(self, dataset_id: str, version: str,
                             report: Dict[str, Any]) -> None:
        """落库路由评测报告（F-53 / R-03）。

        独立表 `routing_reports`（core/schema/routing.py）——
        路由评测关注分类决策质量，与任务级评测报告语义不同，混表会让基线对比失真。
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO routing_reports"
                " (dataset_id, version, created_at, report_json)"
                " VALUES (?,?,?,?)",
                (dataset_id, version, time.time(),
                 json.dumps(report, ensure_ascii=False, default=str)))
            self._conn.commit()

    def list_routing_reports(self, limit: int = 20
                             ) -> List[Dict[str, Any]]:
        """最近 N 条路由评测报告（按时间倒序）。"""
        rows = self._conn.execute(
            "SELECT report_json FROM routing_reports"
            " ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                out.append(json.loads(r["report_json"]))
            except (TypeError, ValueError):
                continue
        return out

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
