# -*- coding: utf-8 -*-
"""E-12 失败案例→回归资产 + E-13 实验编排（队列/预算/并发/停止条件）。

E-12 转换规则（contracts REGRESSION_*）：仅 DIAGNOSED 可固化；
OPEN/INCONCLUSIVE、源任务缺失 → unknown 不转换；一案一案幂等。
E-13 停止条件（contracts EVOLUTION_STOP_*）：预算耗尽/无证据/并发上限。
"""

import shutil
import tempfile
import unittest

from evoagent.acceptance.verify import Verdict
from evoagent.app import App
from evoagent.core import contracts as C
from evoagent.core.task import Task
from evoagent.evolution.candidates import CandidateStatus
from evoagent.evolution.diagnostics import CaseStatus, DiagnosticCase
from evoagent.tools.contract import ToolResult


def _failed() -> ToolResult:
    return ToolResult(ok=False, error="timeout error", error_class="timeout",
                      submitted=True, executed=True, contract_valid=True,
                      side_effect_uncertain=False, evidence={"tool": "http_get"})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_e12e13_")
        self.app = App(data_dir=self.tmp)
        self.evolution = self.app.evolution
        self.diag = self.app.diagnostics

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _failed_diag_case(self, tid: str, domain: str = "photo_edit"):
        task = Task(task_id=tid, user_goal="导出相册到备份盘", domain=domain)
        task.acceptance_criteria = [{"type": "result_nonempty"}]
        self.app.db.save_task(task)
        case = self.diag.record_failure(
            task, Verdict(passed=False, checks=[]), [_failed()], stop_reason="")
        return case, task

    def _unknown_diag_case(self, tid: str):
        task = Task(task_id=tid, user_goal="无证据案例")
        self.app.db.save_task(task)
        # 无 verdict、无工具结果、无停止原因 → 无规则命中 → 兜底假设
        # confidence=0.30 → INCONCLUSIVE（诊断未收敛）
        return self.diag.record_failure(task, None, [], stop_reason="")


class TestE12RegressionAssets(Base):
    def test_diagnosed_case_becomes_idempotent_regression_case(self):
        case, task = self._failed_diag_case("t_e12a")
        self.assertEqual(case.status, CaseStatus.DIAGNOSED)
        out = self.evolution.regression_builder.convert_pending()
        self.assertEqual(out["converted"], [f"reg_{case.case_id}"])
        ds = self.evolution.datasets[C.REGRESSION_DATASET_ID]
        asset = ds.cases[0]
        self.assertEqual(asset.case_id, f"reg_{case.case_id}")
        self.assertEqual(asset.input_goal, task.user_goal)
        # expected = 源任务验收（回归即重验原验收）
        self.assertEqual(asset.expected, task.acceptance_criteria)
        self.assertEqual(asset.domain, task.domain)
        self.assertFalse(asset.holdout)
        self.assertIn(C.REGRESSION_TAG, asset.tags)
        self.assertIn(f"diagnostic_case/{case.case_id}", asset.tags)
        self.assertIn(f"task/{task.task_id}", asset.tags)
        # 幂等：重复执行不产生重复用例
        out2 = self.evolution.regression_builder.convert_pending()
        self.assertEqual(out2["converted"], [])
        self.assertEqual(out2["already"], 1)
        self.assertEqual(len(ds.cases), 1)

    def test_unknown_evidence_not_converted(self):
        case = self._unknown_diag_case("t_e12b")
        self.assertEqual(case.status, CaseStatus.INCONCLUSIVE)
        out = self.evolution.regression_builder.convert_pending()
        self.assertEqual(out["converted"], [])
        self.assertIn(case.case_id, out["skipped_unknown"])
        self.assertNotIn(C.REGRESSION_DATASET_ID, self.evolution.datasets)

    def test_missing_source_task_not_converted(self):
        # 案已建但源任务读取不到 → 无法复现目标（证据不足不转换）
        case = DiagnosticCase(case_id="ghost", task_id="t_never_saved",
                              symptom="幽灵案")
        case.status = CaseStatus.DIAGNOSED
        self.app.db.save_diagnostic_case(case)
        out = self.evolution.regression_builder.convert_pending()
        self.assertEqual(out["converted"], [])
        self.assertIn("ghost", out["skipped_unknown"])
        self.assertNotIn(C.REGRESSION_DATASET_ID, self.evolution.datasets)


class TestE13EvolutionRound(Base):
    def test_full_round_converts_proposes_evaluates(self):
        case, _ = self._failed_diag_case("t_e13a")
        report = self.evolution.run_evolution_round()
        # E-12 转换先行
        self.assertEqual(report["converted"], [f"reg_{case.case_id}"])
        # E-07 提案 + E-13 受评（门禁独立裁决：候选不再是 CANDIDATE_CREATED）
        self.assertEqual(len(report["proposed"]), 1)
        self.assertEqual(len(report["evaluated"]), 1)
        cand = self.evolution.candidates[report["proposed"][0]]
        self.assertNotEqual(cand.status, CandidateStatus.CANDIDATE_CREATED)
        # 第二轮：无新证据 → 记停止原因，不重复评测
        report2 = self.evolution.run_evolution_round()
        self.assertIn(C.EVOLUTION_STOP_NO_PENDING_EVIDENCE,
                      report2["stop_reasons"])
        self.assertEqual(report2["proposed"], [])
        self.assertEqual(report2["evaluated"], [])

    def test_budget_exhausted_stops_round_immediately(self):
        self._failed_diag_case("t_e13b")
        orig = self.evolution._evolution_pool_problem
        self.evolution._evolution_pool_problem = lambda: "日池耗尽"
        try:
            report = self.evolution.run_evolution_round()
        finally:
            self.evolution._evolution_pool_problem = orig
        self.assertTrue(any(r.startswith(C.EVOLUTION_STOP_BUDGET_EXHAUSTED)
                            for r in report["stop_reasons"]))
        self.assertEqual(report["converted"], [])
        self.assertEqual(report["proposed"], [])
        self.assertEqual(report["evaluated"], [])

    def test_concurrency_cap_blocks_new_evaluation(self):
        # 两个进行中候选占满默认并发额度（=1）
        for comp in ("expert/a", "expert/b"):
            cand = self.evolution.propose_candidate("1.0.0", comp, "p", "h")
            cand.transition(CandidateStatus.TESTING, "构造占位进行中")
        self.assertEqual(self.evolution.max_concurrent_evaluations, 1)
        report = self.evolution.run_evolution_round(max_evaluations=2)
        self.assertIn(C.EVOLUTION_STOP_CONCURRENCY_LIMIT,
                      report["stop_reasons"])
        self.assertEqual(report["evaluated"], [])

    def test_clean_round_reports_no_pending_evidence(self):
        report = self.evolution.run_evolution_round()
        self.assertIn(C.EVOLUTION_STOP_NO_PENDING_EVIDENCE,
                      report["stop_reasons"])
        self.assertEqual(report["converted"], [])
        self.assertEqual(report["proposed"], [])
        self.assertEqual(report["evaluated"], [])


if __name__ == "__main__":
    unittest.main()
