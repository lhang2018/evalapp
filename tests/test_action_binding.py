"""ALN-07-B 运行时动作绑定：八条验收逐条覆盖。"""
from __future__ import annotations

import copy
import unittest

from evoagent.core.budget import BudgetController, BudgetLimits
from evoagent.core.permissions import PermissionController
from evoagent.core.persistence import Database
from evoagent.core.side_effects import SideEffectLedger
from evoagent.models.base import Action
from evoagent.models.mock import MockModel
from evoagent.planning.binding import ActionBindings, BindingConflict, BindingError
from evoagent.planning.planner import ExecutionPlan, PlanStep
from evoagent.planning.reconcile import ReconciliationError, reconcile_plan
from evoagent.react.engine import ReactEngine
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.tools.registry import ToolRegistry


def _db(tmp=None):
    import tempfile
    import os
    path = os.path.join(tmp or tempfile.mkdtemp(prefix="aln07b-"), "b.db")
    return Database(path)


def _bindings():
    db = _db()
    return ActionBindings(db), db


def _plan(*steps):
    return ExecutionPlan("p", "g", list(steps),
                         acceptance_criteria=[{"type": "tool_succeeded"}])


def _step(sid, tool=None, deps=(), state="pending"):
    return PlanStep(sid, sid, deps=list(deps), tool_hint=tool, state=state)


def _ok():
    return ToolResult(True, submitted=True, executed=True, contract_valid=True)


class ScriptModel(MockModel):
    def __init__(self, actions):
        super().__init__()
        self.actions = list(actions)

    def suggest_action(self, messages, tools):
        return self.actions.pop(0) if self.actions else Action("finish",
                                                               answer="done")


class TestIdempotency(unittest.TestCase):
    """验收1：重复绑定同一对不报错、不产生第二行。"""

    def test_same_pair_is_noop(self):
        b, db = _bindings()
        try:
            first = b.bind("e1", "s1", task_id="t1", tool_id="file_write")
            second = b.bind("e1", "s1", task_id="t1", tool_id="file_write")
            self.assertEqual(first, second)
            self.assertEqual(b.count("t1"), 1, "重复绑定产生了第二行")
            self.assertEqual(second["bound_at"], first["bound_at"],
                             "幂等绑定刷新了首次归属时间")
        finally:
            db.close()


class TestConflict(unittest.TestCase):
    """验收2：同一 effect 绑到不同 step 必须拒绝且不覆盖原绑定。"""

    def test_conflicting_step_rejected(self):
        b, db = _bindings()
        try:
            b.bind("e1", "s1", task_id="t1")
            with self.assertRaises(BindingConflict):
                b.bind("e1", "s2", task_id="t1")
            self.assertEqual(b.get("e1")["step_id"], "s1",
                             "冲突绑定覆盖了原归属")
        finally:
            db.close()

    def test_conflicting_task_rejected(self):
        b, db = _bindings()
        try:
            b.bind("e1", "s1", task_id="t1")
            with self.assertRaises(BindingConflict):
                b.bind("e1", "s1", task_id="t2")
            self.assertEqual(b.get("e1")["task_id"], "t1")
        finally:
            db.close()

    def test_one_step_may_own_many_effects(self):
        b, db = _bindings()
        try:
            b.bind("e1", "s1", task_id="t1")
            b.bind("e2", "s1", task_id="t1")
            self.assertEqual(b.for_step("t1", "s1"), ["e1", "e2"])
        finally:
            db.close()


class TestInputValidation(unittest.TestCase):
    """验收3：空值拒绝，不落半条记录。"""

    def test_empty_identifiers_rejected(self):
        b, db = _bindings()
        try:
            for bad in ("", "   ", None, 123):
                with self.subTest(effect_id=bad):
                    with self.assertRaises(BindingError):
                        b.bind(bad, "s1", task_id="t1")
                with self.subTest(step_id=bad):
                    with self.assertRaises(BindingError):
                        b.bind("e1", bad, task_id="t1")
                with self.subTest(task_id=bad):
                    with self.assertRaises(BindingError):
                        b.bind("e1", "s1", task_id=bad)
            self.assertEqual(b.count("t1"), 0, "非法输入落下了记录")
        finally:
            db.close()


class TestExport(unittest.TestCase):
    """验收4：for_task 直接可作为 07-E 的 step_bindings。"""

    def test_for_task_shape(self):
        b, db = _bindings()
        try:
            b.bind("e1", "s1", task_id="t1")
            b.bind("e2", "s2", task_id="t1")
            b.bind("e9", "s9", task_id="t2")      # 其他任务不得混入
            self.assertEqual(b.for_task("t1"), {"e1": "s1", "e2": "s2"})
            self.assertEqual(b.for_task("t2"), {"e9": "s9"})
            self.assertEqual(b.for_task("nope"), {})
        finally:
            db.close()


class TestReconcilerIntegration(unittest.TestCase):
    """验收5：绑定让同工具多声明的对账从 ambiguous 变为可确定。"""

    def _records(self, *specs):
        return [{"effect_id": eid, "task_id": "t1", "tool_id": tool,
                 "status": "confirmed"} for eid, tool in specs]

    def test_ambiguity_resolved_by_binding(self):
        b, db = _bindings()
        try:
            plan = _plan(_step("s1", "file_write", state="done"),
                         _step("s2", "file_write", state="done"))
            rows = self._records(("e1", "file_write"), ("e2", "file_write"))
            # 无绑定：只能判歧义
            bare = reconcile_plan(plan, rows, task_id="t1")
            self.assertFalse(bare.ok, "无绑定时不应声称可确定归因")
            self.assertTrue(getattr(bare, "ambiguous", []),
                            "无绑定时未标歧义")
            # 有绑定：可确定归因
            b.bind("e1", "s2", task_id="t1", tool_id="file_write")
            b.bind("e2", "s1", task_id="t1", tool_id="file_write")
            rep = reconcile_plan(plan, rows, task_id="t1",
                                 step_bindings=b.for_task("t1"))
            self.assertTrue(rep.ok, f"有绑定仍判不一致: {rep.findings()}")
            pairs = sorted((m.effect_id, m.step_id) for m in rep.matched)
            self.assertEqual(pairs, [("e1", "s2"), ("e2", "s1")])
        finally:
            db.close()

    def test_binding_cannot_forge_attribution(self):
        """绑定若指向未声明该工具的步骤，对账必须拒绝而不是照单全收。"""
        b, db = _bindings()
        try:
            plan = _plan(_step("s1", "file_write", state="done"),
                         _step("s2", "http_post", state="done"))
            rows = self._records(("e1", "file_write"))
            b.bind("e1", "s2", task_id="t1", tool_id="file_write")
            with self.assertRaises(ReconciliationError):
                reconcile_plan(plan, rows, task_id="t1",
                               step_bindings=b.for_task("t1"))
        finally:
            db.close()


class TestBoundedScope(unittest.TestCase):
    """验收6：绑定不修改副作用账本、不修改计划。"""

    def test_ledger_and_plan_untouched(self):
        db = _db()
        try:
            ledger = SideEffectLedger(db)
            eid = ledger.record("t1", "file_write", summary="probe")
            before_rows = copy.deepcopy(ledger.history("t1"))
            plan = _plan(_step("s1", "file_write", state="done"))
            plan.completed_work.append("s1")
            before_plan = copy.deepcopy(plan.to_dict())

            bindings = ActionBindings(db)
            bindings.bind(eid, "s1", task_id="t1", tool_id="file_write")

            self.assertEqual(ledger.history("t1"), before_rows,
                             "绑定修改了副作用账本")
            self.assertEqual(plan.to_dict(), before_plan,
                             "绑定修改了计划")
            self.assertEqual(ledger.get(eid)["status"], "planned",
                             "绑定改动了副作用状态")
        finally:
            db.close()


class TestEngineWiring(unittest.TestCase):
    """验收7：真实 ReactEngine 执行后产生绑定。"""

    def _engine(self, db, bindings):
        registry = ToolRegistry(
            PermissionController(high_risk_mode="allow"), db)
        ledger = SideEffectLedger(db)
        calls = []

        def runner(args):
            calls.append(args)
            return _ok()

        registry.register(ToolContract("send", side_effect_level="external"),
                          runner)
        model = ScriptModel([Action("tool_call", tool="send",
                                    args={"target": "a"})])
        engine = ReactEngine(model, registry, effects=ledger,
                             bindings=bindings)
        return engine, calls

    def test_binding_recorded_for_active_step(self):
        db = _db()
        try:
            bindings = ActionBindings(db)
            engine, calls = self._engine(db, bindings)
            plan = _plan(_step("s1", "send"))
            engine.run([], BudgetController(BudgetLimits()), plan=plan,
                       task_id="t1", run_id="r1")
            self.assertEqual(len(calls), 1, "工具未真正执行，无法验证绑定")
            exported = bindings.for_task("t1")
            self.assertEqual(list(exported.values()), ["s1"],
                             f"运行时未记录步骤绑定: {exported}")
        finally:
            db.close()

    def test_no_binding_without_active_step(self):
        db = _db()
        try:
            bindings = ActionBindings(db)
            engine, calls = self._engine(db, bindings)
            # 无计划 → active_step 为 None → 不应猜测归属
            engine.run([], BudgetController(BudgetLimits()),
                       task_id="t1", run_id="r1")
            self.assertEqual(len(calls), 1)
            self.assertEqual(bindings.count("t1"), 0,
                             "没有活动步骤却产生了绑定（在猜归属）")
        finally:
            db.close()


class TestWiringNoRegression(unittest.TestCase):
    """验收8：不传 bindings 时行为与绑定零差异。"""

    def test_without_bindings_same_outcome(self):
        results = {}
        for label, use_bindings in (("off", False), ("on", True)):
            db = _db()
            try:
                registry = ToolRegistry(
                    PermissionController(high_risk_mode="allow"), db)
                ledger = SideEffectLedger(db)

                def runner(args):
                    return _ok()

                registry.register(
                    ToolContract("send", side_effect_level="external"), runner)
                model = ScriptModel([
                    Action("tool_call", tool="send", args={"target": "a"}),
                    Action("finish", answer="done"),
                ])
                kwargs = {"bindings": ActionBindings(db)} if use_bindings else {}
                engine = ReactEngine(model, registry, effects=ledger, **kwargs)
                plan = _plan(_step("s1", "send"))
                outcome = engine.run([], BudgetController(BudgetLimits()),
                                     plan=plan, task_id="t1", run_id="r1")
                results[label] = (outcome.finished, outcome.answer,
                                  plan.completed_work,
                                  [s.state for s in plan.steps])
            finally:
                db.close()
        self.assertEqual(results["off"], results["on"],
                         "注入 bindings 改变了原有执行结果")


if __name__ == "__main__":
    unittest.main()
