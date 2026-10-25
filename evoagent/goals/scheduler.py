"""长期目标周期推进与负载守护（ALN-13-P）。

13-A（Goal 十态 + 持久授权）与 14-A（无进展停止 + 机会排序）已交付
``GoalGovernor.advance/report/reconcile``；本模块只补两件尚未落地的能力，
不重新实现已接受切片：

- **周期**：为活跃目标维护持久 ``next_due_at``（`goal_schedules`），``tick()``
  只推进到期目标；``command_id`` 按 ``(目标, 时间槽)`` 派生，同一槽位重试不会
  重复派发（``advance`` 自带幂等表，崩溃后重放只返回既有结果）。
- **负载守护**：主任务（``origin=user/scheduled``）未完成时让出本轮，不让长期
  自主目标饿死用户任务；派生推进受 ``max_goals_per_tick`` 与
  ``max_steps_per_goal`` 限制。
- **停止与授权每轮重查**：tick 先 ``reconcile``（14-A 停止条件会把目标置为
  paused/review），再逐目标调用 ``advance``；授权撤销/过期/等级不足由
  ``GoalStore._grants`` 每轮拒绝，调度器记录原因并退避，不缓存旧授权。
- **多进程**：``goal_schedules`` 行内租约 + fencing token（``attempts`` 每次
  接管递增），崩溃后租约过期可接管，同一目标同一时刻只有一个推进者。

边界：本卡验证的是调度语义与恢复路径（本地运行时 + 可注入时钟/负载计数），
真实多机负载与生产调度环境仍属整包后续范围；`start()/stop()` 提供的后台线程
仅供接入，不在本卡写入边界内改动 App/CLI 装配。
"""

import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterator, List, Optional

from ..core.leases import LeaseLost
from .store import ACTIVE, TERMINAL, GoalStore, _json

SCHEDULED_PREFIX = "sched"

#: 计入「主任务负载」的非终态：正在占用运行时或即将占用。
_PRIMARY_ACTIVE = {"CREATED", "CLASSIFYING", "PLANNING", "READY", "RUNNING",
                   "RECOVERING", "VERIFYING", "WAITING_DEPENDENCY"}


@dataclass(frozen=True)
class SchedulerPolicy:
    """周期与守护参数（默认值保守：1 分钟一轮、单轮最多 3 个目标/各 1 步）。"""

    interval_seconds: float = 60.0
    max_goals_per_tick: int = 3
    max_steps_per_goal: int = 1
    #: 主任务未完成数 >= 该值就让出本轮（0 表示不让出，不建议）
    primary_busy_limit: int = 1
    lease_ttl_seconds: float = 30.0
    failure_backoff_seconds: float = 120.0
    max_backoff_seconds: float = 3600.0

    @classmethod
    def parse(cls, values: Optional[Dict[str, Any]]) -> "SchedulerPolicy":
        if values is None:
            return cls()
        if not isinstance(values, dict):
            raise ValueError("scheduler policy must be a mapping")
        if set(values) - set(cls.__dataclass_fields__):
            raise ValueError("unknown scheduler policy field")
        result = cls(**values)
        for key, value in asdict(result).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(key + " must be a number")
            if not value > 0:
                raise ValueError(key + " must be positive")
        for key in ("max_goals_per_tick", "max_steps_per_goal", "primary_busy_limit"):
            value = getattr(result, key)
            if type(value) is not int or not 1 <= value <= 1000:
                raise ValueError(key + " must be an integer in 1..1000")
        if result.max_steps_per_goal > 32:
            raise ValueError("max_steps_per_goal must be <= 32")
        return result


class GoalScheduler:
    """按周期推进活跃目标，并在主任务繁忙时让出。"""

    def __init__(self, governor: Any, *, db: Any = None, bus: Any = None,
                 clock=None, policy: Any = None, owner_id: Optional[str] = None):
        self.governor = governor
        self.db = db if db is not None else getattr(governor, "db", None)
        if self.db is None:
            raise ValueError("GoalScheduler 需要 Database")
        self.goals = getattr(governor, "goals", None) or GoalStore(
            self.db, clock=clock)
        self.bus = bus
        self.clock = clock or getattr(self.goals, "clock", None) or time.time
        self.policy = (policy if isinstance(policy, SchedulerPolicy)
                       else SchedulerPolicy.parse(policy))
        self.owner_id = owner_id or ("sched_" + uuid.uuid4().hex)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 事务与事件 ----
    @contextmanager
    def _write(self) -> Iterator[Any]:
        with self.db._lock:
            conn = self.db._conn
            started = not conn.in_transaction
            if started:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if started and conn.in_transaction:
                    conn.rollback()
                raise
            else:
                if started and conn.in_transaction:
                    conn.commit()

    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit(event_type=event_type, component_id="goal_scheduler",
                          payload=payload)
        except Exception:  # noqa: BLE001 —— 事件故障不影响调度主路径
            pass

    # ---- 调度状态 ----
    def sync(self, *, now: Optional[float] = None) -> int:
        """为尚未登记调度的目标补行（首个 tick 立即到期）。

        非终态目标都登记：paused/blocked/review 的目标之后可能恢复，
        登记后每轮只记录跳过原因，不做任何推进。
        """
        moment = self.clock() if now is None else float(now)
        goals = [g for g in self.goals.list(limit=1000)
                 if g["status"] not in TERMINAL]
        created = 0
        with self._write() as conn:
            for goal in goals:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO goal_schedules"
                    " (goal_id, next_due_at, interval_seconds, data)"
                    " VALUES (?,?,?,?)",
                    (goal["goal_id"], moment, self.policy.interval_seconds,
                     _json({"goal_id": goal["goal_id"]})))
                created += cursor.rowcount
        return created

    def due(self, *, now: Optional[float] = None, limit: int = 100
            ) -> List[Dict[str, Any]]:
        moment = self.clock() if now is None else float(now)
        rows = self.db._conn.execute(
            "SELECT * FROM goal_schedules WHERE next_due_at<=?"
            " ORDER BY next_due_at ASC, goal_id ASC LIMIT ?",
            (moment, int(limit))).fetchall()
        return [dict(row) for row in rows]

    def stats(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        moment = self.clock() if now is None else float(now)
        rows = self.db._conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN next_due_at<=? THEN 1 ELSE 0 END) AS due,"
            " SUM(CASE WHEN lease_token IS NOT NULL THEN 1 ELSE 0 END) AS leased"
            " FROM goal_schedules", (moment,)).fetchone()
        return {"total": int(rows["total"] or 0), "due": int(rows["due"] or 0),
                "leased": int(rows["leased"] or 0),
                "policy": asdict(self.policy),
                "next_due_at": self._next_due()}

    def _next_due(self) -> Optional[float]:
        row = self.db._conn.execute(
            "SELECT MIN(next_due_at) AS at FROM goal_schedules").fetchone()
        return row["at"] if row and row["at"] is not None else None

    # ---- 租约（fencing）----
    def _claim(self, goal_id: str, moment: float, ttl: Optional[float] = None
               ) -> Optional[Dict[str, Any]]:
        token = uuid.uuid4().hex
        ttl = self.policy.lease_ttl_seconds if ttl is None else float(ttl)
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE goal_schedules SET lease_owner=?, lease_token=?,"
                " lease_expires_at=?, attempts=attempts+1"
                " WHERE goal_id=? AND next_due_at<=?"
                " AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
                (self.owner_id, token, moment + ttl, goal_id, moment, moment))
            if cursor.rowcount <= 0:
                return None
            row = conn.execute(
                "SELECT attempts, next_due_at FROM goal_schedules WHERE goal_id=?",
                (goal_id,)).fetchone()
        return {"goal_id": goal_id, "token": token, "owner": self.owner_id,
                "attempts": int(row["attempts"]), "expires_at": moment + ttl}

    def _finish(self, claim: Dict[str, Any], moment: float,
                outcome: Dict[str, Any], ok: bool) -> None:
        """释放租约并排下一轮；失败按指数退避（fencing：token 不符则不写）。"""
        with self._write() as conn:
            row = conn.execute(
                "SELECT consecutive_failures FROM goal_schedules WHERE goal_id=?",
                (claim["goal_id"],)).fetchone()
            failures = 0 if ok else int(row["consecutive_failures"] or 0) + 1
            delay = self._delay(failures)
            cursor = conn.execute(
                "UPDATE goal_schedules SET next_due_at=?, last_run_at=?,"
                " last_outcome=?, consecutive_failures=?, lease_owner=NULL,"
                " lease_token=NULL, lease_expires_at=NULL"
                " WHERE goal_id=? AND lease_token=?",
                (moment + delay, moment,
                 _json({**outcome, "at": moment, "slot_interval":
                        self.policy.interval_seconds}),
                 failures, claim["goal_id"], claim["token"]))
        if cursor.rowcount <= 0:
            raise LeaseLost("目标调度租约已失效，禁止旧持有者写入")

    def _push_due(self, goal_id: str, moment: float, *, delay: Optional[float] = None
                  ) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE goal_schedules SET next_due_at=?, last_run_at=?"
                " WHERE goal_id=?",
                (moment + (delay if delay is not None
                           else self.policy.interval_seconds), moment, goal_id))

    def _delay(self, failures: int) -> float:
        if failures <= 0:
            return self.policy.interval_seconds
        backoff = self.policy.failure_backoff_seconds * (2 ** (failures - 1))
        return max(self.policy.interval_seconds,
                   min(backoff, self.policy.max_backoff_seconds))

    # ---- 主任务负载守护 ----
    def primary_load(self) -> Dict[str, Any]:
        """当前主任务 / 派生在途计数（供守护与排障读取）。"""
        tasks = self.db.list_tasks(limit=500)
        primary = [t for t in tasks
                   if t.origin in ("user", "scheduled") and not t.is_terminal()
                   and t.task_status.value in _PRIMARY_ACTIVE]
        derived = [t for t in tasks
                   if t.origin in ("derived", "opportunity") and not t.is_terminal()]
        return {"busy": len(primary),
                "primary_task_ids": [t.task_id for t in primary][:10],
                "derived_in_flight": len(derived),
                "limit": self.policy.primary_busy_limit}

    # ---- 单轮推进 ----
    def tick(self, *, now: Optional[float] = None,
             command_prefix: str = SCHEDULED_PREFIX) -> Dict[str, Any]:
        """执行一轮周期推进；返回本轮报告（可持久化/审计）。"""
        moment = self.clock() if now is None else float(now)
        slot = int(moment // self.policy.interval_seconds) if self.policy.interval_seconds else 0
        report: Dict[str, Any] = {
            "tick_id": f"tick_{slot}", "at": moment, "slot": slot,
            "advances": [], "skipped": [], "stopped": [], "errors": [],
            "load": {}, "reason": None, "ok": True,
        }
        self.goals.expire_due()  # 期限/授权过期会在本轮先落库（13-A 语义）
        load = self.primary_load()
        report["load"] = load
        if load["busy"] >= self.policy.primary_busy_limit:
            report["reason"] = "primary_load_guard"
            self._emit("goal_schedule_yielded",
                       {"reason": "primary_load_guard", **load, "at": moment})
            return report
        self.sync(now=moment)
        processed = 0
        for row in self.due(now=moment,
                            limit=self.policy.max_goals_per_tick * 4):
            if processed >= self.policy.max_goals_per_tick:
                break
            goal_id = row["goal_id"]
            try:
                goal = self.goals.get(goal_id)
            except KeyError:
                self._push_due(goal_id, moment)
                report["skipped"].append({"goal_id": goal_id, "reason": "goal_missing"})
                continue
            if goal["status"] not in ACTIVE:
                self._push_due(goal_id, moment)
                report["skipped"].append({"goal_id": goal_id,
                                          "reason": "inactive:" + goal["status"]})
                continue
            claim = self._claim(goal_id, moment)
            if claim is None:
                report["skipped"].append({"goal_id": goal_id,
                                          "reason": "leased_by_other"})
                continue
            processed += 1
            outcome, ok = self._advance_one(goal_id, moment, slot,
                                            command_prefix, report)
            try:
                self._finish(claim, moment, outcome, ok)
            except LeaseLost:
                self._emit("goal_schedule_lease_lost",
                           {"goal_id": goal_id, "at": moment})
            if not ok:
                report["ok"] = False
        self._emit("goal_schedule_tick",
                   {"tick_id": report["tick_id"], "at": moment,
                    "advanced": [item["goal_id"] for item in report["advances"]],
                    "skipped": len(report["skipped"]),
                    "stopped": len(report["stopped"]),
                    "errors": len(report["errors"]),
                    "load": load})
        return report

    def _advance_one(self, goal_id: str, moment: float, slot: int,
                     command_prefix: str, report: Dict[str, Any]):
        """对单个目标执行 reconcile + advance；返回 (outcome, ok)。"""
        try:
            reconciled = self.governor.reconcile(goal_id)
        except Exception as exc:  # noqa: BLE001 —— 单目标故障不影响其他目标
            report["errors"].append({"goal_id": goal_id,
                                     "error": f"{type(exc).__name__}: {exc}"})
            return {"status": "error", "reason": "reconcile_failed"}, False
        goal = self.goals.get(goal_id)
        if goal["status"] not in ACTIVE:
            reason = next((item.get("reason") for item in reconciled
                           if item.get("reason")), None)
            report["stopped"].append({"goal_id": goal_id,
                                      "reason": reason or ("inactive:" + goal["status"])})
            return {"status": "stopped", "reason": reason or "inactive",
                    "goal_status": goal["status"]}, True
        command_id = f"{command_prefix}:{goal_id}:{slot}"
        try:
            result = self.governor.advance(
                goal_id, command_id=command_id,
                max_steps=self.policy.max_steps_per_goal)
        except PermissionError as exc:  # 授权/停止条件（guard）每轮重查
            report["stopped"].append({"goal_id": goal_id, "reason": str(exc)})
            return {"status": "refused", "reason": str(exc)}, True
        except Exception as exc:  # noqa: BLE001 —— 冲突/状态变化按失败退避
            name = type(exc).__name__
            conflict = name == "GoalConflict"
            bucket = "skipped" if conflict else "errors"
            report[bucket].append({"goal_id": goal_id, "reason": str(exc)})
            return {"status": "conflict" if conflict else "error",
                    "reason": str(exc)}, conflict
        tasks = [item.get("task_id") for item in (result or {}).get("tasks", [])]
        status = "advanced" if tasks else "no_task"
        report["advances"].append({
            "goal_id": goal_id, "command_id": command_id, "tasks": tasks,
            "goal_status": ((result or {}).get("goal") or {}).get("status"),
        })
        return {"status": status, "reason": "", "tasks": tasks}, True

    # ---- 后台周期（接入用；不在本卡写入边界内改 App/CLI）----
    def start(self, *, poll_seconds: Optional[float] = None) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        interval = float(poll_seconds or self.policy.interval_seconds)

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 —— 周期任务不得静默退出
                    self._emit("goal_schedule_error",
                               {"error": f"{type(exc).__name__}: {exc}"})
                if self._stop.wait(max(1.0, interval)):
                    break

        self._thread = threading.Thread(target=loop, daemon=True,
                                        name="goal-scheduler")
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None
