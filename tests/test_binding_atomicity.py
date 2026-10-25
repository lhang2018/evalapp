# -*- coding: utf-8 -*-
"""ALN-07-C：原子绑定、精确步骤授权与对账消费。

验收对照：
- 崩溃重启无半绑定：意图先于副作用登记，崩溃后 heal_pending 确定性补绑；
- 授权不扩大/重复消费：接管恢复经对账门禁，副作用不明禁止自动重放，
  授权不因恢复重新发放（复用既有 04 语义并断言）；
- 真实恢复先对账后决定：recover_task 在续跑前执行三方对账
  （计划声明 × 副作用账本 × step 绑定），干净放行、分歧转 WAITING_USER。
"""

import sqlite3
import unittest

import evoagent.core.task as T
from evoagent.app import App
from evoagent.core.persistence import Database
from evoagent.planning.binding import ActionBindings, BindingConflict
from evoagent.planning.planner import ExecutionPlan, PlanStep


def _plan_dict(step_states=(("s1", "file_write", "done"),)):
    steps = [PlanStep(sid, f"步骤{sid}", tool_hint=tool, state=state)
             for sid, tool, state in step_states]
    completed = [sid for sid, _, state in step_states if state == "done"]
    plan = ExecutionPlan("p_gate", "目标", list(steps),
                         completed_work=list(completed),
                         acceptance_criteria=[{"type": "tool_succeeded"}])
    return plan.to_dict()


def _row(effect_id, task_id="t", tool="file_write", status="confirmed"):
    return {"effect_id": effect_id, "task_id": task_id,
            "tool_id": tool, "status": status}


class IntentLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.bindings = ActionBindings(self.db)

    def tearDown(self):
        self.db.close()

    def test_intent_resolve_roundtrip(self):
        intent = self.bindings.record_intent("t", "s1", "file_write")
        self.assertEqual(self.bindings.pending_intents("t")[0]["intent_id"],
                         intent)
        bound = self.bindings.resolve_intent(intent, "fx_1")
        self.assertEqual(bound["step_id"], "s1")
        self.assertEqual(self.bindings.pending_intents("t"), [])
        self.assertEqual(self.bindings.get("fx_1")["step_id"], "s1")
        # 幂等：重复 resolve 同一 effect 无副作用
        again = self.bindings.resolve_intent(intent, "fx_1")
        self.assertEqual(again["step_id"], "s1")

    def test_intent_cannot_rebind_to_other_effect(self):
        intent = self.bindings.record_intent("t", "s1", "file_write")
        self.bindings.resolve_intent(intent, "fx_1")
        with self.assertRaises(BindingConflict):
            self.bindings.resolve_intent(intent, "fx_2")

    def test_record_intent_idempotent(self):
        first = self.bindings.record_intent(
            "t", "s1", "file_write", now=123.0)
        second = self.bindings.record_intent(
            "t", "s1", "file_write", now=123.0)
        self.assertEqual(first, second)
        self.assertEqual(len(self.bindings.pending_intents("t")), 1)


class HealPendingTest(unittest.TestCase):
    """崩溃窗口：副作用已发生、绑定未写 → 恢复时确定性补绑。"""

    def setUp(self):
        self.db = Database(":memory:")
        self.bindings = ActionBindings(self.db)

    def tearDown(self):
        self.db.close()

    def test_heal_binds_crashed_effect_uniquely(self):
        intent = self.bindings.record_intent("t", "s1", "file_write")
        # 崩溃：execute 已落账（fx_1 confirmed）但 resolve 未执行
        result = self.bindings.heal_pending("t", [_row("fx_1")])
        self.assertEqual(result["healed"], {"fx_1": "s1"})
        self.assertEqual(result["stale"], [])
        self.assertEqual(self.bindings.get("fx_1")["step_id"], "s1")
        self.assertEqual(self.bindings.pending_intents("t"), [])
        # 意图已固化（effect_id 回填）
        self.assertEqual(self.bindings.intent(intent)["effect_id"], "fx_1")

    def test_heal_without_effect_marks_stale_not_bound(self):
        self.bindings.record_intent("t", "s1", "file_write")
        result = self.bindings.heal_pending("t", [])
        self.assertEqual(result["healed"], {})
        self.assertEqual(result["stale"],
                         [self.bindings.pending_intents("t")[0]["intent_id"]])
        # stale 意图保留为证据；若副作用迟到落账，再次 heal 仍可
        # 确定性补绑（恢复一致性，而不是永久放弃归属）
        self.assertEqual(len(self.bindings.pending_intents("t")), 1)
        again = self.bindings.heal_pending("t", [_row("fx_1")])
        self.assertEqual(again["healed"], {"fx_1": "s1"})

    def test_heal_multiple_candidates_is_ambiguous_not_autobound(self):
        self.bindings.record_intent("t", "s1", "file_write")
        result = self.bindings.heal_pending(
            "t", [_row("fx_1"), _row("fx_2")])
        self.assertEqual(result["healed"], {})
        self.assertEqual(len(result["ambiguous"]), 1)
        self.assertIsNone(self.bindings.get("fx_1"))
        self.assertIsNone(self.bindings.get("fx_2"))

    def test_heal_skips_effects_already_bound(self):
        self.bindings.bind("fx_0", "s1", task_id="t", tool_id="file_write")
        self.bindings.record_intent("t", "s2", "file_write")
        result = self.bindings.heal_pending(
            "t", [_row("fx_0", tool="file_write")])
        self.assertEqual(result["healed"], {})
        # s1 已有绑定 effect，s2 的候选被排除（防重复归因）


class RecoveryGateTest(unittest.TestCase):
    """恢复先对账后决定：干净放行、分歧 WAITING_USER、副作用不重放。"""

    def setUp(self):
        import tempfile, shutil
        self.tmp = tempfile.mkdtemp(prefix="evoagent_07c_")
        self.app = App(data_dir=self.tmp)
        self.task = T.Task(task_id="t_gate", user_goal="接管恢复对账")
        self.task.runtime_manifest["execution_plan"] = _plan_dict()
        # CREATED 时建立首个运行（租约入账），_abandon 再模拟执行中失联
        self.task.task_status = T.TaskStatus.CREATED
        self.app.db.save_task(self.task)

    def tearDown(self):
        self.app.db.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _abandon(self):
        # 复刻 test_run_ownership 的 abandon 范式：begin 产生真实租约后
        # 停掉心跳并把 expires_at 置 0，模拟属主进程失联
        task = self.app.db.get_task(self.task.task_id)
        rid, _ = self.app.controller.runs.begin(task, {})
        token, reset, stop, thread = \
            self.app.controller.runs._handles.pop(rid)
        stop.set()
        thread.join(2)
        from evoagent.core.leases import CURRENT_LEASE
        CURRENT_LEASE.reset(reset)
        self.app.db._conn.execute(
            "UPDATE run_leases SET expires_at=0 WHERE task_id=?",
            (self.task.task_id,))
        self.app.db._conn.commit()
        # 模拟原属主崩溃时的任务状态：执行中
        task.task_status = T.TaskStatus.RUNNING
        self.app.db.save_task(task)

    def _checkpoint(self):
        self.app.db.save_checkpoint(
            "cp", self.task.task_id, 1.0,
            {"budget_state": {"usage": {"model_calls": 0}},
             "react_state": {}, "task_state": {}})

    def _gate(self):
        task = self.app.db.get_task(self.task.task_id)
        if task.task_status != T.TaskStatus.RUNNING:
            task.task_status = T.TaskStatus.RUNNING
            self.app.db.save_task(task)
        return self.app.controller._reconciliation_gate(task)

    def test_clean_reconciliation_allows_recovery(self):
        self.app.controller.react.bindings.bind(
            "fx_1", "s1", task_id="t_gate", tool_id="file_write")
        self.app.db.save_side_effect(
            "fx_1", "t_gate", "", "file_write", None, "", "confirmed")
        gated = self._gate()
        self.assertIsNone(gated, "对账干净应放行续跑")
        events = [e for e in self.app.bus.query()
                  if e.event_type == "reconciliation_gate"]
        self.assertEqual(events[-1].payload["decision"], "resume")

    def test_divergence_blocks_recovery_without_replay(self):
        # 未声明副作用：账本有记录、计划无对应声明匹配
        self.app.db.save_side_effect(
            "fx_rogue", "t_gate", "", "file_write", None, "", "unknown")
        calls_before = len(self.app.bus.query())
        gated = self._gate()
        self.assertIsNotNone(gated)
        self.assertEqual(gated.task_status.value, "WAITING_USER")
        self.assertIn("副作用分歧", gated.waiting_request["question"])
        # 未发生任何工具重放：无新 tool_call 事件
        tool_calls = [e for e in self.app.bus.query()[calls_before:]
                      if e.event_type == "tool_call"]
        self.assertEqual(tool_calls, [])

    def test_crashed_intent_healed_then_clean_resume(self):
        bindings = self.app.controller.react.bindings
        bindings.record_intent("t_gate", "s1", "file_write")
        self.app.db.save_side_effect(
            "fx_1", "t_gate", "", "file_write", None, "", "confirmed")
        gated = self._gate()
        self.assertIsNone(gated, "补绑后对账干净应放行")
        self.assertEqual(bindings.get("fx_1")["step_id"], "s1")

    def test_no_plan_task_gate_skips_with_marker(self):
        task = T.Task(task_id="t_noplan", user_goal="无计划任务")
        task.task_status = T.TaskStatus.RUNNING
        self.app.db.save_task(task)
        gated = self.app.controller._reconciliation_gate(task)
        self.assertIsNone(gated)
        self.assertEqual(task.runtime_manifest.get("reconciliation_skipped"),
                         "no_plan")

    def test_recover_task_e2e_divergence_waits_user(self):
        # 分歧用 confirmed 未声明效果构造：未决(planned/submitted/unknown)
        # 副作用由 leases 恢复检查先行阻断（04-R 语义），gate 层处理的是
        # 已决分歧——confirmed 但无声明匹配（undeclared）
        self._checkpoint()
        self._abandon()
        # confirmed 且工具不在任何声明中（http_post）→ undeclared 分歧；
        # 同工具的 confirmed 会被 07-E 按工具合理归因为 matched，不是分歧
        self.app.db.save_side_effect(
            "fx_rogue", "t_gate", "", "http_post", None, "", "confirmed")
        out = self.app.controller.recover_task("t_gate")
        self.assertEqual(out.task_status.value, "WAITING_USER")
        # 授权不重复发放：分歧拦截发生在授权阶段之前/复用，无新增 grant
        tables = {r["name"] for r in self.app.db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        auth_tables = [t for t in tables if "authoriz" in t]
        for table in auth_tables:
            n = self.app.db._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE task_id=?",
                ("t_gate",)).fetchone()["n"]
            self.assertEqual(n, 0, f"对账拦截后不得发放任何新授权（{table}）")

    def test_recover_task_e2e_clean_path_completes(self):
        self.task.runtime_manifest["execution_plan"] = _plan_dict(
            (("s1", "current_time", "done"),))
        self.app.db.save_task(self.task)
        # current_time 为纯观察工具：账本无行、计划 done——为使对账干净，
        # 不给它 tool_hint 声明（无副作用声明即无分歧）
        self.task.runtime_manifest["execution_plan"] = {
            "plan_id": "p_gate", "goal": "g",
            "steps": [{"step_id": "s1", "description": "完成",
                       "state": "done"}],
            "completed_work": ["s1"],
            "acceptance_criteria": [{"type": "tool_succeeded"}]}
        self.app.db.save_task(self.task)
        self._checkpoint()
        self._abandon()
        out = self.app.controller.recover_task("t_gate")
        self.assertNotEqual(out.task_status.value, "WAITING_USER",
                            out.failure_reason or "")
        self.assertIn(out.task_status.value, ("SUCCEEDED", "FAILED"))
        # 干净路径的失败只能是业务验收，不得是对账拦截
        self.assertNotIn("对账", (out.failure_reason or ""))


if __name__ == "__main__":
    unittest.main()
