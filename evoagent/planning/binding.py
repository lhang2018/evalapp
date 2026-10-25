"""ALN-07-B：运行时动作绑定（effect ↔ step）。

闭合 ALN-07-E 与 ALN-07-P 之间的断点：对账已经能接收
``step_bindings={effect_id: step_id}``，但运行时从来没有人产生这份绑定，
于是「同一工具被多个步骤声明」时只能永远判 ``ambiguous``。

本模块只做一件事：**记录「这次副作用是哪一步触发的」这条证据。**

边界（刻意收窄）
----------------
* **不是授权**：绑定存在不代表动作被允许；本模块不参与任何放行/拒绝判定，
  不改变工具可见性。
* **不改账本**：不 transition / reconcile / record 副作用，不释放 pending。
* **不改计划**：不写 ``completed_work``、不改步骤状态。
* **不猜**：只记录调用方明确给出的一对 ``(effect_id, step_id)``；本模块
  不解析幂等键、不按工具名反推归属。

一致性约束
----------
* 同一 ``(effect_id, step_id)`` 重复绑定 → 幂等，不产生第二行；
* 同一 ``effect_id`` 绑到**不同** ``step_id`` → 拒绝（:class:`BindingConflict`），
  且原绑定不被覆盖：归属一旦确立就不允许被后写改写；
* 同一 ``step_id`` 可以有多个 ``effect_id``（一步多次调用工具是正常的）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

from ..core.leases import guard_write


class BindingError(ValueError):
    """绑定输入不合法。"""


class BindingConflict(BindingError):
    """同一副作用被试图绑定到不同步骤（归属冲突，拒绝覆盖）。"""


#: 处理 WHERE 分支时 Cursor.description 位置：字段顺序与 SELECT 保持一致
_ROW_FIELDS = ("effect_id", "task_id", "step_id", "tool_id", "bound_at")


class ActionBindings:
    """副作用 → 计划步骤 的绑定账本（只记录、可只读导出）。

    参数
    ----
    db:
        ``evoagent.core.persistence.Database`` 实例。新表由
        ``core/schema/action_bindings.py`` 自动建（该包按域自动发现）。
    """

    def __init__(self, db: Any):
        if db is None:
            raise BindingError("绑定账本需要数据库连接")
        self.db = db

    # ---------------- 写入 ----------------

    def bind(self, effect_id: str, step_id: str, *,
             task_id: str = "", tool_id: Optional[str] = None,
             now: Optional[float] = None) -> Dict[str, Any]:
        """把一次副作用绑定到触发它的步骤（幂等；冲突拒绝）。

        返回当前生效的绑定行。重复绑定同一对是**无操作**，不会刷新
        ``bound_at``——首次归属时间才是审计上有意义的时刻。
        """
        effect_id = _need(effect_id, "effect_id")
        step_id = _need(step_id, "step_id")
        task_id = _need(task_id, "task_id")
        if tool_id is not None:
            tool_id = _need(tool_id, "tool_id")
        bound_at = float(now if now is not None else time.time())

        with self.db._lock:
            try:
                guard_write(self.db)
                # guard_write opens the fenced transaction only under a lease.
                # Standalone callers still need a cross-connection write lock
                # before reading, so a concurrent insert cannot win the gap.
                if not self.db._conn.in_transaction:
                    self.db._conn.execute("BEGIN IMMEDIATE")
                row = self._select(effect_id)
                if row is not None:
                    if row["step_id"] != step_id or row["task_id"] != task_id:
                        raise BindingConflict(
                            f"副作用 {effect_id} 已归属于任务 {row['task_id']} "
                            f"步骤 {row['step_id']}，不能改写归属")
                    if tool_id is not None and row["tool_id"] != tool_id:
                        raise BindingConflict(f"副作用 {effect_id} 的工具归属冲突")
                else:
                    self.db._conn.execute(
                        "INSERT INTO action_step_bindings"
                        " (effect_id, task_id, step_id, tool_id, bound_at)"
                        " VALUES (?,?,?,?,?)",
                        (effect_id, task_id, step_id, tool_id, bound_at))
                    row = self._select(effect_id)
                self.db._conn.commit()
                return dict(row)
            except BaseException:
                self.db._conn.rollback()
                raise

    # ---------------- 只读 ----------------

    def get(self, effect_id: str) -> Optional[Dict[str, Any]]:
        with self.db._lock:
            row = self._select(effect_id)
        return dict(row) if row else None

    def for_task(self, task_id: str) -> Dict[str, str]:
        """导出 ``{effect_id: step_id}``，可直接作为 07-E 对账的
        ``step_bindings`` 入参。"""
        task_id = _need(task_id, "task_id")
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT effect_id, step_id FROM action_step_bindings"
                " WHERE task_id=? ORDER BY effect_id", (task_id,)).fetchall()
        return {r["effect_id"]: r["step_id"] for r in rows}

    def for_step(self, task_id: str, step_id: str) -> List[str]:
        """某步骤绑定的全部 effect_id（按绑定时间排序）。"""
        task_id = _need(task_id, "task_id")
        step_id = _need(step_id, "step_id")
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT effect_id FROM action_step_bindings"
                " WHERE task_id=? AND step_id=? ORDER BY bound_at, effect_id",
                (task_id, step_id)).fetchall()
        return [r["effect_id"] for r in rows]

    def count(self, task_id: str) -> int:
        task_id = _need(task_id, "task_id")
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT COUNT(*) AS n FROM action_step_bindings WHERE task_id=?",
                (task_id,)).fetchone()
        return int(row["n"] if row else 0)

    # ---------------- 预绑定意图（ALN-07-C：消崩溃半绑定窗口） ----------------

    def record_intent(self, task_id: str, step_id: str, tool_id: str, *,
                      run_id: Optional[str] = None,
                      now: Optional[float] = None) -> str:
        """执行前登记绑定意图（effect_id 未决）。

        必须发生在副作用工具调用之前：任何时刻崩溃后，意图流都能证明
        「该步骤当时正要产生该工具的副作用」，使补绑成为确定性匹配而
        不是猜测。intent_id 幂等：同键重复登记返回同一意图。
        """
        task_id = _need(task_id, "task_id")
        step_id = _need(step_id, "step_id")
        tool_id = _need(tool_id, "tool_id")
        created_at = float(now if now is not None else time.time())
        intent_id = f"{task_id}:{step_id}:{created_at}"
        with self.db._lock:
            try:
                if not self.db._conn.in_transaction:
                    self.db._conn.execute("BEGIN IMMEDIATE")
                self.db._conn.execute(
                    "INSERT OR IGNORE INTO action_binding_intents"
                    " (intent_id, task_id, step_id, tool_id, run_id,"
                    "  created_at, effect_id) VALUES (?,?,?,?,?,?,NULL)",
                    (intent_id, task_id, step_id, tool_id, run_id, created_at))
                self.db._conn.commit()
            except BaseException:
                self.db._conn.rollback()
                raise
        return intent_id

    def resolve_intent(self, intent_id: str, effect_id: str, *,
                       now: Optional[float] = None) -> Dict[str, Any]:
        """执行成功后回填 effect_id 并固化权威绑定（相邻原子写）。

        绑定仍走 bind() 的幂等/冲突语义；回填失败不撤销已发生的副作用。
        """
        intent_id = _need(intent_id, "intent_id")
        row = self.intent(intent_id)
        if row is None:
            raise BindingError(f"绑定意图不存在: {intent_id}")
        if row["effect_id"] not in (None, effect_id):
            raise BindingConflict(
                f"意图 {intent_id} 已回填 {row['effect_id']}，不能改写")
        bound = self.bind(effect_id, row["step_id"], task_id=row["task_id"],
                          tool_id=row["tool_id"], now=now)
        with self.db._lock:
            try:
                if not self.db._conn.in_transaction:
                    self.db._conn.execute("BEGIN IMMEDIATE")
                self.db._conn.execute(
                    "UPDATE action_binding_intents SET effect_id=?"
                    " WHERE intent_id=?", (effect_id, intent_id))
                self.db._conn.commit()
            except BaseException:
                self.db._conn.rollback()
                raise
        return bound

    def intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT intent_id, task_id, step_id, tool_id, run_id,"
                " created_at, effect_id FROM action_binding_intents"
                " WHERE intent_id=?", (intent_id,)).fetchone()
        return dict(row) if row else None

    def pending_intents(self, task_id: str) -> List[Dict[str, Any]]:
        """未回填（崩溃时未固化）的意图，按登记时间排序。"""
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT intent_id, task_id, step_id, tool_id, run_id,"
                " created_at FROM action_binding_intents"
                " WHERE task_id=? AND effect_id IS NULL"
                " ORDER BY created_at, intent_id", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def heal_pending(self, task_id: str,
                     ledger_rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """崩溃重启后对未决意图做确定性补绑（先对账后决定的一部分）。

        匹配规则（不猜）：账本行 tool_id/task_id 与意图一致、effect 尚无
        任何绑定，且该意图步骤没有其它已绑定 effect 时代入候选；
        唯一候选 → 补绑；零候选 → 副作用未发生，意图标记 stale；
        多候选 → ambiguous，不自动绑，留给对账报告人工裁决。
        """
        task_id = _need(task_id, "task_id")
        existing = self.for_task(task_id)
        by_step: Dict[str, List[str]] = {}
        for effect_id, step_id in existing.items():
            by_step.setdefault(step_id, []).append(effect_id)
        unbound = [r for r in ledger_rows
                   if r.get("task_id") == task_id
                   and r.get("effect_id") not in existing
                   and r.get("effect_id")]
        healed: Dict[str, str] = {}
        stale: List[str] = []
        ambiguous: List[Dict[str, Any]] = []
        for intent in self.pending_intents(task_id):
            candidates = [r for r in unbound
                          if r.get("tool_id") == intent["tool_id"]]
            if not candidates:
                stale.append(intent["intent_id"])
                continue
            if len(candidates) > 1 or by_step.get(intent["step_id"]):
                ambiguous.append({"intent_id": intent["intent_id"],
                                  "candidates": [c.get("effect_id")
                                                 for c in candidates]})
                continue
            row = candidates[0]
            self.resolve_intent(intent["intent_id"], row["effect_id"])
            healed[row["effect_id"]] = intent["step_id"]
        return {"healed": healed, "stale": stale, "ambiguous": ambiguous}

    # ---------------- 内部 ----------------

    def _select(self, effect_id: str):
        return self.db._conn.execute(
            "SELECT effect_id, task_id, step_id, tool_id, bound_at"
            " FROM action_step_bindings WHERE effect_id=?",
            (effect_id,)).fetchone()


def _need(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BindingError(f"{name} 必须是非空字符串")
    return value.strip()
