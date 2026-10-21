# -*- coding: utf-8 -*-
"""F-03 副作用账本（契约 09 卷 §8）。四层验证：

1) 状态机：合法/非法转移、unknown 必须 reconcile、幂等转移
2) 映射：ToolResult → 六态（ok+executed→confirmed / uncertain→unknown /
   未执行→failed / level=none 不入账）
3) 投影：pending() 与 resume 的联动（GAP §7：投影自账本而非检查点自持）
4) 线上：外部工具超时 → unknown → resume 拒绝(409 语义) → 复原口径
"""

import shutil
import tempfile
import unittest

from evoagent.app import App
from evoagent.core.side_effects import EFFECT_STATUS, TRANSITIONS
from evoagent.tools.contract import ToolResult

try:  # tests 运行方式兼容（包内/直接）
    from .test_control import Base  # noqa: F401
except ImportError:  # pragma: no cover
    Base = None


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.paths = tempfile.mkdtemp(prefix="evoagent_f03_")
        self.app = App(data_dir=self.paths)
        self.ledger = self.app.controller.side_fx

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.paths, ignore_errors=True)

    def test_six_contract_states(self):
        self.assertEqual(
            EFFECT_STATUS,
            ("planned", "submitted", "confirmed", "failed", "unknown",
             "compensated"))

    def test_legal_chain_planned_to_confirmed(self):
        eff = self.ledger.record("taskA", "http_request",
                                 idempotency_key="k1", summary="外呼",
                                 status="planned")
        self.ledger.transition(eff, "submitted")
        self.ledger.transition(eff, "confirmed")
        self.assertEqual(self.ledger.get(eff)["status"], "confirmed")

    def test_illegal_transition_rejected(self):
        eff = self.ledger.record("taskB", "send_message", status="planned")
        # planned → confirmed 非法（未经提交/执行）
        self.assertFalse(self.ledger.transition(eff, "confirmed"))
        self.assertEqual(self.ledger.get(eff)["status"], "planned")
        # failed/compensated 终态拒绝一切转出
        eff2 = self.ledger.record("taskB", "send_message", status="failed")
        self.assertFalse(self.ledger.transition(eff2, "confirmed"))

    def test_known_transitions_complete(self):
        for src, dsts in TRANSITIONS.items():
            self.assertTrue(dsts <= set(EFFECT_STATUS), f"{src} 目标越界")

    def test_unknown_requires_reconcile(self):
        eff = self.ledger.record("taskC", "http_request", status="unknown")
        self.assertEqual(
            self.ledger.get(eff)["last_reconciled_at"], None)
        ok = self.ledger.reconcile(eff, "confirmed", receipt_ref="r-1")
        self.assertTrue(ok)
        row = self.ledger.get(eff)
        self.assertEqual(row["status"], "confirmed")
        self.assertIsNotNone(row["last_reconciled_at"])
        self.assertEqual(row["external_receipt_ref"], "r-1")
        # 二次对账拒绝：no longer unknown
        self.assertFalse(self.ledger.reconcile(eff, "failed"))

    def test_reconcile_illegal_verdict_rejected(self):
        eff = self.ledger.record("taskD", "http_request", status="unknown")
        # unknown → planned 非法
        self.assertFalse(self.ledger.reconcile(eff, "planned"))


class TestMappingAndProjection(unittest.TestCase):
    def setUp(self):
        self.paths = tempfile.mkdtemp(prefix="evoagent_f03_map_")
        self.app = App(data_dir=self.paths)
        self.ledger = self.app.controller.side_fx

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.paths, ignore_errors=True)

    def _mk(self, evidence=None, ok=True, executed=True, uncertain=False,
            key="k"):
        return ToolResult(ok=ok, executed=executed,
                          side_effect_uncertain=uncertain,
                          evidence=dict(evidence or {}),
                          idempotency_key=key)

    def test_ok_executed_maps_confirmed(self):
        eff = self.ledger.record_from_tool_result(
            "t1", "http_request", self._mk(
                {"side_effect_level": "external"}))
        self.assertEqual(self.ledger.get(eff)["status"], "confirmed")

    def test_uncertain_maps_unknown(self):
        eff = self.ledger.record_from_tool_result(
            "t2", "http_request", self._mk(
                {"side_effect_level": "external"}, ok=False,
                executed=False, uncertain=True))
        self.assertEqual(self.ledger.get(eff)["status"], "unknown")

    def test_no_execution_maps_failed(self):
        eff = self.ledger.record_from_tool_result(
            "t3", "send_message", self._mk(
                {"side_effect_level": "external"}, ok=False,
                executed=False, uncertain=False))
        self.assertEqual(self.ledger.get(eff)["status"], "failed")

    def test_plain_tool_not_recorded(self):
        eff = self.ledger.record_from_tool_result(
            "t4", "file_write", self._mk(None))
        self.assertIsNone(eff)
        self.assertEqual(self.ledger.history("t4"), [])

    def test_pending_projection_and_reconcile_shrink(self):
        eff = self.ledger.record_from_tool_result(
            "t5", "http_request", self._mk(
                {"side_effect_level": "external"}, ok=False,
                executed=False, uncertain=True))
        done = self.ledger.record_from_tool_result(
            "t5", "file_write", self._mk(
                {"side_effect_level": "local"}))
        pend = self.ledger.pending("t5")
        self.assertEqual([p["effect_id"] for p in pend], [eff])
        # 对账后 shrunk
        self.ledger.reconcile(eff, "failed")
        self.assertEqual(self.ledger.pending("t5"), [])
        self.assertEqual(self.ledger.get(done)["status"], "confirmed")


class TestResumeWiring(unittest.TestCase):
    """resume 直读账本：unknown 未对账 → 需 force；对账为 failed 后无需 force。"""

    def setUp(self):
        self.paths = tempfile.mkdtemp(prefix="evoagent_f03_resume_")
        self.app = App(data_dir=self.paths)
        self.ledger = self.app.controller.side_fx

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.paths, ignore_errors=True)

    def test_ledger_pending_drives_resume_guard(self):
        from evoagent.core.task import Task, TaskStatus
        t = Task(task_id="task_fx_1", user_goal="g")
        t.task_status = TaskStatus.SUSPENDED
        self.app.db.save_task(t)
        eff = self.ledger.record("task_fx_1", "http_request",
                                 status="unknown")
        pending = self.ledger.pending("task_fx_1")
        self.assertTrue(pending)
        # resume 不带 force → PermissionError（沿用既有语义）
        self.assertRaises(
            PermissionError,
            self.app.controller.resume_task, "task_fx_1")
        # 对账（未知→补偿）后不再拦截
        self.ledger.reconcile(eff, "compensated")
        resumed = self.app.controller.resume_task("task_fx_1")
        self.assertIsNotNone(resumed)


if __name__ == "__main__":
    unittest.main()
