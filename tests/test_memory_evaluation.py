"""记忆质量评测与运行反馈（PRD 28 / Arch 8.2）。

覆盖内置用例指标、缺样本标注、运行收益估计，以及 App/API 持久化接线。
"""

import json
import shutil
import tempfile
import threading
import types
import unittest
import urllib.request

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.memory.evaluation import (MemoryEvalCase,
                                       builtin_memory_cases,
                                       MemoryEvaluator)


class TestMemoryEvaluator(unittest.TestCase):
    def test_builtin_metrics(self):
        report = MemoryEvaluator().evaluate()
        self.assertEqual(report["case_count"], len(builtin_memory_cases()))
        self.assertEqual(report["write_accuracy"], 1.0)
        self.assertEqual(report["recall"], 1.0)
        self.assertEqual(report["precision"], 1.0)
        self.assertEqual(report["mrr"], 1.0)
        self.assertEqual(report["expired_misuse_rate"], 0.0)
        self.assertEqual(report["conflict_correctness"], 1.0)
        self.assertEqual(report["thresholds"]["min_write_accuracy"], 0.95)

    def test_expired_item_excluded_from_recall(self):
        case = MemoryEvalCase(
            case_id="expired", query="旧部署脚本",
            items=[{"memory_id": "m_old", "content": "旧部署脚本仍在使用",
                    "kind": "project_state", "scope": "project",
                    "status": "verified",
                    "valid_until": 1.0}],
            expect_absent=["m_old"])
        report = MemoryEvaluator().evaluate([case])
        self.assertEqual(report["expired_misuse_rate"], 0.0)
        self.assertEqual(report["cases"][0]["retrieved"], [])

    def test_conflict_correctness_failure_is_reported(self):
        case = MemoryEvalCase(
            case_id="bad_conflict", query="默认导出格式",
            items=[
                {"memory_id": "m_pdf", "content": "默认导出格式是 PDF",
                 "kind": "project_state", "scope": "project",
                 "status": "verified", "evidence": {"task_id": "t1"}},
                {"memory_id": "m_csv", "content": "默认导出格式是 CSV",
                 "kind": "project_state", "scope": "project",
                 "status": "verified", "evidence": {"task_id": "t2"}},
            ],
            expected=["m_csv"], expect_absent=["m_pdf"], conflict=True)
        report = MemoryEvaluator().evaluate([case])
        self.assertEqual(report["conflict_correctness"], 0.0)
        self.assertFalse(report["cases"][0]["conflict_correct"])

    def test_runtime_feedback_insufficient_evidence(self):
        out = MemoryEvaluator().runtime_feedback([])
        self.assertEqual(out["memory_benefit"]["status"],
                         "insufficient_evidence")
        self.assertIsNone(out["memory_benefit"]["value"])
        self.assertEqual(out["memory_caused_error_rate"]["status"],
                         "insufficient_evidence")

    def test_runtime_feedback_measured(self):
        def task(success, used_memory):
            return types.SimpleNamespace(
                task_status=types.SimpleNamespace(
                    value="SUCCEEDED" if success else "FAILED"),
                runtime_manifest={"context": {"memory_used": used_memory}})

        tasks = ([task(True, 1) for _ in range(3)]
                 + [task(False, 1)]
                 + [task(False, 0) for _ in range(3)])
        feedback = [{"outcome": "helpful"}, {"outcome": "harmful"},
                    {"outcome": "misleading"}, {"outcome": "neutral"}]
        out = MemoryEvaluator().runtime_feedback(tasks, feedback=feedback)
        self.assertEqual(out["memory_benefit"]["status"], "measured")
        self.assertAlmostEqual(out["memory_benefit"]["value"], 0.75,
                               places=4)
        self.assertEqual(out["memory_caused_error_rate"]["harmful"], 2)
        self.assertAlmostEqual(out["memory_caused_error_rate"]["value"], 0.5,
                               places=4)


class TestMemoryEvalAppApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_memeval_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=18304)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:18304"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.app.db.close()
        cls.app.memory.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def test_run_and_list_reports(self):
        st, report = self._post("/memory/evaluate", {})
        self.assertEqual(st, 200)
        self.assertEqual(report["recall"], 1.0)
        self.assertIn("runtime_feedback", report)
        listed = self._get("/memory/evaluations?limit=5")["evaluations"]
        self.assertTrue(any(r["report_id"] == report["report_id"]
                            for r in listed))
        snap = self.app.metrics.snapshot()
        self.assertEqual(snap["memory"]["evaluation"]["recall"], 1.0)
        self.assertIn("evoagent_memory_eval_recall 1.0",
                      self.app.metrics.prometheus())


if __name__ == "__main__":
    unittest.main()
