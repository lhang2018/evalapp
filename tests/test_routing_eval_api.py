"""F-53 路由质量评测 —— 真实 HTTP 端到端验证。

为什么要有这层：只测函数无法证明端点**真的被分派到**。
本测试起真实 HTTP 服务，走真实鉴权与路由链路。

判定口径同 FEATURE_SPLIT.md §5.4 DoD：
新增字段必须有产生它的代码路径，并有走通该路径的测试。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.request

from evoagent.app import App
from evoagent.api.server import ApiServer


class TestRoutingEvalApiEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_routing_eval_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return r.status, json.loads(r.read().decode())

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())

    def test_list_datasets(self):
        """GET /routing/eval 列出可用评测集。"""
        status, out = self._get("/routing/eval")
        self.assertEqual(status, 200)
        ids = {d["dataset_id"] for d in out["datasets"]}
        self.assertIn("builtin_routing", ids)
        self.assertIn("builtin_routing_holdout", ids)
        for d in out["datasets"]:
            self.assertGreater(d["case_count"], 0,
                               "评测集不得为空 —— 空集评测毫无意义")

    def test_run_eval_returns_real_metrics(self):
        """POST /routing/eval 跑出真实指标 —— 不是空壳端点。"""
        status, out = self._post("/routing/eval",
                                 {"dataset_id": "builtin_routing"})
        self.assertEqual(status, 200)
        rep = out["report"]
        self.assertEqual(rep["dataset_id"], "builtin_routing")
        self.assertGreater(rep["case_count"], 0)
        # 真实指标：内置集上准确率必须高
        self.assertGreaterEqual(rep["intent_accuracy"], 0.95,
                                f"意图准确率过低：{rep['intent_accuracy']}")
        self.assertGreaterEqual(rep["expert_accuracy"], 0.95)
        # 混淆矩阵必须有内容
        self.assertTrue(out["confusion"])
        self.assertTrue(out["per_intent"])
        self.assertTrue(out["method_counts"])
        # 五层覆盖复核随报告返回
        self.assertEqual([r["layer"] for r in out["coverage"]],
                         ["intent", "expert", "workflow",
                          "execution_mode", "model"])

    def test_coverage_endpoint(self):
        """GET /routing/coverage 返回五层覆盖矩阵与缺口。"""
        status, out = self._get("/routing/coverage")
        self.assertEqual(status, 200)
        self.assertEqual(out["order"],
                         ["intent", "expert", "workflow",
                          "execution_mode", "model"])
        layers = {r["layer"]: r for r in out["layers"]}
        self.assertTrue(layers["intent"]["evaluated"])
        self.assertFalse(layers["execution_mode"]["evaluated"])
        self.assertTrue(layers["execution_mode"]["gap"])

    def test_run_eval_reports_misclassified(self):
        """错分样本必须被列出（这是「纠错」的输入）。"""
        status, out = self._post("/routing/eval",
                                 {"dataset_id": "builtin_routing"})
        self.assertEqual(status, 200)
        # 内置集修好后应无错分
        self.assertEqual(out["misclassified"], [],
                         f"内置集仍有错分：{out['misclassified']}")

    def test_unknown_dataset_returns_404(self):
        """未知评测集必须报 404，不能静默返回空报告。"""
        try:
            status, out = self._post("/routing/eval",
                                     {"dataset_id": "no_such_dataset"})
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
            return
        self.fail(f"应报 404，实际 {status}")

    def test_reports_persisted(self):
        """评测报告必须落库并可读回 —— 否则无法做基线对比。"""
        self._post("/routing/eval", {"dataset_id": "builtin_routing"})
        status, out = self._get("/routing/reports")
        self.assertEqual(status, 200)
        self.assertTrue(out["reports"], "报告未落库")
        self.assertEqual(out["reports"][0]["dataset_id"], "builtin_routing")

    def test_holdout_dataset_runs(self):
        """保留集必须能独立评测。"""
        status, out = self._post("/routing/eval",
                                 {"dataset_id": "builtin_routing_holdout"})
        self.assertEqual(status, 200)
        self.assertTrue(out["report"]["holdout"])
        self.assertEqual(out["report"]["case_count"], 2)

    def test_existing_endpoints_unaffected(self):
        """新增端点不得影响存量端点（存量行为零变化）。"""
        status, _ = self._get("/health")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
