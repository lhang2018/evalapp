# -*- coding: utf-8 -*-
"""impact_analysis 传递闭包 + 回归范围真实消费（GAP 死字段修复④，C-09）。

原状：`experts/registry.py` 单跳闭包、must_rerun_tests 硬编码
["capability_eval"]、can_reuse_evidence 布尔翻牌、全仓零调用方零测试。
现口径：
- 依赖闭包双向传递（A→B→C 上/下游闭包齐出，环安全）
- must_rerun_tests 按受影响闭包分层：触及 shared 内核 → smoke+holdout+
  安全套件；仅专家/领域层 → builtin_holdout；无受影响 → 空
- 真实调用方①：propose_candidate 落 impact_result 入候选
- 真实调用方②：_pick_dataset 选集时触及 shared 内核 → builtin_holdout
- 真实调用方③：GET /impact/ 查询端点
"""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.request

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.evolution.controller import EvolutionController
from evoagent.experts.registry import ExpertRegistry


class TestImpactClosure(unittest.TestCase):
    """闭包与分层回归范围（直接用 ExpertRegistry 手工依赖图）。"""

    def setUp(self):
        self.reg = ExpertRegistry()

        def cap(cid, domain="coding"):
            return type("C", (), {"capability_id": cid, "version": "1.0.0",
                                  "domain": domain, "description": cid,
                                  "tool_ids": []})()

        self.cap = cap
        # A(内核 shared) ← B(专家) ← C(专家)：A→B→C 链
        self.reg.register_capability(cap("A", "shared"))
        self.reg.register_capability(cap("B", "coding"))
        self.reg.register_capability(cap("C", "coding"))
        self.reg.register_capability(cap("A2", "shared"), [])
        self.reg._dep_edges["B"] = ["A"]
        self.reg._dep_edges["C"] = ["B"]

    def test_downstream_transitive_closure(self):
        out = self.reg.impact_analysis("A")
        self.assertEqual(out["directly_affected"], ["B"])      # 一跳兼容
        self.assertEqual(out["affected_closure"], ["B", "C"])  # 传递闭包含 C
        self.assertFalse(out["can_reuse_evidence"])

    def test_upstream_transitive_closure(self):
        out = self.reg.impact_analysis("C")
        self.assertEqual(out["depends_on"], ["B"])
        self.assertEqual(out["upstream_closure"], ["A", "B"])
        self.assertEqual(out["affected_closure"], [])

    def test_shared_kernel_triggers_full_regression(self):
        out = self.reg.impact_analysis("A")
        self.assertEqual(out["must_rerun_tests"],
                         ["builtin_smoke", "builtin_holdout",
                          "builtin_safety"])

    def test_expert_only_triggers_holdout(self):
        self.reg._dep_edges["E"] = ["D"]  # D ← E，D 无 shared 上游
        out = self.reg.impact_analysis("D")
        self.assertEqual(out["affected_closure"], ["E"])
        self.assertEqual(out["must_rerun_tests"], ["builtin_holdout"])

    def test_unknown_capability_empty(self):
        out = self.reg.impact_analysis("ghost")
        self.assertEqual(out["affected_closure"], [])
        self.assertEqual(out["must_rerun_tests"], [])
        self.assertTrue(out["can_reuse_evidence"])

    def test_cycle_safe(self):
        self.reg._dep_edges["A"] = ["B"]  # A→B→A 环
        out = self.reg.impact_analysis("A")
        self.assertEqual(out["affected_closure"], ["B", "C"])
        self.assertEqual(out["upstream_closure"], ["B"])


class TestImpactWiring(unittest.TestCase):
    """真实调用方①②：候选落闭包；选集按闭包分层。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_imp_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_propose_candidate_records_impact(self):
        cand = self.app.evolution.propose_candidate(
            "1.0.0", "file_toolset", "p", "h")
        self.assertIsNotNone(cand.impact_result)
        self.assertIn("coding_tasks",
                      cand.impact_result["affected_closure"])
        # 变更源即 shared 内核 → 全量回归
        self.assertEqual(cand.impact_result["must_rerun_tests"],
                         ["builtin_smoke", "builtin_holdout",
                          "builtin_safety"])

    def test_propose_without_experts_is_none(self):
        bare = EvolutionController(event_bus=None, store=None, experts=None)
        c2 = bare.propose_candidate("1.0.0", "x", "p", "h")
        self.assertIsNone(c2.impact_result)

    def test_pick_dataset_shared_kernel_prefers_holdout(self):
        cand = self.app.evolution.propose_candidate(
            "1.0.0", "file_toolset", "p", "h")   # 触及 shared 内核
        ds = self.app.evolution._pick_dataset(cand)
        self.assertEqual(ds.dataset_id, "builtin_holdout")

    def test_pick_dataset_unaffected_uses_first(self):
        cand = self.app.evolution.propose_candidate(
            "1.0.0", "expert/general", "p", "h")  # 非能力组件：闭包空
        ds = self.app.evolution._pick_dataset(cand)
        self.assertEqual(ds.dataset_id, "builtin_smoke")

    def test_pick_dataset_explicit_wins(self):
        cand = self.app.evolution.propose_candidate(
            "1.0.0", "file_toolset", "p", "h")
        ds = self.app.evolution._pick_dataset(cand, "builtin_smoke")
        self.assertEqual(ds.dataset_id, "builtin_smoke")

    def test_builtin_graph_consistency(self):
        out = self.app.experts.impact_analysis("file_toolset")
        self.assertIn("coding_tasks", out["affected_closure"])
        self.assertEqual(out["must_rerun_tests"][0], "builtin_smoke")


class TestImpactEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_imp_api_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=0)
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.app.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_endpoint_returns_closure(self):
        req = urllib.request.Request(
            self.base + "/impact/?capability_id=file_toolset")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        self.assertEqual(body["impact"]["affected_closure"], ["coding_tasks"])

    def test_endpoint_requires_capability_id(self):
        req = urllib.request.Request(self.base + "/impact/")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("应 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


if __name__ == "__main__":
    unittest.main()
