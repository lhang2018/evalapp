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

import hashlib
import json
import math
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
        self._atomic_write = False

    # ---------------- 写入 ----------------

    def _begin_write(self):
        if self.db._conn.in_transaction:
            raise BindingError("绑定账本不能提交调用方在途事务")
        guard_write(self.db)
        if not self.db._conn.in_transaction:
            self.db._conn.execute("BEGIN IMMEDIATE")

    def _write_binding(self, effect_id, step_id, task_id, tool_id, bound_at):
        row = self._select(effect_id)
        if row is not None:
            if row["step_id"] != step_id or row["task_id"] != task_id:
                raise BindingConflict("副作用归属与原绑定冲突")
            if tool_id is not None and row["tool_id"] != tool_id:
                raise BindingConflict("副作用工具归属冲突")
        else:
            self.db._conn.execute(
                "INSERT INTO action_step_bindings"
                " (effect_id,task_id,step_id,tool_id,bound_at) VALUES (?,?,?,?,?)",
                (effect_id, task_id, step_id, tool_id, bound_at))
            row = self._select(effect_id)
        return dict(row)

    def bind(self, effect_id: str, step_id: str, *, task_id: str = "",
             tool_id: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
        effect_id, step_id, task_id = (_need(effect_id, "effect_id"),
            _need(step_id, "step_id"), _need(task_id, "task_id"))
        tool_id = _need(tool_id, "tool_id") if tool_id is not None else None
        with self.db._lock:
            if self._atomic_write:
                if not self.db._conn.in_transaction:
                    raise BindingError("原子绑定事务已丢失")
                return self._write_binding(effect_id, step_id, task_id, tool_id,
                    float(now if now is not None else time.time()))
            self._begin_write()
            try:
                bound = self._write_binding(effect_id, step_id, task_id, tool_id,
                    float(now if now is not None else time.time()))
                self.db._conn.commit()
                return bound
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
                      run_id: Optional[str] = None, now: Optional[float] = None) -> str:
        task_id, step_id, tool_id = (_need(task_id, "task_id"),
            _need(step_id, "step_id"), _need(tool_id, "tool_id"))
        run_id = _need(run_id, "run_id") if run_id is not None else None
        created_at = float(now if now is not None else time.time())
        if not math.isfinite(created_at):
            raise BindingError("意图时间必须有限")
        identity = json.dumps([task_id, step_id, tool_id, run_id, created_at],
                              ensure_ascii=False, separators=(",", ":"))
        intent_id = "intent_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        with self.db._lock:
            self._begin_write()
            try:
                self.db._conn.execute(
                    "INSERT INTO action_binding_intents"
                    " (intent_id,task_id,step_id,tool_id,run_id,created_at,effect_id)"
                    " VALUES (?,?,?,?,?,?,NULL) ON CONFLICT(intent_id) DO NOTHING",
                    (intent_id, task_id, step_id, tool_id, run_id, created_at))
                self.db._conn.commit()
            except BaseException:
                self.db._conn.rollback()
                raise
        return intent_id

    def _resolve_in_transaction(self, intent_id, effect_id, now):
        row = self.db._conn.execute(
            "SELECT * FROM action_binding_intents WHERE intent_id=?", (intent_id,)).fetchone()
        if row is None:
            raise BindingError("绑定意图不存在")
        if row["effect_id"] not in (None, effect_id):
            raise BindingConflict("意图已回填，不能改写副作用")
        # Keep the public persistence hook (including subclass fencing/fault
        # injection), while the base implementation joins this owned transaction.
        bound = self.bind(effect_id, row["step_id"], task_id=row["task_id"],
                          tool_id=row["tool_id"], now=now)
        self.db._conn.execute(
            "UPDATE action_binding_intents SET effect_id=? WHERE intent_id=?",
            (effect_id, intent_id))
        return bound

    def resolve_intent(self, intent_id: str, effect_id: str, *,
                       now: Optional[float] = None) -> Dict[str, Any]:
        intent_id, effect_id = _need(intent_id, "intent_id"), _need(effect_id, "effect_id")
        with self.db._lock:
            self._begin_write()
            self._atomic_write = True
            try:
                bound = self._resolve_in_transaction(intent_id, effect_id, now)
                self.db._conn.commit()
                return bound
            except BaseException:
                self.db._conn.rollback()
                raise
            finally:
                self._atomic_write = False

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
        """Only heal a one-to-one mapping; missing records are not execution proof."""
        task_id = _need(task_id, "task_id")
        rows = list(ledger_rows)
        with self.db._lock:
            self._begin_write()
            self._atomic_write = True
            try:
                existing = self.for_task(task_id)
                by_step = set(existing.values())
                unbound = [r for r in rows if r.get("task_id") == task_id
                           and r.get("effect_id") and r["effect_id"] not in existing]
                intents = self.pending_intents(task_id)
                def compatible(intent, row):
                    if row.get("tool_id") != intent["tool_id"]:
                        return False
                    if intent["run_id"] is not None and row.get("run_id") != intent["run_id"]:
                        return False
                    created = row.get("created_at")
                    if created is not None and (type(created) not in (int, float)
                            or not math.isfinite(created) or created < intent["created_at"]):
                        return False
                    return True
                matches = {i["intent_id"]: [r for r in unbound if compatible(i, r)]
                           for i in intents}
                healed, stale, ambiguous = {}, [], []
                for intent in intents:
                    candidates = matches[intent["intent_id"]]
                    if not candidates:
                        stale.append(intent["intent_id"])
                        continue
                    effect_id = candidates[0]["effect_id"]
                    competing = sum(any(r["effect_id"] == effect_id for r in found)
                                    for found in matches.values())
                    if (len(candidates) != 1 or competing != 1
                            or intent["step_id"] in by_step):
                        ambiguous.append({"intent_id": intent["intent_id"],
                            "candidates": [r["effect_id"] for r in candidates]})
                        continue
                    self._resolve_in_transaction(intent["intent_id"], effect_id, None)
                    healed[effect_id] = intent["step_id"]
                    by_step.add(intent["step_id"])
                self.db._conn.commit()
                return {"healed": healed, "stale": stale, "ambiguous": ambiguous}
            except BaseException:
                self.db._conn.rollback()
                raise
            finally:
                self._atomic_write = False

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
