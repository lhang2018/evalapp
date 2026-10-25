"""持久问题队列（ALN-09-H：真实候选宿主、持久问题队列与进程隔离）。

问题票据在 ALN-09-F 之前只存在 ``EvolutionController.tickets`` 这个内存
deque 里：进程退出即丢，多进程之间也无法安全共享。本模块把票据落到
SQLite（``evolution_problem_queue``，见 ``core/schema/evolution.py``），
并用「租约 + fencing token」实现跨进程领取与崩溃恢复：

- **入队幂等**：``ticket_id`` 是主键，同一票据重复登记不会产生第二行；
  已消费的票据也不会被重新激活（证据只登记一次）。
- **领取原子**：单条 ``UPDATE ... WHERE ticket_id=(子查询)`` 在 SQLite
  写事务内完成；两个进程并发领取时只有一个能改到行，另一个得到 None。
- **崩溃恢复**：租约过期后其他进程可 ``recover_stale`` 接管（``attempts``
  递增使旧 token 失效）。票据本身永不丢，也不猜测未完成工作的副作用。
- **fencing**：``complete``/``release`` 都校验当前 lease token；旧持有者
  （租约已被接管）写入会被 ``LeaseLost`` 拒绝，僵尸进程无法覆盖新持有者的
  候选身份。
- **候选身份持久**：完成时把 ``candidate_id`` 写回同一行，重启/重开后仍可
  解析（验收：崩溃后队列和候选身份可恢复）。

纯内存行为保持兼容：``EvolutionController`` 在没有 Database 时仍然只用
deque，不构造本队列。
"""

import dataclasses
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from ..core.leases import LeaseLost

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_CONSUMED = "consumed"

_RETRYABLE = (sqlite3.OperationalError,)


@dataclass
class QueueClaim:
    """一次成功领取：token 是 fencing 凭据，被接管后旧 token 立即失效。"""

    ticket_id: str
    owner_id: str
    token: str
    attempts: int
    expires_at: float
    ticket: Any


class ProblemQueue:
    """问题票据的持久队列；一个实例代表一个工作进程的领取身份。"""

    def __init__(self, db: Any, ttl: float = 15.0,
                 clock=None, owner_id: Optional[str] = None):
        conn = getattr(db, "_conn", None)
        if conn is None:
            raise ValueError("ProblemQueue 需要带连接的 Database")
        self.db = db
        self.conn = conn
        self.ttl = float(ttl)
        self.clock = clock or time.time
        self.owner_id = owner_id or ("queue_" + uuid.uuid4().hex)

    # ---- 序列化（ProblemTicket 定义在 controller，惰性导入避免循环）----
    @staticmethod
    def _encode(ticket: Any) -> str:
        return json.dumps(dataclasses.asdict(ticket), ensure_ascii=False,
                          sort_keys=True, default=str)

    @staticmethod
    def _decode(text: str) -> Any:
        from .controller import ProblemTicket

        data = json.loads(text)
        fields = {f.name for f in dataclasses.fields(ProblemTicket)}
        return ProblemTicket(**{k: v for k, v in data.items() if k in fields})

    # ---- 事务辅助 ----
    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """写事务：调用方若已持有事务则不重复 BEGIN（避免嵌套报错）。"""
        with self.db._lock:
            started = not self.conn.in_transaction
            if started:
                self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                if started and self.conn.in_transaction:
                    self.conn.rollback()
                raise
            else:
                if started and self.conn.in_transaction:
                    self.conn.commit()

    # ---- 入队 ----
    def enqueue(self, ticket: Any) -> bool:
        """登记票据；重复 ticket_id 返回 False（幂等，不覆盖已有证据）。"""
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO evolution_problem_queue"
                " (ticket_id, status, detected_at, attempts, candidate_id, data)"
                " VALUES (?,?,?,0,?,?)",
                (ticket.ticket_id, STATUS_PENDING,
                 float(ticket.detected_at or self.clock()),
                 ticket.candidate_id, self._encode(ticket)))
            return cursor.rowcount > 0

    # ---- 领取 ----
    def claim(self, ticket_id: Optional[str] = None, *, ttl: Optional[float] = None,
              owner_id: Optional[str] = None) -> Optional[QueueClaim]:
        """领取一条票据（默认取最早的可领票据）；无可领返回 None。

        可领 = pending，或租约已过期的 claimed（崩溃接管）。
        """
        now = self.clock()
        ttl = self.ttl if ttl is None else float(ttl)
        token = uuid.uuid4().hex
        owner = owner_id or self.owner_id
        head = ("UPDATE evolution_problem_queue SET status=?, lease_owner=?,"
                " lease_token=?, lease_expires_at=?, attempts=attempts+1"
                " WHERE ")
        if ticket_id is None:
            sql = head + (
                "ticket_id=(SELECT ticket_id FROM evolution_problem_queue"
                " WHERE status=? OR (status=? AND lease_expires_at<=?)"
                " ORDER BY detected_at ASC, ticket_id ASC LIMIT 1)")
            params = (STATUS_CLAIMED, owner, token, now + ttl,
                      STATUS_PENDING, STATUS_CLAIMED, now)
        else:
            sql = head + (
                "ticket_id=? AND (status=? OR (status=? AND lease_expires_at<=?))")
            params = (STATUS_CLAIMED, owner, token, now + ttl,
                      ticket_id, STATUS_PENDING, STATUS_CLAIMED, now)
        with self._write() as conn:
            cursor = conn.execute(sql, params)
            if cursor.rowcount <= 0:
                return None
            row = conn.execute(
                "SELECT * FROM evolution_problem_queue WHERE lease_token=?",
                (token,)).fetchone()
        if row is None:  # 目标行在同一事务内被他人改写（防御）
            return None
        return QueueClaim(ticket_id=row["ticket_id"], owner_id=owner, token=token,
                          attempts=int(row["attempts"]),
                          expires_at=float(row["lease_expires_at"]),
                          ticket=self._decode(row["data"]))

    def renew(self, claim: QueueClaim, ttl: Optional[float] = None) -> bool:
        """续租；租约已被接管或票据已完成返回 False（调用方应停止写入）。"""
        now = self.clock()
        ttl = self.ttl if ttl is None else float(ttl)
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE evolution_problem_queue SET lease_expires_at=?"
                " WHERE ticket_id=? AND lease_token=? AND status=?"
                " AND lease_expires_at>?",
                (now + ttl, claim.ticket_id, claim.token, STATUS_CLAIMED, now))
            ok = cursor.rowcount > 0
        if ok:
            claim.expires_at = now + ttl
        return ok

    # ---- 完成 / 释放 ----
    def complete(self, claim: QueueClaim, *,
                 candidate_id: Optional[str] = None) -> Any:
        """把票据标记为已消费并回填候选身份；token 失效时抛 LeaseLost。"""
        ticket = claim.ticket
        ticket.consumed = True
        ticket.candidate_id = candidate_id or ticket.candidate_id
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE evolution_problem_queue SET status=?, lease_owner=NULL,"
                " lease_token=NULL, lease_expires_at=NULL, candidate_id=?, data=?"
                " WHERE ticket_id=? AND lease_token=? AND status=?",
                (STATUS_CONSUMED, ticket.candidate_id, self._encode(ticket),
                 claim.ticket_id, claim.token, STATUS_CLAIMED))
            if cursor.rowcount <= 0:
                raise LeaseLost("问题票据租约已失效，禁止旧持有者完成")
        return ticket

    def release(self, claim: QueueClaim, *, reason: str = "") -> None:
        """失败时把票据放回队列（不消费证据）；token 失效时抛 LeaseLost。"""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE evolution_problem_queue SET status=?, lease_owner=NULL,"
                " lease_token=NULL, lease_expires_at=NULL"
                " WHERE ticket_id=? AND lease_token=? AND status=?",
                (STATUS_PENDING, claim.ticket_id, claim.token, STATUS_CLAIMED))
            if cursor.rowcount <= 0:
                raise LeaseLost("问题票据租约已失效，禁止旧持有者释放")

    # ---- 崩溃恢复与查询 ----
    def recover_stale(self, now: Optional[float] = None) -> int:
        """把过期租约退回 pending（不代表工作已失败，只表示可重新领取）。"""
        moment = self.clock() if now is None else float(now)
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE evolution_problem_queue SET status=?, lease_owner=NULL,"
                " lease_token=NULL, lease_expires_at=NULL"
                " WHERE status=? AND lease_expires_at<=?",
                (STATUS_PENDING, STATUS_CLAIMED, moment))
            return int(cursor.rowcount)

    def get(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM evolution_problem_queue WHERE ticket_id=?",
            (ticket_id,)).fetchone()
        return self._row(row) if row else None

    def pending(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM evolution_problem_queue WHERE status=?"
            " ORDER BY detected_at ASC, ticket_id ASC LIMIT ?",
            (STATUS_PENDING, int(limit))).fetchall()
        return [self._row(r) for r in rows]

    def stats(self, now: Optional[float] = None) -> Dict[str, int]:
        moment = self.clock() if now is None else float(now)
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM evolution_problem_queue"
            " GROUP BY status").fetchall()
        counts = {STATUS_PENDING: 0, STATUS_CLAIMED: 0, STATUS_CONSUMED: 0}
        for row in rows:
            counts[row["status"]] = int(row["n"])
        stale = self.conn.execute(
            "SELECT COUNT(*) AS n FROM evolution_problem_queue"
            " WHERE status=? AND lease_expires_at<=?",
            (STATUS_CLAIMED, moment)).fetchone()
        counts["stale_claimed"] = int(stale["n"]) if stale else 0
        counts["total"] = sum(counts[key] for key in
                              (STATUS_PENDING, STATUS_CLAIMED, STATUS_CONSUMED))
        return counts

    def _row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {"ticket_id": row["ticket_id"], "status": row["status"],
                "detected_at": row["detected_at"],
                "attempts": int(row["attempts"]),
                "candidate_id": row["candidate_id"],
                "lease_owner": row["lease_owner"],
                "lease_expires_at": row["lease_expires_at"],
                "ticket": self._decode(row["data"])}
