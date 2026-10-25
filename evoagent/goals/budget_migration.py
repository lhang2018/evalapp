"""旧 Goal 历史用量的聚合预算迁移（ALN-15-P）。

ALN-15-R 的运行时接线在绑定前用 ``_legacy_goal_history`` 拒绝存在历史
支出的未迁移 Goal 家族（"existing Goal spending requires explicit
aggregate migration"），但没有提供迁移工具——本模块补上它：

- **幂等迁移**（migrate）：把家族内所有未绑定 runtime 账户的旧任务
  ``execution_budget_state`` 用量记入对应 Goal 权威账户（15-R 的
  ``identity("goal", goal_id)`` 命名空间；预留沿账户父子链逐级扣减）；
  每个任务写入一条 binding 标记行，使 ``_legacy_goal_history`` 的
  ``b.task_id IS NULL`` 门放行、且重复绑定不重放历史（标记**不含
  legacy_usage**，防 ``import_legacy`` 二次计费）。
- **重放安全**：预留幂等键 ``"goal-migration:v1|{goal_id}|{task_id}"``
  在账户上唯一，migrate 重跑不重复计费（重放返回已有预留）。
- **可回退**（rollback）：仅对**未下发**的迁移预留退款；账户、账本
  事件与 binding 标记保留（账户不可变，真实历史用量不撤销）。
- **余额一致**（verify）：已知成本任务的用量精确进入 Goal 账户
  spent；未知成本任务以 unknown 挂账（持续占用上界，绝不假定零费），
  在 verify 里单独汇报待对账。
"""

import json

from ..core.budget import BudgetExceeded
from ..core.reservations import ReservationConflict
from ..core.runtime_budget import identity, limits, micros

_MIGRATION_KIND = "goal-migration:v1"


def _usage_amount(usage):
    """旧执行预算 state 中的 usage dict → (amount, unknown)。"""
    usage = usage or {}
    unknown = bool(usage.get("unknown_model_costs") or usage.get("unknown_tool_costs"))
    amount = {"cost_micros": micros(usage.get("cost", 0.0) or 0.0),
              "model_calls": int(usage.get("model_calls", 0) or 0),
              "tool_calls": int(usage.get("tool_calls", 0) or 0)}
    return amount, unknown


class GoalBudgetMigration:
    """旧 Goal → 权威总账的迁移工具；只记账，不授予动作权限。"""

    AUDIT = {"actor": "budget:goal-migration",
             "reason": "legacy goal aggregate migration"}

    def __init__(self, ledger, *, clock=None):
        if ledger.db is None:
            raise ValueError("goal migration requires a Database-backed BudgetLedger")
        self.ledger, self.db = ledger, ledger.db
        self.store = ledger.reservation_store(clock=clock)

    # ---- 家族与账户 ----

    def family(self, goal_id):
        """返回 goal_id 及其全部后代（先根后叶，含孙层）。"""
        if not isinstance(goal_id, str) or not goal_id:
            raise ValueError("goal_id required")
        with self.db._lock:
            rows = self.db._conn.execute(
                "WITH RECURSIVE family(goal_id, depth) AS ("
                "  SELECT goal_id, 0 FROM goals WHERE goal_id=?"
                "  UNION SELECT g.goal_id, f.depth+1 FROM goals g"
                "    JOIN family f ON g.parent_goal_id=f.goal_id)"
                "SELECT goal_id, depth FROM family ORDER BY depth, goal_id",
                (goal_id,)).fetchall()
        return [{"goal_id": row[0], "depth": row[1]} for row in rows]

    def _chain(self, goal_id):
        """goal_id 向根的授权链（返回为根→叶序）。"""
        result = []
        current = goal_id
        seen = set()
        while current and current not in seen:
            seen.add(current)
            with self.db._lock:
                row = self.db._conn.execute("SELECT * FROM goals WHERE goal_id=?",
                                            (current,)).fetchone()
                if row is None:
                    raise PermissionError("unknown goal: " + current)
                grant_row = self.db._conn.execute(
                    "SELECT data FROM goal_grants WHERE goal_id=? "
                    "AND revoked_at IS NULL ORDER BY rowid DESC LIMIT 1",
                    (current,)).fetchone()
            if grant_row is None:
                raise PermissionError("goal is not authorized: " + current)
            result.append((current, json.loads(grant_row[0])))
            current = row["parent_goal_id"]
        return list(reversed(result))  # 根→叶

    def ensure_goal_accounts(self, goal_id):
        """按 bind() 语义逐级建立 goal 权威账户（根→叶）；返回账户名列表。"""
        parent = None
        names = []
        for gid, grant in self._chain(goal_id):
            cap = limits(grant["budget_policy"])
            if parent:
                outer = self.store.get_account(parent)["limits"]
                cap = {k: min(cap[k], outer[k]) for k in cap}
            name = identity("goal", gid)
            try:
                account = self.store.get_account(name)
            except KeyError:
                try:
                    account = self.store.create_account(
                        name, cap, kind="goal", reference=gid,
                        currency="USD", parent_id=parent, **self.AUDIT)
                except ValueError:
                    account = self.store.get_account(name)
            if (account["kind"] != "goal" or account["reference"] != gid
                    or account["currency"] != "USD"
                    or account["parent_id"] != parent):
                raise ValueError("goal account identity is immutable: " + name)
            if {k: min(cap[k], account["limits"][k]) for k in cap} \
                    != account["limits"]:
                raise ValueError(
                    "goal account limits do not match grant policy: " + name)
            names.append(name)
            parent = name
        return names

    def _leaf_account(self, goal_id):
        names = self.ensure_goal_accounts(goal_id)
        return names[-1] if names else identity("goal", goal_id)

    # ---- 迁移 ----

    def _legacy_tasks(self, goal_id):
        """家族内未绑定 runtime 账户、带执行预算状态的旧任务。"""
        with self.db._lock:
            rows = self.db._conn.execute(
                "WITH RECURSIVE family(goal_id) AS ("
                "  SELECT ? UNION SELECT g.goal_id FROM goals g"
                "    JOIN family f ON g.parent_goal_id=f.goal_id)"
                "SELECT t.task_id, t.goal_id, e.state FROM tasks t"
                " JOIN execution_budget_state e ON e.task_id=t.task_id"
                " LEFT JOIN budget_runtime_bindings b ON b.task_id=t.task_id"
                " WHERE t.goal_id IN (SELECT goal_id FROM family)"
                "   AND b.task_id IS NULL", (goal_id,)).fetchall()
        return [{"task_id": r[0], "goal_id": r[1], "state": r[2]} for r in rows]

    def migrate(self, goal_id, *, actor=None, reason=None):
        """幂等迁移整个 Goal 家族；返回 receipt。"""
        audit = dict(self.AUDIT)
        if actor:
            audit["actor"] = str(actor)
        if reason:
            audit["reason"] = str(reason)
        receipt = {"goal_id": goal_id, "migrated": [], "skipped": [],
                   "reconciliation_required": []}
        # Known amounts first: a pure-unknown task holds the remaining upper
        # bound, which must not block the family's known historical charges.
        tasks = sorted(
            self._legacy_tasks(goal_id),
            key=lambda task: _usage_amount(
                json.loads(task["state"] or "{}").get("usage"))[1])
        for task in tasks:
            amount, unknown = _usage_amount(
                json.loads(task["state"] or "{}").get("usage"))
            if not any(amount.values()) and not unknown:
                self._write_binding(task["task_id"], task["goal_id"])
                receipt["skipped"].append(task["task_id"])
                continue
            leaf = self._leaf_account(task["goal_id"])
            hold = dict(amount)
            if unknown and not hold["cost_micros"]:
                remaining = self.store.balance(leaf)["available"]["cost_micros"]
                if remaining <= 0:
                    # No conservative bound left: never assume the old charge
                    # was free, and never silently drop it.
                    receipt["reconciliation_required"].append(task["task_id"])
                    continue
                hold["cost_micros"] = remaining
            key = f"{_MIGRATION_KIND}|{task['goal_id']}|{task['task_id']}"
            reservation = self.store.reserve(
                leaf, key, hold, ttl_seconds=1,
                known_free=not bool(hold["cost_micros"]), context={}, **audit)
            if reservation["state"] == "refunded":
                raise BudgetExceeded(
                    "migration", "historical use cannot be refunded as unused",
                    scope=leaf)
            if reservation["state"] not in {"settled", "unknown"}:
                if not reservation["dispatched"]:
                    self.store.mark_dispatched(
                        reservation["reservation_id"], **audit)
                if unknown:
                    reservation = self.store.mark_unknown(
                        reservation["reservation_id"],
                        evidence_ref=f"goal-migration:unknown-cost|{task['task_id']}",
                        **audit)
                else:
                    reservation = self.store.settle(
                        reservation["reservation_id"], amount,
                        evidence_ref=f"execution_budget:{task['task_id']}",
                        **audit)
            self._write_binding(task["task_id"], task["goal_id"])
            receipt["migrated"].append(
                {"task_id": task["task_id"], "goal_id": task["goal_id"],
                 "reservation_id": reservation["reservation_id"],
                 "state": reservation["state"]})
        return receipt

    def _write_binding(self, task_id, goal_id):
        """写入幂等 binding 标记（不含 legacy_usage，防 import_legacy 重算）。

        account_id 与 goal_ids 均以**任务直属 goal** 的账户/链为准。
        """
        chain = [gid for gid, _ in reversed(self._chain(goal_id))]  # 根→叶
        data = json.dumps(
            {"schema_version": 1,
             "account_id": identity("goal", goal_id),
             "additional_accounts": [],
             "goal_ids": chain,
             "task_id": task_id,
             "legacy_usage": None},
            sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT data FROM budget_runtime_bindings WHERE task_id=?",
                (task_id,)).fetchone()
            if row is None:
                self.db._conn.execute(
                    "INSERT INTO budget_runtime_bindings VALUES (?,?)",
                    (task_id, data))
                self.db._conn.commit()
                return
        existing = json.loads(row[0])
        if existing.get("legacy_usage"):
            raise ReservationConflict(
                "migration binding must not carry legacy_usage (double billing)")
        if existing.get("account_id") != identity("goal", goal_id):
            raise ReservationConflict(
                "task is already bound to a different account")

    # ---- 验证与回退 ----

    def verify(self, goal_id):
        """已知成本迁移后的 Goal 账户 spent 应精确等于家族内旧用量总和；未知成本任务单独汇报。"""
        total = {"cost_micros": 0, "model_calls": 0, "tool_calls": 0}
        unknown_tasks = []
        for task in self._legacy_tasks(goal_id) + self._bound_tasks(goal_id):
            amount, unknown = _usage_amount(
                json.loads(task["state"] or "{}").get("usage"))
            if unknown:
                unknown_tasks.append(task["task_id"])
            else:
                for key, value in amount.items():
                    total[key] += value
        root = identity("goal", goal_id)
        try:
            balance = self.store.balance(root)
        except KeyError:
            return {"goal_id": goal_id, "consistent": False,
                    "reason": "root account missing", "expected_spent": total,
                    "unknown_tasks": unknown_tasks}
        spent = {k: balance["spent"][k] for k in total}
        return {"goal_id": goal_id, "consistent": spent == total,
                "expected_spent": total, "spent": spent,
                "unknown_tasks": unknown_tasks}

    def _bound_tasks(self, goal_id):
        """家族内已带迁移标记（Goal 账户）的任务；运行时原绑定不参与比对。"""
        with self.db._lock:
            rows = self.db._conn.execute(
                "WITH RECURSIVE family(goal_id) AS ("
                "  SELECT ? UNION SELECT g.goal_id FROM goals g"
                "    JOIN family f ON g.parent_goal_id=f.goal_id)"
                "SELECT t.task_id, t.goal_id, e.state, b.data FROM tasks t"
                " JOIN execution_budget_state e ON e.task_id=t.task_id"
                " JOIN budget_runtime_bindings b ON b.task_id=t.task_id"
                " WHERE t.goal_id IN (SELECT goal_id FROM family)",
                (goal_id,)).fetchall()
        tasks = []
        for row in rows:
            data = json.loads(row[3])
            if str(data.get("account_id", "")).startswith("goal:"):
                tasks.append({"task_id": row[0], "goal_id": row[1], "state": row[2]})
        return tasks

    def rollback(self, goal_id):
        """回退未下发的迁移预留；已下发（结算/未知）不撤销。"""
        family_ids = [g["goal_id"] for g in self.family(goal_id)]
        reverted, kept = [], []
        for gid in family_ids:
            name = identity("goal", gid)
            try:
                self.store.get_account(name)
            except KeyError:
                continue
            with self.db._lock:
                rows = self.db._conn.execute(
                    "SELECT reservation_id FROM budget_reservations "
                    "WHERE account_id=? AND idem_key LIKE ?",
                    (name, _MIGRATION_KIND + "|%")).fetchall()
            for (reservation_id,) in rows:
                record = self.store.get(reservation_id)
                if record["state"] == "reserved" and not record["dispatched"]:
                    try:
                        self.store.refund(reservation_id, **self.AUDIT)
                        reverted.append(reservation_id)
                    except ReservationConflict:
                        kept.append(reservation_id)
                else:
                    kept.append(reservation_id)
        return {"goal_id": goal_id, "reverted": reverted, "kept": kept}
