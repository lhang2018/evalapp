"""模型辅助诊断假设测试（Arch 9.4：解释不等于根因）。"""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from evoagent.acceptance.verify import Verdict
from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.task import Task
from evoagent.evolution.diagnostics import (CaseStatus, DiagnosticExperiment,
                                            FaultCategory, HypothesisStatus)
from evoagent.models.base import ModelProfile
from evoagent.tools.contract import ToolResult


class FakeModel:
    def __init__(self, output="[]"):
        self.output = output
        self.calls = []

    def profile(self):
        return ModelProfile(model_id="fake-diag", provider="test")

    def generate(self, messages):
        self.calls.append(messages)
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def _failed():
    return ToolResult(ok=False, error="timeout error", error_class="timeout",
                      submitted=True, executed=True, contract_valid=True,
                      evidence={"tool": "http_get"})


def _ok():
    return ToolResult(ok=True, data="ok", submitted=True, executed=True,
                      contract_valid=True, goal_reached=True,
                      evidence={"tool": "current_time"})


_TIMEOUT_JSON = json.dumps([{
    "category": "TOOL", "component": "tool/http_get/retry",
    "statement": "重试策略未覆盖瞬时超时",
    "evidence_refs": ["tool=http_get error_class=timeout"],
    "suggested_probes": ["tool_health", "not_a_probe"],
    "confidence": 0.95,
}], ensure_ascii=False)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_assist_")
        self.app = App(data_dir=self.tmp)
        self.engine = self.app.diagnostics
        self.model = FakeModel(_TIMEOUT_JSON)
        self.engine.model = self.model
        task = Task(user_goal="诊断辅助测试",
                    version_manifest={"version": "v1"})
        self.case = self.engine.record_failure(
            task, Verdict(passed=False, checks=[]), [_failed(), _ok()])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _suggest(self, output=None, limit=None):
        if output is not None:
            self.model.output = output
        return self.engine.suggest_hypotheses(self.case.case_id,
                                              max_items=limit)


class TestModelHypotheses(Base):
    def test_hypothesis_is_advisory_and_confidence_capped(self):
        before = self.app.db.get_diagnostic_case(self.case.case_id).status
        out = self._suggest()
        self.assertTrue(out["advisory"])
        self.assertEqual(len(out["created"]), 1)
        h = out["created"][0]
        self.assertEqual(h["source"], "model")
        self.assertEqual(h["confidence"], 0.5)
        self.assertEqual(h["status"], HypothesisStatus.PROPOSED.value)
        self.assertEqual(h["suggested_probes"], ["tool_health"])
        self.assertTrue(h["supporting_evidence"][0].startswith("引用证据:"))
        # 模型输出不改变案例状态（不是根因判定）
        self.assertEqual(
            self.app.db.get_diagnostic_case(self.case.case_id).status, before)
        self.assertEqual(
            self.app.db.get_diagnostic_case(self.case.case_id).status,
            CaseStatus.DIAGNOSED)

    def test_evidence_refs_must_exist_in_snapshot(self):
        out = self._suggest(json.dumps([{
            "category": "AGENT", "component": "planning/invented",
            "statement": "机房着火了",
            "evidence_refs": ["机房着火"],
            "confidence": 0.9,
        }], ensure_ascii=False))
        self.assertEqual(out["created"], [])
        self.assertEqual(out["skipped"][0]["reason"],
                         "evidence_refs_not_in_snapshot")
        self.assertEqual(len(self.app.db.list_hypotheses(self.case.case_id)),
                         2)  # 仅规则生成的两条

    def test_duplicate_rule_component_is_skipped(self):
        out = self._suggest(json.dumps([{
            "category": "TOOL", "component": "tool/http_get/timeout",
            "statement": "工具超时",
            "evidence_refs": ["timeout"],
        }], ensure_ascii=False))
        self.assertEqual(out["created"], [])
        self.assertEqual(out["skipped"][0]["reason"], "duplicate")

    def test_invalid_json_does_not_raise(self):
        out = self._suggest("抱歉，我无法给出结构化结果。")
        self.assertEqual(out["created"], [])
        self.assertEqual(out["skipped"][0]["reason"], "invalid_json")

    def test_model_error_does_not_break_diagnosis(self):
        out = self._suggest(RuntimeError("gateway down"))
        self.assertEqual(out["created"], [])
        self.assertTrue(out["skipped"][0]["reason"].startswith("model_error:"))

    def test_no_model_raises(self):
        self.engine.model = None
        with self.assertRaises(RuntimeError):
            self.engine.suggest_hypotheses(self.case.case_id)

    def test_max_items_is_bounded(self):
        items = [{
            "category": "AGENT", "component": f"component/{i}",
            "statement": f"解释 {i}", "evidence_refs": ["timeout"],
        } for i in range(6)]
        out = self._suggest(json.dumps(items, ensure_ascii=False), limit=99)
        self.assertEqual(len(out["created"]), 5)

    def test_promotion_requires_probe_verification(self):
        h = self._suggest()["created"][0]
        with self.assertRaises(ValueError):
            self.engine.promote_to_candidate(self.case, h["hypothesis_id"])
        self.app.db.save_experiment(DiagnosticExperiment(
            case_id=self.case.case_id, hypothesis_id=h["hypothesis_id"],
            method="tool_health", result="fail",
            evidence={"reason": "injected"}))
        cand = self.engine.promote_to_candidate(self.case,
                                               h["hypothesis_id"])
        self.assertTrue(cand.candidate_id)


class TestAssistApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_assist_api_")
        cls.app = App(data_dir=cls.tmp)
        cls.model = FakeModel(_TIMEOUT_JSON)
        cls.app.diagnostics.model = cls.model
        task = Task(user_goal="API 辅助假设",
                    version_manifest={"version": "v1"})
        cls.case = cls.app.diagnostics.record_failure(
            task, Verdict(passed=False, checks=[]), [_failed(), _ok()])
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=18302)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:18302"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_suggest_endpoint_returns_advisory(self):
        st, out = self._post(
            f"/diagnostics/{self.case.case_id}/hypotheses", {})
        self.assertEqual(st, 200)
        self.assertTrue(out["advisory"])
        self.assertEqual(out["model_id"], "fake-diag")
        self.assertEqual(out["created"][0]["source"], "model")

    def test_unknown_case_404(self):
        st, out = self._post("/diagnostics/nope/hypotheses", {})
        self.assertEqual(st, 404)

    def test_missing_model_503(self):
        saved = self.app.diagnostics.model
        self.app.diagnostics.model = None
        try:
            st, out = self._post(
                f"/diagnostics/{self.case.case_id}/hypotheses", {})
        finally:
            self.app.diagnostics.model = saved
        self.assertEqual(st, 503)


if __name__ == "__main__":
    unittest.main()
