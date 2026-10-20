"""层级预算（PRD 55-56 / Arch 12）与成本报告（PRD 58）测试。"""

import shutil
import tempfile
import unittest

from evoagent.config import BudgetConfig
from evoagent.core.budget import (BudgetController, BudgetExceeded,
                                  BudgetLedger, BudgetLimits, CostReporter)
from evoagent.core.persistence import Database
from evoagent.core.task import Task, TaskStatus
from evoagent.evolution.controller import EvolutionController
from evoagent.evolution.evaluator import (EvalCase, EvalDataset, Evaluator)
from evoagent.evolution.runner import ControllerTaskRunner


class TestHierarchicalBudget(unittest.TestCase):
    def test_single_model_call_cost_blocks_next_call(self):
        b = BudgetController(BudgetLimits(
            max_model_calls=10, max_cost_per_model_call=0.1))
        b.record_model_call(0.2, "m1")
        with self.assertRaises(BudgetExceeded) as ctx:
            b.check_model_call()
        self.assertEqual(ctx.exception.dimension, "model_call_cost")
        self.assertIn("model_call_cost", b.snapshot()["violations"])

    def test_per_tool_call_limit_and_override(self):
        limits = BudgetLimits(
            max_tool_calls=10, max_tool_calls_per_tool=5,
            tool_limits={"search": {"max_calls": 2, "max_cost": 0.05}})
        b = BudgetController(limits)
        b.record_tool_call("search")
        b.record_tool_call("search")
        with self.assertRaises(BudgetExceeded) as ctx:
            b.check_tool_call("search")
        self.assertEqual(ctx.exception.dimension, "tool_calls:search")
        # 其他工具仍可执行
        b.check_tool_call("calculate")

    def test_per_tool_cost_violation(self):
        limits = BudgetLimits(tool_limits={
            "search": {"max_calls": 10, "max_cost": 0.05}})
        b = BudgetController(limits)
        b.record_tool_call("search", 0.1)
        with self.assertRaises(BudgetExceeded) as ctx:
            b.check_tool_call("search")
        self.assertEqual(ctx.exception.dimension, "tool_cost:search")

    def test_workflow_scope_checks_local_and_parent(self):
        ledger = BudgetLedger()
        parent = BudgetController(BudgetLimits(max_model_calls=10),
                                  ledger=ledger, ledger_scope="runtime")
        wf = parent.for_scope("workflow:w1",
                              BudgetLimits(max_model_calls=2, max_cost=10))
        wf.record_model_call(0.01, "m")
        wf.record_model_call(0.01, "m")
        with self.assertRaises(BudgetExceeded) as ctx:
            wf.check_model_call()
        self.assertEqual(ctx.exception.scope, "workflow:w1")
        self.assertEqual(parent.usage.model_calls, 2)
        self.assertEqual(ledger.usage("workflow:w1")["model_calls"], 2)
        self.assertEqual(wf.snapshot()["usage"]["model_calls"], 2)

    def test_provider_pool_shared_across_tasks(self):
        ledger = BudgetLedger()
        first = BudgetController(BudgetLimits(), ledger=ledger)
        first.bind_pool("provider:mock", max_model_calls=2, max_cost=10)
        first.record_model_call(0.01, "mock-1")
        first.record_model_call(0.01, "mock-1")
        second = BudgetController(BudgetLimits(), ledger=ledger)
        second.bind_pool("provider:mock", max_model_calls=2, max_cost=10)
        with self.assertRaises(BudgetExceeded) as ctx:
            second.check_model_call()
        self.assertEqual(ctx.exception.scope, "provider:mock")

    def test_expert_pool_persisted_in_ledger(self):
        ledger = BudgetLedger()
        b = BudgetController(BudgetLimits(), ledger=ledger)
        b.bind_pool("expert:general", max_model_calls=5, max_cost=1.0)
        b.record_model_call(0.02, "m")
        self.assertEqual(ledger.usage("expert:general", "day")["model_calls"], 1)
        self.assertEqual(ledger.usage("expert:general", "month")["model_calls"], 1)

    def test_day_and_month_ledger_keys(self):
        ledger = BudgetLedger()
        ledger.record("runtime", model_calls=3, cost=0.3)
        self.assertEqual(ledger.usage("runtime", "day")["model_calls"], 3)
        self.assertEqual(ledger.usage("runtime", "month")["model_calls"], 3)
        report = ledger.report(("runtime",))
        self.assertEqual(report["runtime"]["day"]["cost"], 0.3)


class _StaticRunner:
    def run_case(self, case):
        return True, 0.01, {"cost": 0.01, "tool_ok": True}


class TestEvolutionBudget(unittest.TestCase):
    def test_daily_evolution_pool_rejects_candidate(self):
        cfg = BudgetConfig(evolution_daily_model_calls=1,
                           evolution_daily_cost=100.0)
        ledger = BudgetLedger()
        ledger.record("evolution", model_calls=1)
        ctl = EvolutionController(evaluator=Evaluator(_StaticRunner()),
                                  budget=ledger, budget_config=cfg)
        cand = ctl.propose_candidate("v0.1.0", "tool/x", "problem", "hypothesis")
        ds = EvalDataset("ds", cases=[EvalCase("c1", "g")])
        out = ctl.run_evaluation(cand, ds)
        self.assertFalse(out["passed"])
        self.assertIn("evolution", out["budget_exhausted"])
        self.assertEqual(cand.status.value, "REJECTED")

    def test_monthly_evolution_pool_checked(self):
        cfg = BudgetConfig(evolution_daily_model_calls=100,
                           evolution_daily_cost=100.0,
                           evolution_monthly_model_calls=2,
                           evolution_monthly_cost=100.0)
        ledger = BudgetLedger()
        ledger.record("evolution", model_calls=2)
        ctl = EvolutionController(budget=ledger, budget_config=cfg)
        self.assertIsNotNone(ctl._evolution_pool_problem())
        self.assertIn("month", ctl._evolution_pool_problem())

    def test_candidate_budget_scope_records_evolution_pool(self):
        ledger = BudgetLedger()
        cfg = BudgetConfig(candidate_max_model_calls=3)
        ctl = EvolutionController(budget=ledger, budget_config=cfg)
        cand = ctl.propose_candidate("v0.1.0", "tool/x", "p", "h")
        budget = ctl._make_candidate_budget(cand)
        budget.record_usage(model_calls=2, tool_calls=1, cost=0.4)
        self.assertIsNone(budget.exceeded())
        budget.record_usage(model_calls=2)
        self.assertIsNotNone(budget.exceeded())
        self.assertEqual(
            ledger.usage(f"candidate:{cand.candidate_id}")["model_calls"], 4)
        self.assertEqual(ledger.usage("evolution")["model_calls"], 4)


class _FakeTask:
    task_id = "t1"
    task_status = TaskStatus.SUCCEEDED
    runtime_manifest = {}
    failure_reason = None
    result_evidence = [{"budget": {"usage": {
        "model_calls": 2, "tool_calls": 1, "cost": 0.4, "seconds": 0.1}}}]


class _FakeController:
    def submit_task(self, goal, **kwargs):
        return _FakeTask()


class TestCandidateRunnerBudget(unittest.TestCase):
    def test_runner_stops_when_candidate_budget_exhausted(self):
        ledger = BudgetLedger()
        runner = ControllerTaskRunner(_FakeController(), ledger=ledger)
        budget = BudgetController(
            BudgetLimits(max_model_calls=3, max_cost=10, max_seconds=60),
            ledger=ledger, ledger_scope="candidate:c1",
            extra_scopes=["evolution"])
        runner.set_budget(budget)
        case = EvalCase("c1", "g")
        ok1, _, ev1 = runner.run_case(case)
        ok2, _, ev2 = runner.run_case(case)
        ok3, _, ev3 = runner.run_case(case)
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertFalse(ok3)
        self.assertIn("budget_exhausted", ev3)
        self.assertIsNotNone(budget.exceeded())
        self.assertEqual(ledger.usage("evolution")["model_calls"], 4)
        self.assertAlmostEqual(ev1["cost"], 0.4)
        runner.set_budget(None)
        self.assertIsNone(runner.budget)


class TestCostReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(tempfile.mktemp(dir=self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, status, cost, domain="coding"):
        t = Task(user_goal="g", intent="general", domain=domain,
                 task_status=status,
                 version_manifest={"version": "v1"},
                 runtime_manifest={"model_route": {"model_id": "m1"}},
                 result_evidence=[{"budget": {"usage": {
                     "model_calls": 2, "tool_calls": 1, "cost": cost}}}])
        self.db.save_task(t)
        return t

    def test_report_totals_and_breakdowns(self):
        self._task(TaskStatus.SUCCEEDED, 0.2)
        self._task(TaskStatus.FAILED, 0.1)
        report = CostReporter(self.db).report()
        totals = report["totals"]
        self.assertEqual(totals["tasks"], 2)
        self.assertEqual(totals["successful_tasks"], 1)
        self.assertEqual(totals["failed_tasks"], 1)
        self.assertAlmostEqual(totals["cost"], 0.3)
        self.assertAlmostEqual(totals["cost_per_success"], 0.2)
        self.assertAlmostEqual(totals["cost_per_success_including_waste"], 0.3)
        self.assertEqual(report["by_domain"]["coding"]["tasks"], 2)
        self.assertEqual(report["by_model"]["m1"]["tasks"], 2)
        self.assertEqual(report["by_version"]["v1"]["tasks"], 2)
        self.assertEqual(len(report["by_task"]), 2)

    def test_evolution_cost_report(self):
        self.db.save_eval_report("r1", "ds", "cand_001", {
            "baseline": {"cost": 0.1, "success_rate": 0.5},
            "candidate": {"cost": 0.2, "success_rate": 0.75},
            "holdout": {"cost": 0.05, "success_rate": 0.7}})
        rows = self.db.list_eval_reports()
        self.assertEqual(rows[0]["report_id"], "r1")
        self.assertEqual(rows[0]["candidate_id"], "cand_001")
        evo = CostReporter(self.db).report()["evolution"]
        self.assertEqual(evo["experiments"], 1)
        self.assertAlmostEqual(evo["total_experiment_cost"], 0.35)
        entry = evo["candidates"][0]
        self.assertAlmostEqual(entry["cost_delta"], 0.1)
        self.assertAlmostEqual(entry["quality_gain"], 0.25)


if __name__ == "__main__":
    unittest.main()
