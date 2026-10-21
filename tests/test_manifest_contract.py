# -*- coding: utf-8 -*-
"""Runtime Manifest 契约字段批（GAP ⑤c，契约 §5）。

补齐八个缺口：runtime_adapter{name,version}、model_profile_ref、
router_policy_ref、context_policy_ref、expert_ref、evaluation_suite_ref、
environment_digest(sha256)、tool_versions[]（对象数组），以及
workflow_ref 契约形态（{id,version} 对象或 null）。

真实产生链：
- 注册表 version_inventory → tool_versions[]
- manifest_extras（evolution runner 传数据集 id/version）→ 快照 merge
  → run manifest evaluation_suite_ref
- _run_react 专家解析 → expert_ref
- execute 内 wf_lookup 核对 → workflow_ref 对象
"""

import datetime
import re
import shutil
import tempfile
import unittest

import evoagent
import evoagent.core.task as T
from evoagent.app import App
from evoagent.evolution.evaluator import EvalCase
from evoagent.evolution.runner import ControllerTaskRunner
from evoagent.workflows.models import WorkflowDef, WorkflowNode


CONTRACT_KEYS = (
    "runtime_adapter", "model_profile_ref", "router_policy_ref",
    "context_policy_ref", "expert_ref", "evaluation_suite_ref",
    "environment_digest", "tool_versions", "workflow_ref",
    "core_version", "created_at",
)


class TestManifestContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_man5c_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_simple_task(self):
        return self.app.submit(
            "计算 2024 + 2000 + 500", wait=True)

    def test_contract_fields_landed(self):
        task = self._run_simple_task()
        vm = task.version_manifest
        for k in CONTRACT_KEYS:
            self.assertIn(k, vm, k)
        self.assertEqual(vm["core_version"], evoagent.__version__)
        # created_at: RFC3339 可解析
        datetime.datetime.fromisoformat(vm["created_at"])
        # runtime_adapter 对象形态
        ra = vm["runtime_adapter"]
        self.assertIsInstance(ra, dict)
        self.assertIn(ra["name"], ("mock", "openai_compat"))
        self.assertTrue(ra["version"])
        # *_ref 形态 id/version
        self.assertIn(self.app.config.provider.model_id,
                      vm["model_profile_ref"])
        self.assertRegex(vm["router_policy_ref"], r"^adapter/.+")
        self.assertRegex(vm["context_policy_ref"], r"^env/.+")

    def test_environment_digest_shape_and_stability(self):
        vm1 = self.app.controller._version_manifest()
        vm2 = self.app.controller._version_manifest()
        self.assertRegex(vm1["environment_digest"], r"^[0-9a-f]{64}$")
        # 稳定事实指纹：同事实重复组装摘要一致（created_at 不参与）
        self.assertEqual(vm1["environment_digest"],
                         vm2["environment_digest"])

    def test_tool_versions_object_array(self):
        task = self._run_simple_task()
        inv = task.version_manifest["tool_versions"]
        self.assertIsInstance(inv, list)
        self.assertTrue(inv, "注册表应有工具")
        for row in inv:
            self.assertIsInstance(row, dict)
            self.assertTrue(row["tool_id"])
            self.assertTrue(row["version"])
        ids = {row["tool_id"] for row in inv}
        self.assertIn("current_time", ids)
        self.assertIn("calculate", ids)

    def test_expert_ref_stamped_after_routing(self):
        task = self._run_simple_task()
        ref = (task.runtime_manifest or {}).get("expert_ref")
        self.assertTrue(ref, "expert_ref 应在专家解析后落值")
        # 形态 expert_id/version，且版本锚=发布版本
        parts = ref.split("/", 1)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[1],
                         task.version_manifest["version"])
        self.assertNotEqual(parts[1], "")

    def test_evaluation_suite_ref_chain(self):
        runner = ControllerTaskRunner(self.app.controller,
                                      ledger=self.app.ledger)
        runner.suite_ref = "builtin_smoke/1.0.0"
        case = EvalCase(case_id="man5c_case", input_goal="计算 2+3",
                        intent="computation", domain="general",
                        expected=[{"type": "tool_goal_reached"}])
        ok, latency, evid = runner.run_case(case)
        self.assertTrue(ok, evid)
        task = self.app.db.get_task(evid["task_id"])
        self.assertEqual((task.runtime_manifest or {})
                         .get("evaluation_suite_ref"),
                         "builtin_smoke/1.0.0")
        # 快照 merge 后 run manifest 也带套件引用
        self.assertEqual(task.version_manifest["evaluation_suite_ref"],
                         "builtin_smoke/1.0.0")

    def test_evaluate_candidate_sets_suite_ref(self):
        cand = self.app.evolution.propose_candidate(
            "1.0.0", "expert/general", "p", "h")
        self.app.evolution.evaluate_candidate(cand.candidate_id)
        self.assertEqual(self.app.evaluator.runner.suite_ref,
                         "builtin_smoke/1.0.0")

    def test_workflow_ref_object_shape(self):
        if "wf_man5c" not in self.app._workflows:
            self.app._workflows["wf_man5c"] = WorkflowDef(
                workflow_id="wf_man5c", version="2.5.0",
                name="清单契约测试流", nodes=[
                    WorkflowNode(node_id="n1", tool="calculate",
                                 args={"expression": "1+1"})])
        task = T.Task(task_id="t_man5c_wf", user_goal="跑清单测试流")
        task.workflow_ref = "wf_man5c"
        self.app.db.save_task(task)
        out = self.app.controller.execute(task)
        vm = out.version_manifest
        self.assertIsInstance(vm["workflow_ref"], dict)
        self.assertEqual(vm["workflow_ref"]["id"], "wf_man5c")
        self.assertEqual(vm["workflow_ref"]["version"], "2.5.0")

    def test_workflow_ref_null_without_workflow(self):
        task = self._run_simple_task()
        self.assertIsNone(task.version_manifest["workflow_ref"])


if __name__ == "__main__":
    unittest.main()
