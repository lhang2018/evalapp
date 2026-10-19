"""工作流引擎测试（PRD 13 / Arch 16）。"""

import shutil
import tempfile
import unittest

from evoagent.core.budget import BudgetLimits
from evoagent.workflows.builtin import (comic_drama_workflow,
                                        register_comic_drama_tools)
from evoagent.workflows.engine import WorkflowEngine
from evoagent.workflows.models import WorkflowDef, WorkflowNode, WorkflowNodeState
from evoagent.tools.registry import ToolRegistry


class TestWorkflow(unittest.TestCase):
    def setUp(self):
        self.tools = ToolRegistry()
        from evoagent.tools.builtin import BUILTIN_FACTORIES
        for factory in BUILTIN_FACTORIES.values():
            contract, runner = factory()
            self.tools.register(contract, runner)
        register_comic_drama_tools(self.tools)
        self.engine = WorkflowEngine(self.tools)

    def test_comic_drama_success(self):
        wf = comic_drama_workflow("测试主题")
        out = self.engine.run(wf, {"goal": "生成30s漫剧"})
        self.assertTrue(out.ok, out.error)
        self.assertIn("compose", out.outputs)
        self.assertEqual(wf.status, WorkflowNodeState.SUCCESS)
        # DAG 依赖：clip 依赖 script/storyboard（顺序由拓扑保证）
        stats = {n["node"]: n for n in out.node_stats}
        self.assertEqual(stats["clip"]["state"], "success")

    def test_node_retry_and_failure(self):
        from evoagent.tools.contract import ToolContract, ToolResult
        def flaky(args):
            return ToolResult.ok if args.get("go") else ToolResult(ok=False, error="boom")
        self.tools.register(ToolContract(tool_id="flaky",
                                         input_schema={"type": "object"}),
                            lambda a: (_ for _ in ()).throw(RuntimeError("always fail")))
        wf = WorkflowDef(workflow_id="wf_fail", nodes=[
            WorkflowNode(node_id="n1", tool="flaky", max_retries=2)])
        out = self.engine.run(wf, {})
        self.assertFalse(out.ok)
        self.assertIn("n1", out.error)
        self.assertGreaterEqual(out.retries_used, 3)  # 初次+2重试

    def test_dep_reference(self):
        wf = WorkflowDef(workflow_id="wf_ref", nodes=[
            WorkflowNode(node_id="a", tool="mock_script_gen",
                         args={"theme": "T"}),
            WorkflowNode(node_id="b", tool="mock_video_clip",
                         deps=["a"], args={"scene": 1}),
        ])
        out = self.engine.run(wf, {})
        self.assertTrue(out.ok)

    def test_cyclic_rejected(self):
        wf = WorkflowDef(workflow_id="wf_cyc", nodes=[
            WorkflowNode(node_id="a", tool="mock_script_gen", deps=["b"],
                         args={"theme": "T"}),
            WorkflowNode(node_id="b", tool="mock_script_gen", deps=["a"],
                         args={"theme": "T"}),
        ])
        out = self.engine.run(wf, {})
        self.assertFalse(out.ok)
        self.assertIn("定义错误", out.error)

    def test_requires_confirmation_pauses(self):
        wf = WorkflowDef(workflow_id="wf_auth", nodes=[
            WorkflowNode(node_id="risky", tool="mock_compose",
                         requires_confirmation=True)])
        out_no = self.engine.run(wf, {"goal": "x"}, confirm=lambda n: False)
        self.assertEqual(out_no.status, WorkflowNodeState.WAITING_AUTH)
        wf2 = WorkflowDef(workflow_id="wf_auth2", nodes=[
            WorkflowNode(node_id="risky", tool="mock_compose",
                         requires_confirmation=True)])
        out_ok = self.engine.run(wf2, {"goal": "x"}, confirm=lambda n: True)
        self.assertTrue(out_ok.ok)

    def test_budget_pause(self):
        from evoagent.core.budget import BudgetController
        wf = comic_drama_workflow("主题")
        b = BudgetController(BudgetLimits(max_tool_calls=1, max_model_calls=5,
                                          max_cost=99, max_seconds=60))
        out = self.engine.run(wf, {"goal": "x"}, budget=b)
        self.assertEqual(out.status, WorkflowNodeState.PAUSED)


if __name__ == "__main__":
    unittest.main()
