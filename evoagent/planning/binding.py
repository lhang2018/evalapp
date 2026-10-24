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
from typing import Any, Dict, List, Optional

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
