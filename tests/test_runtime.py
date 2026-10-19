"""ReAct 循环 + 检查点恢复 + 控制器集成测试（PRD 11-12, 34-36）。"""

import shutil
import tempfile
import unittest
from pathlib import Path

from evoagent.app import App
from evoagent.core.task import RiskLevel


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_test_")
        self.app = App(data_dir=self.tmp)
        self.confirm_app = App(
            data_dir=self.tmp,
            confirm_callback=lambda op, args: op == "file_write")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRuntime(Base):
    def test_time_task_full_pipeline(self):
        t = self.app.submit("现在几点，请告诉我准确时间",
                            intent="time_query",
                            acceptance=[{"type": "tool_succeeded"}],
                            wait=True)
        self.assertEqual(t.task_status.value, "SUCCEEDED")
        self.assertIn(":", str(t.result))
        self.assertEqual(t.result_evidence[-1]["verdict"], "1/1 通过")

    def test_compute_task(self):
        t = self.app.submit("请帮我计算 12*34 结果", intent="computation",
                            acceptance=[{"type": "tool_goal_reached"}])
        self.assertEqual(t.task_status.value, "SUCCEEDED")
        self.assertIn("408", str(t.result))

    def test_high_risk_without_confirm_fails_safe(self):
        t = self.app.submit("写笔记 path=notes/x.txt content=abc",
                            intent="note_taking",
                            acceptance=[{"type": "tool_succeeded"}])
        # 无确认回调 → file_write 被拒绝 → Mock 走降级 finish → 工具未成功
        self.assertEqual(t.task_status.value, "FAILED")

    def test_high_risk_with_confirm_succeeds(self):
        t = self.confirm_app.submit("写笔记 path=notes/x.txt content=abc",
                                    intent="note_taking",
                                    acceptance=[{"type": "tool_succeeded"}])
        self.assertEqual(t.task_status.value, "SUCCEEDED")
        # file_write 的独立验证已重读比对；文件应存在于 workspace
        self.assertTrue(Path(self.tmp, "workspace", "notes", "x.txt").exists())

    def test_cancel_terminal(self):
        t = self.app.submit("现在几点了", intent="time_query", wait=True)
        out = self.app.controller.cancel_task(t.task_id)
        self.assertFalse(out["ok"])  # 终态任务不可取消

    def test_checkpoint_created_during_run(self):
        t = self.app.submit("现在几点了", intent="time_query",
                            acceptance=[{"type": "tool_succeeded"}])
        cp = self.app.controller.cp_mgr.latest(t.task_id)
        self.assertIsNotNone(cp)
        self.assertEqual(cp.task_id, t.task_id)

    def test_budget_exhaust_stops_task(self):
        t = self.app.submit("现在几点了", intent="time_query",
                            acceptance=[{"type": "tool_succeeded"}],
                            budget_overrides={"max_tool_calls": 0})
        self.assertEqual(t.task_status.value, "FAILED")
        self.assertIn("预算", str(t.failure_reason))


if __name__ == "__main__":
    unittest.main()
