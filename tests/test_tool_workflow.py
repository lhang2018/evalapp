"""工具生命周期与工作流执行补强测试（PRD 13-16 / Architecture 6, 16）。"""

import shutil
import tempfile
import threading
import time
import unittest

from evoagent.app import App
from evoagent.core.budget import BudgetController, BudgetExceeded, BudgetLimits
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.tools.registry import ToolRegistry
from evoagent.workflows.engine import WorkflowEngine
from evoagent.workflows.models import (WorkflowDef, WorkflowNode,
                                       WorkflowNodeState)


def _ok(data=None):
    return ToolResult(ok=True, data=data if data is not None else {"v": 1},
                      submitted=True, executed=True, contract_valid=True,
                      goal_reached=True, independently_verified=True)


class TestToolLifecycle(unittest.TestCase):
    def setUp(self):
        self.reg = ToolRegistry()

    def test_timeout_is_enforced(self):
        def slow(_args):
            time.sleep(0.4)
            return _ok()

        self.reg.register(ToolContract(tool_id="slow", timeout_policy=0.05,
                                       side_effect_level="none"), slow)
        started = time.time()
        res = self.reg.execute("slow", {})
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "timeout")
        self.assertLess(time.time() - started, 0.3)

    def test_retry_for_safe_tool(self):
        calls = {"n": 0}

        def flaky(_args):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("temporary")
            return _ok()

        self.reg.register(ToolContract(tool_id="flaky", timeout_policy=1,
                                       retry_policy=1, side_effect_level="none"),
                          flaky)
        res = self.reg.execute("flaky", {})
        self.assertTrue(res.ok)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(res.evidence["attempts"], 2)

    def test_no_retry_for_external_tool_without_idempotency(self):
        calls = {"n": 0}

        def broken(_args):
            calls["n"] += 1
            raise ConnectionError("down")

        self.reg.register(ToolContract(tool_id="ext", timeout_policy=1,
                                       retry_policy=3,
                                       side_effect_level="external"), broken)
        res = self.reg.execute("ext", {})
        self.assertFalse(res.ok)
        self.assertEqual(calls["n"], 1)
        self.assertTrue(res.side_effect_uncertain)

    def test_output_contract_violation(self):
        self.reg.register(ToolContract(
            tool_id="shape", timeout_policy=1,
            output_schema={"type": "object", "required": ["answer"]},
            side_effect_level="none"), lambda _a: _ok({"wrong": True}))
        res = self.reg.execute("shape", {})
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "contract_violation")

    def test_idempotency_journal_replays(self):
        tmp = tempfile.mkdtemp(prefix="evoagent_idem_")
        try:
            app = App(data_dir=tmp)
            calls = {"n": 0}

            def once(_args):
                calls["n"] += 1
                return _ok({"n": calls["n"]})

            registry = ToolRegistry(store=app.db)
            registry.register(ToolContract(
                tool_id="pay", timeout_policy=1, idempotency_policy="key",
                side_effect_level="external"), once)
            first = registry.execute("pay", {"amount": 1},
                                     idempotency_key="order-42")
            second = registry.execute("pay", {"amount": 1},
                                      idempotency_key="order-42")
            self.assertTrue(first.ok and second.ok)
            self.assertEqual(calls["n"], 1)
            self.assertTrue(second.evidence.get("replayed"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestWorkflowEnhancements(unittest.TestCase):
    def setUp(self):
        self.engine = WorkflowEngine(ToolRegistry(), max_parallel=2)

    def test_independent_nodes_run_in_parallel(self):
        # 两个节点必须同时到达屏障；串行执行会因等待超时而失败
        barrier = threading.Barrier(2)

        def wait_together(name):
            barrier.wait(timeout=2)
            return name

        wf = WorkflowDef(workflow_id="parallel")
        wf.nodes = [
            WorkflowNode(node_id="a", fn=lambda _c: wait_together("a")),
            WorkflowNode(node_id="b", fn=lambda _c: wait_together("b")),
        ]
        res = self.engine.run(wf, {})
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.outputs, {"a": "a", "b": "b"})

    def test_conditional_skip_does_not_block(self):
        wf = WorkflowDef(workflow_id="cond")
        wf.nodes = [
            WorkflowNode(node_id="a", fn=lambda _c: "ready"),
            WorkflowNode(node_id="b", deps=["a"], condition=lambda c: False,
                         fn=lambda _c: "unreachable"),
            WorkflowNode(node_id="c", deps=["b"], fn=lambda _c: "done"),
        ]
        res = self.engine.run(wf, {})
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.skipped, ["b"])
        self.assertEqual(res.outputs, {"a": "ready", "c": "done"})
        self.assertEqual(wf.node("b").state, WorkflowNodeState.SKIPPED)

    def test_node_timeout_fails_node(self):
        wf = WorkflowDef(workflow_id="slow")
        # 若超时未生效，运行会阻塞到 1.5s；阈值只要求明显早于节点自然结束
        wf.nodes = [WorkflowNode(node_id="a", timeout=0.05,
                                 fn=lambda _c: _sleep(1.5, "late"))]
        started = time.time()
        res = self.engine.run(wf, {})
        self.assertFalse(res.ok)
        self.assertIn("超时", res.error)
        self.assertLess(time.time() - started, 1.0)

    def test_checkpoint_resume_reuses_completed_outputs(self):
        tmp = tempfile.mkdtemp(prefix="evoagent_wfcp_")
        try:
            app = App(data_dir=tmp)
            executed = []

            def build(fail_b):
                wf = WorkflowDef(workflow_id="resume", version="1.0.0")
                wf.created_from_task = "task_wf"
                wf.nodes = [
                    WorkflowNode(node_id="a",
                                 fn=lambda _c: executed.append("a") or "A"),
                    WorkflowNode(node_id="b", deps=["a"],
                                 fn=lambda _c: _boom() if fail_b else "B"),
                ]
                return wf

            first = app.controller.workflows.run(build(True), {})
            self.assertFalse(first.ok)
            cp = app.controller.cp_mgr.latest("task_wf")
            self.assertEqual(cp.task_state["workflow_outputs"], {"a": "A"})

            second = app.controller.workflows.run(
                build(False), {}, completed=cp.task_state["workflow_outputs"])
            self.assertTrue(second.ok, second.error)
            self.assertEqual(executed, ["a"])
            self.assertEqual(second.outputs, {"a": "A", "b": "B"})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_budget_pause_marks_paused(self):
        budget = BudgetController(BudgetLimits(max_tool_calls=0))

        def exhausted():
            raise BudgetExceeded("seconds", "test")

        budget.check_alive = exhausted
        wf = WorkflowDef(workflow_id="budget")
        wf.nodes = [WorkflowNode(node_id="a", fn=lambda _c: "x")]
        res = self.engine.run(wf, {}, budget=budget)
        self.assertEqual(res.status, WorkflowNodeState.PAUSED)


class TestBudgetReport(unittest.TestCase):
    def test_ledger_report_has_scopes(self):
        tmp = tempfile.mkdtemp(prefix="evoagent_report_")
        try:
            app = App(data_dir=tmp)
            app.ledger.record("runtime", model_calls=2, tool_calls=1)
            report = app.ledger.report()
            self.assertEqual(report["runtime"]["day"]["model_calls"], 2)
            self.assertEqual(report["runtime"]["day"]["tool_calls"], 1)
            self.assertIn("month", report["runtime"])
            self.assertIn("evolution", report)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _sleep(seconds, value):
    time.sleep(seconds)
    return value


def _boom():
    raise RuntimeError("boom")


if __name__ == "__main__":
    unittest.main()
