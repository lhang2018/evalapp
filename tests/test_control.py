"""执行内核接线测试（PRD 11-12, 19, 34-36, 55-56）。

覆盖：模型调用硬预算、计划驱动与进度持久化、模型路由、暂停/恢复、
失败重跑新任务、副作用未知时的恢复闸门。
"""

import shutil
import tempfile
import threading
import time
import unittest

from evoagent.app import App
from evoagent.core.checkpoint import Checkpoint
from evoagent.core.task import TaskStatus
from evoagent.models.mock import MockModel


class BlockingModel(MockModel):
    """首次决策时阻塞，便于测试确定性地在运行中暂停。"""

    def __init__(self, entered, proceed):
        super().__init__()
        self.entered = entered
        self.proceed = proceed
        self._blocked = False

    def suggest_action(self, messages, available_tools):
        if not self._blocked:
            self._blocked = True
            self.entered.set()
            if not self.proceed.wait(5):
                raise RuntimeError("pause test timeout")
        return super().suggest_action(messages, available_tools)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_control_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestBudgetWiring(Base):
    def test_model_calls_are_metered(self):
        t = self.app.submit("现在几点了", intent="time_query",
                            acceptance=[{"type": "tool_succeeded"}])
        self.assertEqual(t.task_status.value, "SUCCEEDED")
        usage = t.result_evidence[-1]["budget"]["usage"]
        # 规划 1 次 + ReAct 至少 1 次
        self.assertGreaterEqual(usage["model_calls"], 2)
        self.assertGreaterEqual(usage["tool_calls"], 1)

    def test_model_call_limit_stops_task(self):
        t = self.app.submit("现在几点了", intent="time_query",
                            acceptance=[{"type": "tool_succeeded"}],
                            budget_overrides={"max_model_calls": 1})
        self.assertEqual(t.task_status.value, "FAILED")
        self.assertIn("预算", str(t.failure_reason))


class TestPlanDriven(Base):
    def test_plan_persisted_and_progressed(self):
        t = self.app.submit("现在几点了", intent="time_query",
                            acceptance=[{"type": "tool_succeeded"}])
        self.assertEqual(t.task_status.value, "SUCCEEDED")
        plan = t.runtime_manifest.get("execution_plan")
        self.assertIsInstance(plan, dict)
        self.assertTrue(plan.get("steps"))
        self.assertEqual(plan["plan_id"], t.execution_plan_ref)
        progress = t.runtime_manifest.get("plan_progress")
        self.assertIsInstance(progress, dict)
        self.assertIn("s1", progress["done"])
        # 原目标与硬约束保留在计划中（PRD 11.2）
        self.assertEqual(plan["original_goal"], t.user_goal)


class TestModelRouting(Base):
    def test_router_is_invoked_and_recorded(self):
        calls = []
        original = self.app.controller.router.route

        def spy(intent, domain, **kwargs):
            calls.append((intent, domain))
            return original(intent, domain, **kwargs)

        self.app.controller.router.route = spy
        t = self.app.submit("现在几点了", intent="time_query",
                            acceptance=[{"type": "tool_succeeded"}])
        self.assertEqual(t.task_status.value, "SUCCEEDED")
        self.assertTrue(calls)
        self.assertEqual(t.runtime_manifest["model_route"]["model_id"],
                         "mock-1")


class TestPauseResume(Base):
    def test_pause_then_resume_completes(self):
        entered, proceed = threading.Event(), threading.Event()
        blocking = BlockingModel(entered, proceed)
        self.app.controller.router.route = lambda *a, **k: blocking

        t = self.app.controller.submit_task(
            "现在几点了", intent="time_query",
            acceptance=[{"type": "tool_succeeded"}], wait=False)
        self.assertTrue(entered.wait(5), "模型未进入首次决策")

        out = self.app.controller.pause_task(t.task_id)
        self.assertTrue(out["ok"], out)
        proceed.set()

        current = None
        deadline = time.time() + 5
        while time.time() < deadline:
            current = self.app.get_task(t.task_id)
            if current and current.task_status == TaskStatus.SUSPENDED:
                break
            time.sleep(0.02)
        self.assertIsNotNone(current)
        self.assertEqual(current.task_status, TaskStatus.SUSPENDED)

        cp = self.app.controller.cp_mgr.latest(t.task_id)
        self.assertIsNotNone(cp)
        self.assertEqual(cp.current_stage, "paused")
        self.assertTrue(cp.budget_state.get("usage"))

        resumed = self.app.controller.resume_task(t.task_id)
        self.assertEqual(resumed.task_status.value, "SUCCEEDED")

    def test_resume_failed_creates_new_task(self):
        self.app.permissions.high_risk_mode = "deny"
        t = self.app.submit("写笔记 path=notes/x.txt content=abc",
                            intent="note_taking",
                            acceptance=[{"type": "tool_succeeded"}])
        self.assertEqual(t.task_status.value, "FAILED")

        retry = self.app.controller.resume_task(t.task_id)
        self.assertIsNotNone(retry)
        self.assertNotEqual(retry.task_id, t.task_id)
        self.assertEqual(retry.parent_task_id, t.task_id)
        self.assertEqual(retry.runtime_manifest["resumed_from"], t.task_id)

    def test_resume_requires_force_for_unknown_side_effects(self):
        t = self.app.submit("现在几点了", intent="time_query",
                            acceptance=[{"type": "tool_succeeded"}])
        t.task_status = TaskStatus.SUSPENDED
        self.app.db.save_task(t)
        cp = Checkpoint(task_id=t.task_id, current_stage="paused")
        cp.pending_side_effects = [{"tool": "file_write",
                                    "state": "unknown"}]
        self.app.controller.cp_mgr.save(cp)

        with self.assertRaises(PermissionError):
            self.app.controller.resume_task(t.task_id)

    def test_pause_rejected_when_not_running(self):
        t = self.app.submit("现在几点了", intent="time_query",
                            acceptance=[{"type": "tool_succeeded"}])
        out = self.app.controller.pause_task(t.task_id)
        self.assertFalse(out["ok"])

    def test_resume_terminal_rejected(self):
        t = self.app.submit("现在几点了", intent="time_query",
                            acceptance=[{"type": "tool_succeeded"}])
        with self.assertRaises(ValueError):
            self.app.controller.resume_task(t.task_id)


if __name__ == "__main__":
    unittest.main()
