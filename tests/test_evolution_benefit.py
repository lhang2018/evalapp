"""十维收益评价与发布后监测（PRD 50 / 82, Arch 11.5 / 12.3）。"""

import time
import unittest
from types import SimpleNamespace

from evoagent.core.task import Task, TaskStatus
from evoagent.evolution.benefit import evaluate_benefit
from evoagent.evolution.candidates import CandidateStatus, EvolutionCandidate
from evoagent.evolution.controller import EvolutionController
from evoagent.evolution.evaluator import (EvalDataset, EvalReport,
                                          GateThresholds, ReleaseGate)
from evoagent.evolution.monitor import (MonitorPolicy, PostReleaseMonitor,
                                        collect_metrics)


def make_report(*, domains=None, success=None, cost=0.1, latency=1.0,
                dataset="d", version="1.0.0", baseline=False):
    """按领域生成评测报告；domains: {domain: (passed, cases)}。"""
    domains = domains or {"general": (3, 3)}
    r = EvalReport(dataset_id=dataset, version=version, baseline=baseline)
    r.case_results = []
    r.difficulty_breakdown = {}
    passed = cases = 0
    for domain, (ok_n, total_n) in domains.items():
        for i in range(total_n):
            ok = i < ok_n
            passed += 1 if ok else 0
            cases += 1
            r.case_results.append({"case": f"{domain}_{i}", "ok": ok,
                                   "domain": domain, "difficulty": "easy"})
            bucket = r.difficulty_breakdown.setdefault(
                "easy", {"cases": 0, "passed": 0})
            bucket["cases"] += 1
            bucket["passed"] += 1 if ok else 0
    r.case_count = cases
    r.pass_count = passed
    r.success_rate = success if success is not None else passed / max(1, cases)
    r.cost = cost
    r.avg_latency = latency
    return r


def dims(report):
    return report["dimensions"]


class TestBenefit(unittest.TestCase):
    def test_ten_dimensions_present(self):
        b = evaluate_benefit(make_report(), make_report(),
                             safety={"passed": True})
        self.assertEqual(set(dims(b)), {
            "quality", "success_rate", "stability", "recovery", "cost",
            "latency", "safety", "maintenance_complexity", "cross_domain",
            "long_term"})
        self.assertEqual(dims(b)["safety"]["status"], "passed")

    def test_difficulty_weighted_quality(self):
        base = make_report()
        cand = make_report()
        cand.difficulty_breakdown = {
            "easy": {"cases": 1, "passed": 0},
            "hard": {"cases": 1, "passed": 1}}
        b = evaluate_benefit(base, cand)
        # (0*1 + 1*2) / (1*1 + 1*2) = 0.667
        self.assertAlmostEqual(dims(b)["quality"]["value"]["weighted"],
                               0.6667, places=3)

    def test_cost_and_latency_regression(self):
        b = evaluate_benefit(make_report(cost=1.0, latency=1.0),
                             make_report(cost=2.0, latency=3.0))
        self.assertEqual(dims(b)["cost"]["status"], "regressed")
        self.assertEqual(dims(b)["latency"]["status"], "regressed")
        self.assertEqual(dims(b)["cost"]["value"]["ratio"], 2.0)

    def test_recovery_improvement(self):
        base = make_report()
        base.recovery_attempts, base.recovery_successes = 4, 2
        cand = make_report()
        cand.recovery_attempts, cand.recovery_successes = 4, 4
        b = evaluate_benefit(base, cand)
        self.assertEqual(dims(b)["recovery"]["status"], "improved")

    def test_stability_proxy(self):
        cand = make_report(domains={"general": (2, 4)})
        cand.tool_failures = 2
        b = evaluate_benefit(make_report(), cand)
        self.assertAlmostEqual(dims(b)["stability"]["value"]["candidate"],
                               0.5)
        self.assertEqual(dims(b)["stability"]["value"]["method"],
                         "failure_evidence_proxy")

    def test_cross_domain_regression(self):
        base = make_report(domains={"general": (3, 3), "coding": (3, 3)})
        cand = make_report(domains={"general": (3, 3), "coding": (1, 3)})
        b = evaluate_benefit(base, cand, target_domain="general")
        cross = dims(b)["cross_domain"]
        self.assertEqual(cross["status"], "degraded")
        self.assertIn("coding", cross["degraded_domains"])

    def test_cross_domain_requires_two_domains(self):
        b = evaluate_benefit(make_report(), make_report())
        self.assertEqual(dims(b)["cross_domain"]["status"],
                         "insufficient_evidence")

    def test_maintenance_complexity(self):
        def level(scope):
            return evaluate_benefit(make_report(), make_report(),
                                    change_scope=scope)[
                "dimensions"]["maintenance_complexity"]
        self.assertEqual(level("local")["value"]["level"], "low")
        self.assertEqual(level("expert")["value"]["level"], "medium")
        self.assertEqual(level("shared")["value"]["level"], "high")
        self.assertEqual(level("unknown")["status"], "insufficient_evidence")

    def test_long_term_needs_monitoring_samples(self):
        none = evaluate_benefit(make_report(), make_report())
        self.assertEqual(
            dims(none)["long_term"]["status"], "insufficient_evidence")
        b = evaluate_benefit(
            make_report(), make_report(),
            monitor={"metrics": {"samples": 20, "success_rate": 0.9}})
        self.assertEqual(dims(b)["long_term"]["status"], "observed")

    def test_safety_failure_rejects(self):
        b = evaluate_benefit(make_report(), make_report(),
                             safety={"passed": False})
        self.assertEqual(b["recommendation"], "reject")

    def test_gate_rejects_cross_domain_regression(self):
        base = make_report(domains={"general": (3, 3), "coding": (3, 3)})
        cand = make_report(domains={"general": (3, 3), "coding": (1, 3)})
        benefit = evaluate_benefit(base, cand, target_domain="general")
        out = ReleaseGate().judge(
            base, cand, thresholds=GateThresholds(require_holdout=False),
            safety_ok=True, benefit=benefit)
        self.assertFalse(out["passed"])
        self.assertTrue(any("跨领域回归" in r for r in out["reasons"]))


class FakeStore:
    def __init__(self, tasks):
        self.tasks = tasks

    def list_tasks(self, limit=5000):
        return list(self.tasks[:limit])

    def list_candidates(self, limit=1000):
        return []

    def save_candidate(self, cand):
        pass


class FakeBus:
    def __init__(self, events=None):
        self.events = list(events or [])
        self.emitted = []

    def query(self, task_id=None, limit=100):
        return list(self.events)

    def emit(self, **kwargs):
        self.emitted.append(kwargs)
        return SimpleNamespace(**kwargs)


class FakeReleases:
    def __init__(self):
        self.rolled_back = []

    def rollback(self, reason):
        self.rolled_back.append(reason)
        return {"ok": True, "reason": reason}


def make_task(status, created_at, *, domain="general", cost=0.01,
              source="user", tool_failed=False):
    task = Task(user_goal="x", domain=domain, created_at=created_at)
    task.task_status = status
    task.updated_at = created_at + 1.0
    task.runtime_manifest = {"source": source}
    evidence = [{"budget": {"usage": {"cost": cost}}}]
    if tool_failed:
        evidence.append({"type": "tool_call", "passed": False})
    task.result_evidence = evidence
    return task


def to_monitoring(controller, cand, *, deployed_at, target_domain=""):
    for status, reason in (
            (CandidateStatus.TESTING, "test"),
            (CandidateStatus.EVALUATING, "eval"),
            (CandidateStatus.APPROVAL_PENDING, "pending"),
            (CandidateStatus.RELEASE_READY, "ready"),
            (CandidateStatus.DEPLOYING, "deploy"),
            (CandidateStatus.MONITORING, "monitor")):
        cand.transition(status, reason)
    cand.deployed_at = deployed_at
    cand.target_domain = target_domain
    controller._persist(cand)
    return cand


class TestMonitor(unittest.TestCase):
    def _controller(self, tasks, bus=None):
        releases = FakeReleases()
        c = EvolutionController(store=FakeStore(tasks), event_bus=bus or FakeBus(),
                                releases=releases)
        cand = c.propose_candidate("v1", "expert/general", "p", "h",
                                   change_scope="expert")
        return c, to_monitoring(c, cand, deployed_at=time.time() - 100,
                                target_domain="general"), releases

    def test_collect_excludes_evaluation_tasks(self):
        now = time.time()
        store = FakeStore([
            make_task(TaskStatus.SUCCEEDED, now - 5),
            make_task(TaskStatus.SUCCEEDED, now - 4, source="evaluation"),
            make_task(TaskStatus.FAILED, now - 3),
        ])
        m = collect_metrics(store, now - 10, now + 1)
        self.assertEqual(m["samples"], 2)
        self.assertEqual(m["successes"], 1)
        self.assertEqual(m["failures"], 1)

    def test_insufficient_data_keeps_monitoring(self):
        now = time.time()
        c, cand, _ = self._controller([
            make_task(TaskStatus.SUCCEEDED, now - 5)])
        out = c.monitor(cand)
        self.assertEqual(out["decision"], "insufficient_data")
        self.assertEqual(cand.status, CandidateStatus.MONITORING)

    def test_healthy_promotes(self):
        now = time.time()
        tasks = [make_task(TaskStatus.SUCCEEDED, now - 5 - i)
                 for i in range(6)]
        c, cand, _ = self._controller(tasks)
        out = c.monitor(cand)
        self.assertEqual(out["decision"], "promote")
        self.assertEqual(cand.status, CandidateStatus.PROMOTED)

    def test_low_success_rolls_back(self):
        now = time.time()
        tasks = [make_task(TaskStatus.SUCCEEDED, now - 5)]
        tasks += [make_task(TaskStatus.FAILED, now - 6 - i)
                  for i in range(5)]
        c, cand, releases = self._controller(tasks)
        out = c.monitor(cand)
        self.assertEqual(out["decision"], "rollback")
        self.assertEqual(cand.status, CandidateStatus.ROLLED_BACK)
        self.assertEqual(len(releases.rolled_back), 1)

    def test_warning_holds(self):
        now = time.time()
        c, cand, _ = self._controller([])
        # 成功率达标但成本接近阈值 -> 停止推广，不晋级也不回滚
        out = c.monitor(cand, {"success_rate": 0.90, "error_rate": 0.05,
                               "cost_ratio": 1.35})
        self.assertEqual(out["decision"], "hold")
        self.assertTrue(out["warnings"])
        self.assertEqual(cand.status, CandidateStatus.MONITORING)

    def test_safety_incident_rolls_back_without_samples(self):
        now = time.time()
        event = SimpleNamespace(event_id="e1", event_type="safety_incident",
                                task_id="t1", timestamp=now - 5,
                                severity="critical")
        c, cand, _ = self._controller([], bus=FakeBus([event]))
        out = c.monitor(cand)
        self.assertEqual(out["decision"], "rollback")
        self.assertEqual(cand.status, CandidateStatus.ROLLED_BACK)

    def test_manual_metrics_legacy_path(self):
        now = time.time()
        c, cand, _ = self._controller([])
        out = c.monitor(cand, {"success_rate": 0.95, "error_rate": 0.01,
                               "cost_ratio": 1.0})
        self.assertEqual(out["decision"], "promote")
        self.assertEqual(out["metrics"]["source"], "manual")

    def test_auto_rollback_can_be_disabled(self):
        now = time.time()
        c, cand, releases = self._controller([
            make_task(TaskStatus.FAILED, now - 5 - i) for i in range(6)])
        out = c.monitor(cand, policy=MonitorPolicy(auto_rollback=False))
        self.assertEqual(out["decision"], "rollback_recommended")
        self.assertEqual(cand.status, CandidateStatus.MONITORING)
        self.assertEqual(releases.rolled_back, [])

    def test_manual_cost_and_latency_ratios(self):
        now = time.time()
        c, cand, _ = self._controller([])
        out = c.monitor(cand, {"success_rate": 0.9, "cost_ratio": 3.0,
                               "latency_ratio": 1.0})
        self.assertEqual(out["decision"], "rollback")


class TestBenefitIntegration(unittest.TestCase):
    def test_run_evaluation_records_benefit(self):
        base = make_report(domains={"general": (3, 3), "coding": (3, 3)})
        cand_report = make_report(
            domains={"general": (3, 3), "coding": (1, 3)})

        class FakeEvaluator:
            def run(self, dataset, *, baseline, metadata=None):
                return base if baseline else cand_report

        c = EvolutionController(evaluator=FakeEvaluator())
        c.gate_thresholds = GateThresholds(require_holdout=False)
        cand = c.propose_candidate("v1", "expert/coding", "p", "h",
                                   change_scope="expert",
                                   target_domain="general")
        ds = EvalDataset(dataset_id="d", version="1.0.0")
        out = c.run_evaluation(cand, ds, baseline_report=base)
        self.assertIsNotNone(cand.benefit_result)
        self.assertEqual(
            cand.benefit_result["dimensions"]["cross_domain"]["status"],
            "degraded")
        self.assertFalse(out["passed"])
        self.assertTrue(any("跨领域回归" in r for r in out["reasons"]))


if __name__ == "__main__":
    unittest.main()
