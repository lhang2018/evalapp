"""诊断闭环测试（PRD 41-45 / Architecture 9）。"""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from evoagent.acceptance.verify import Verdict
from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.task import Task
from evoagent.evolution.controller import PUBLISH_RULES
from evoagent.evolution.diagnostics import (CaseStatus, FaultCategory, Hypothesis)
from evoagent.tools.contract import ToolResult


def _failed(tool="http_get", error_class="timeout", executed=True,
            contract_valid=True, side_effect_uncertain=False) -> ToolResult:
    return ToolResult(ok=False, error=f"{error_class} error",
                      error_class=error_class, submitted=executed,
                      executed=executed, contract_valid=contract_valid,
                      side_effect_uncertain=side_effect_uncertain,
                      evidence={"tool": tool})


def _ok(tool="current_time") -> ToolResult:
    return ToolResult(ok=True, data="ok", submitted=True, executed=True,
                      contract_valid=True, goal_reached=True,
                      independently_verified=True, evidence={"tool": tool})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_diag_")
        self.app = App(data_dir=self.tmp)
        self.engine = self.app.diagnostics

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _case(self, *results, verdict=None, stop_reason="", task=None):
        task = task or Task(user_goal="诊断测试", version_manifest={"version": "v9"})
        return self.engine.record_failure(
            task, verdict or Verdict(passed=False, checks=[]),
            list(results), stop_reason=stop_reason)


class TestDiagnosisRules(Base):
    def test_timeout_generates_env_and_tool_hypotheses(self):
        case = self._case(_failed(), _ok())
        self.assertEqual(case.status, CaseStatus.DIAGNOSED)
        hyps = self.app.db.list_hypotheses(case.case_id)
        cats = {h.category for h in hyps}
        self.assertIn(FaultCategory.ENVIRONMENT, cats)
        self.assertIn(FaultCategory.TOOL, cats)
        self.assertTrue(all(h.supporting_evidence for h in hyps))
        self.assertTrue(any(h.opposing_evidence for h in hyps))

    def test_auth_failure_generates_env_and_agent(self):
        case = self._case(_failed(error_class="auth_or_permission"))
        hyps = self.app.db.list_hypotheses(case.case_id)
        pairs = {(h.category, h.component) for h in hyps}
        self.assertIn((FaultCategory.ENVIRONMENT, "permissions/system"), pairs)
        self.assertIn((FaultCategory.AGENT, "agent/permissions"), pairs)

    def test_budget_failure_capped_confidence(self):
        case = self._case(_failed(error_class="budget_exceeded"))
        hyps = self.app.db.list_hypotheses(case.case_id)
        components = {h.component for h in hyps}
        self.assertIn("react/loop", components)
        self.assertIn("harness/budget", components)
        self.assertTrue(all(0.0 < h.confidence <= 0.9 for h in hyps))

    def test_repeated_failure_generates_recovery_hypothesis(self):
        case = self._case(_failed(error_class="unknown"),
                          stop_reason="repeated_failure")
        hyps = self.app.db.list_hypotheses(case.case_id)
        self.assertTrue(any(h.component == "react/recovery" for h in hyps))

    def test_acceptance_mismatch_when_all_tools_ok(self):
        case = self._case(_ok(), _ok("calculate"))
        hyps = self.app.db.list_hypotheses(case.case_id)
        components = {h.component for h in hyps}
        self.assertIn("planning/acceptance", components)
        self.assertIn("harness/verifier", components)

    def test_no_tool_failure_generates_planning_hypothesis(self):
        case = self._case()
        hyps = self.app.db.list_hypotheses(case.case_id)
        self.assertEqual([h.component for h in hyps], ["planning/classifier"])

    def test_successful_task_has_no_case(self):
        t = self.app.submit("现在几点了", intent="time_query",
                            acceptance=[{"type": "tool_succeeded"}])
        self.assertEqual(t.task_status.value, "SUCCEEDED")
        self.assertIsNone(self.app.db.get_case_by_task(t.task_id))

    def test_runtime_failure_creates_persisted_case(self):
        self.app.permissions.high_risk_mode = "deny"
        t = self.app.submit("写笔记 path=notes/x.txt content=abc",
                            intent="note_taking",
                            acceptance=[{"type": "tool_succeeded"}])
        self.assertEqual(t.task_status.value, "FAILED")
        case = self.app.db.get_case_by_task(t.task_id)
        self.assertIsNotNone(case)
        self.assertEqual(t.runtime_manifest["diagnostic_case_id"], case.case_id)
        self.assertTrue(self.app.db.list_hypotheses(case.case_id))


class TestProbesAndExperiments(Base):
    def test_fixed_probes_are_bounded(self):
        calc = self.engine.probe_runner.run("calc_roundtrip")
        self.assertTrue(calc.passed)
        fw = self.engine.probe_runner.run("file_write_sandbox")
        self.assertTrue(fw.passed)
        self.assertEqual(fw.evidence["sandbox"], "temporary")
        self.assertFalse(Path("probe.txt").exists())
        self.assertIsNone(self.engine.probe_runner.run("no_such_probe").passed)

    def test_experiment_limit_per_case(self):
        case = self._case(_failed())
        hyp = self.app.db.list_hypotheses(case.case_id)[0]
        for _ in range(self.app.config.diagnostics.max_experiments_per_case):
            self.engine.run_experiment(case, hyp, "tool_health")
        with self.assertRaises(PermissionError):
            self.engine.run_experiment(case, hyp, "tool_health")
        self.assertEqual(
            len(self.app.db.list_experiments(case.case_id)),
            self.app.config.diagnostics.max_experiments_per_case)

    def test_hypothesis_promotion_core_is_l4(self):
        case = self._case(_failed())
        hyp = Hypothesis(case_id=case.case_id, category=FaultCategory.COMPOSITE,
                         component="core/safety", statement="核心安全策略缺陷",
                         supporting_evidence=["探针失败"], confidence=0.7)
        self.app.db.save_hypothesis(hyp)
        cand = self.engine.promote_to_candidate(case, hyp.hypothesis_id)
        self.assertEqual(cand.risk_level, "L4")
        self.assertEqual(PUBLISH_RULES["L4"], "manual_only")
        self.assertEqual(cand.rollback_version, "v9")
        self.assertEqual(self.app.db.get_diagnostic_case(case.case_id).candidate_id,
                         cand.candidate_id)


class TestDiagnosticsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_diag_api_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    def _post(self, path, payload):
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def test_full_diagnostic_chain(self):
        self.app.permissions.high_risk_mode = "deny"
        t = self.app.submit("写笔记 path=notes/api.txt content=abc",
                            intent="note_taking",
                            acceptance=[{"type": "tool_succeeded"}])
        self.assertEqual(t.task_status.value, "FAILED")
        st, detail = self._post("/diagnostics/scan", {"task_id": t.task_id})
        self.assertEqual(st, 200)
        case_id = detail["case"]["case_id"]
        # 幂等
        st2, detail2 = self._post("/diagnostics/scan", {"task_id": t.task_id})
        self.assertEqual(detail2["case"]["case_id"], case_id)
        listed = self._get("/diagnostics?limit=10")["cases"]
        self.assertTrue(any(c["case_id"] == case_id for c in listed))
        got = self._get(f"/diagnostics/{case_id}")
        self.assertTrue(got["hypotheses"])
        hyp_id = got["hypotheses"][0]["hypothesis_id"]
        st, exp = self._post(f"/diagnostics/{case_id}/experiments",
                             {"method": "tool_health",
                              "hypothesis_id": hyp_id})
        self.assertEqual(st, 200)
        self.assertIn(exp["experiment"]["result"],
                      ("pass", "fail", "inconclusive"))
        st, cand = self._post(f"/diagnostics/{case_id}/candidates",
                              {"hypothesis_id": hyp_id})
        self.assertEqual(st, 201)
        self.assertTrue(cand["candidate_id"])
        self.assertEqual(self.app.db.get_diagnostic_case(case_id).candidate_id,
                         cand["candidate_id"])

    def test_unknown_case_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/diagnostics/nope")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
