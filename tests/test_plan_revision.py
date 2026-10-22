"""ALN-07-P：计划修订与已完成证据保持。

对应任务卡 docs/协作开发/任务卡/07-计划修订与证据保持.md。
本文件是先行的不变量夹具：证明现状违约（修订丢失已完成证据、可降级
已完成步骤、可产生结构非法的计划），再锁定修复后的行为。

背景：react/engine.py 在步骤成功时把 step_id 追加进
plan.completed_work（单调证据）；而 ExecutionPlan.adjust 用新步骤列表
重算该字段 → 修订一旦重写已完成步骤，证据即丢失，续跑可能重放成功
副作用，正是 ALN-07 关闭标准「失败分支修订可续跑且不重放成功副作用」
禁止的行为。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evoagent.planning.planner import (  # noqa: E402
    ExecutionPlan,
    PlanRevisionError,
    PlanStep,
    PlanningEngine,
)


def _plan_with_first_step_done():
    """构造一个 s1 已完成的计划（复刻引擎成功路径后的状态）。"""
    plan = PlanningEngine().make_plan("整理桌面文件", intent="note_taking")
    first = plan.steps[0]
    first.state = "done"
    plan.completed_work.append(first.step_id)
    return plan


def _engine_recovery_revision(plan):
    """复刻 react/engine._mark_step_failed 的 adjust 调用形状。"""
    cur = plan.steps[1]
    cur.state = "failed"
    used = {s.step_id for s in plan.steps}
    n = len(plan.steps) + 1
    while f"r{n}" in used:
        n += 1
    recovery = PlanStep(step_id=f"r{n}",
                        description=f"失败恢复：核验 {cur.step_id} 的副作用",
                        deps=[cur.step_id])
    return list(plan.steps) + [recovery], f"步骤 {cur.step_id} 连续失败"


class TestCompletedEvidencePreserved(unittest.TestCase):
    """不变量 1：completed_work 单调不减（ALN-07-P 验收第 1 条）。"""

    def test_completed_work_survives_revision(self):
        plan = _plan_with_first_step_done()
        done_id = plan.steps[0].step_id
        self.assertEqual(plan.completed_work, [done_id])

        revised = list(plan.steps) + [
            PlanStep(step_id="n1", description="补充收尾检查", deps=[done_id])]
        plan.adjust(revised, reason="replace 追加收尾")
        self.assertIn(done_id, plan.completed_work,
                      "修订后已完成证据必须仍在")

    def test_revision_rejects_dropping_completed_step(self):
        """模型重规划把整个计划重发、已完成步骤不在新清单里 → 必须拒绝。"""
        plan = _plan_with_first_step_done()
        done_id = plan.steps[0].step_id
        before_steps = [s.step_id for s in plan.steps]

        with self.assertRaises(PlanRevisionError):
            plan.adjust([PlanStep(step_id="x1", description="全新计划",
                                  deps=[])],
                        reason="model replan 全量重发")

        self.assertEqual(plan.completed_work, [done_id],
                         "被拒绝的修订不得破坏已完成证据")
        self.assertEqual([s.step_id for s in plan.steps], before_steps,
                         "被拒绝的修订不得改动步骤清单")

    def test_revision_rejects_demoting_completed_step(self):
        """已完成步骤被重发为 pending → 必须拒绝（否则重跑成功副作用）。"""
        plan = _plan_with_first_step_done()
        done_id = plan.steps[0].step_id

        demoted = [PlanStep(step_id=done_id, description="重新做第一步",
                            state="pending")]
        demoted += [s for s in plan.steps[1:]]
        with self.assertRaises(PlanRevisionError):
            plan.adjust(demoted, reason="model replan 不知该步骤已完成")

        self.assertEqual(plan.completed_work, [done_id])
        self.assertEqual(plan.steps[0].state, "done",
                         "被拒绝的修订不得把已完成步骤降级")


class TestRevisionProducesValidPlan(unittest.TestCase):
    """不变量 2：修订后的计划必须结构合法（ALN-07-P 验收第 3 条）。"""

    def test_revision_rejects_dangling_dependency(self):
        plan = _plan_with_first_step_done()
        with self.assertRaises(PlanRevisionError):
            plan.adjust([PlanStep(step_id="a", description="x",
                                  deps=["nonexistent"])],
                        reason="坏修订")
        self.assertTrue(all(s.step_id != "a" for s in plan.steps),
                        "非法修订不得落入计划")

    def test_revision_rejects_dependency_cycle(self):
        plan = _plan_with_first_step_done()
        with self.assertRaises(PlanRevisionError):
            plan.adjust([PlanStep(step_id="a", description="x", deps=["b"]),
                         PlanStep(step_id="b", description="y", deps=["a"])],
                        reason="坏修订")

    def test_revision_rejects_empty_plan(self):
        plan = _plan_with_first_step_done()
        with self.assertRaises(PlanRevisionError):
            plan.adjust([], reason="坏修订")
        self.assertTrue(plan.steps, "空修订不得通过")

    def test_revision_rejects_duplicate_step_ids(self):
        plan = _plan_with_first_step_done()
        with self.assertRaises(PlanRevisionError):
            plan.adjust([PlanStep(step_id="d", description="x"),
                         PlanStep(step_id="d", description="y")],
                        reason="坏修订")


class TestRevisionTraceability(unittest.TestCase):
    """不变量 3：修订留痕且崩溃恢复后可读（ALN-07-P 验收第 4 条）。"""

    def test_revision_is_recorded_and_round_trips(self):
        plan = _plan_with_first_step_done()
        plan.adjust(list(plan.steps) + [
            PlanStep(step_id="n1", description="补充", deps=[])],
            reason="replace 补充步骤")

        self.assertEqual(len(plan.revisions), 1)
        rec = plan.revisions[-1]
        self.assertEqual(rec["reason"], "replace 补充步骤")
        self.assertEqual(rec["level"], "replace")
        self.assertEqual(rec["added"], ["n1"])
        self.assertEqual(rec["removed"], [])
        self.assertIn("at", rec)

        restored = ExecutionPlan.from_dict(plan.to_dict())
        self.assertEqual(restored.revisions, plan.revisions,
                         "序列化往返必须保留修订记录")
        self.assertEqual(restored.completed_work, plan.completed_work)

    def test_change_reasons_still_appended(self):
        plan = _plan_with_first_step_done()
        before = len(plan.change_reasons)
        plan.adjust(list(plan.steps), reason="replace 无变化")
        self.assertEqual(len(plan.change_reasons), before + 1)


class TestRevisionLevels(unittest.TestCase):
    """分级语义：append 只增不减；reauthorize 需人工授权。"""

    def test_append_rejects_removing_existing_step(self):
        plan = _plan_with_first_step_done()
        with self.assertRaises(PlanRevisionError):
            plan.adjust([s for s in plan.steps[1:]], reason="append 删步骤",
                        level="append")

    def test_unknown_level_rejected(self):
        plan = _plan_with_first_step_done()
        with self.assertRaises(PlanRevisionError):
            plan.adjust(list(plan.steps), reason="x", level="teleport")

    def test_reauthorize_not_silently_allowed(self):
        """目标/约束变更必须走人工授权，adjust 不得静默接受。"""
        plan = _plan_with_first_step_done()
        with self.assertRaises(PlanRevisionError):
            plan.adjust(list(plan.steps), reason="改目标", level="reauthorize")


class TestExistingBehaviourLocked(unittest.TestCase):
    """锁定既有正确行为：既有唯一调用方（引擎恢复追加）不得被破坏。"""

    def test_engine_recovery_append_shape_still_works(self):
        plan = _plan_with_first_step_done()
        revised, reason = _engine_recovery_revision(plan)
        plan.adjust(revised, reason=reason)

        self.assertEqual(plan.steps[-1].step_id, "r4")
        self.assertEqual(plan.steps[-1].deps, [plan.steps[1].step_id])
        self.assertTrue(any(reason in r for r in plan.change_reasons))
        self.assertEqual(plan.completed_work, [plan.steps[0].step_id])

    def test_immutable_fields_not_touched_by_adjust(self):
        plan = _plan_with_first_step_done()
        goal, constraints = plan.original_goal, list(plan.immutable_constraints)
        plan.immutable_constraints.append("只读本地文件")
        plan.adjust(list(plan.steps) + [
            PlanStep(step_id="n1", description="补充", deps=[])],
            reason="replace")
        self.assertEqual(plan.original_goal, goal)
        self.assertEqual(plan.immutable_constraints[-1], "只读本地文件")


if __name__ == "__main__":
    unittest.main()
