"""Regressions for independently reviewed CXR-01/02/03 evidence gaps."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evoagent.app import App
from evoagent.evolution.asset_impact import asset_impact
from evoagent.evolution.candidates import EvolutionCandidate
from evoagent.evolution.evaluator import (
    EvalCase, EvalDataset, EvalReport, Evaluator, GateThresholds, ReleaseGate,
)
from evoagent.evolution.regression_gate import component_impact, evaluate_regressions
from evoagent.evolution.regression_store import digest
from evoagent.evolution.runner import ControllerTaskRunner


def regression_result(impact):
    controller = SimpleNamespace(
        regression_builder=SimpleNamespace(convert_pending=lambda: None),
        experts=object(), datasets={}, holdout_dataset=None,
        gate_thresholds=GateThresholds(),
    )
    candidate = EvolutionCandidate("c", "v0", "expert/general", "problem")
    candidate.impact_result = impact
    candidate.safety_result = {"passed": True}
    return evaluate_regressions(controller, candidate)


@pytest.mark.parametrize("store", [None, object()])
def test_missing_asset_store_blocks_regression_even_without_required_suites(store):
    impact = asset_impact(store, "expert/general", ["general"])
    assert impact["asset_scope_status"] == "unavailable"
    assert impact["asset_scope_errors"]
    assert not regression_result(impact)["passed"]


@pytest.mark.parametrize("status", ["unavailable", "invalid"])
def test_legacy_unverifiable_scope_with_empty_errors_cannot_bypass_gate(status):
    result = regression_result({"asset_scope_status": status, "asset_scope_errors": []})
    assert not result["passed"]
    assert any("资产" in reason for reason in result["reasons"])


def test_missing_store_component_mapping_propagates_to_regression_gate(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        impact = component_impact(app.experts, "expert/general", store=None)
        result = regression_result(impact)
        assert any("资产" in reason for reason in result["reasons"])
    finally:
        app.db.close()


def test_verified_empty_asset_registry_remains_distinct_from_unavailable(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        impact = asset_impact(app.db, "expert/general", ["general"])
        assert impact["asset_scope_status"] == "not_registered"
        assert impact["asset_scope_errors"] == []
        assert regression_result(impact)["passed"]
    finally:
        app.db.close()


class Runner:
    def __init__(self, evidence):
        self.evidence = evidence

    def run_case(self, case):
        return True, .1, deepcopy(self.evidence)


def reports():
    evaluator = Evaluator(Runner({"cost": .2, "cost_known": True}))
    primary = EvalDataset("primary", cases=[EvalCase("p", "primary", safety=True)])
    held = EvalDataset("held", holdout=True, cases=[EvalCase("h", "holdout", safety=True)])
    return {
        "baseline": evaluator.run(primary, baseline=True),
        "candidate": evaluator.run(primary, baseline=False),
        "holdout_report": evaluator.run(held, baseline=False),
        "holdout_baseline": evaluator.run(held, baseline=True),
    }


def judge(current):
    return ReleaseGate().judge(**current, thresholds=GateThresholds(require_holdout=True))


def test_complete_evaluator_reports_pass_with_holdout_baseline():
    assert judge(reports())["passed"]


@pytest.mark.parametrize("role", ["baseline", "candidate", "holdout_report", "holdout_baseline"])
@pytest.mark.parametrize("damage", ["empty", "duplicate", "missing", "no_case_id"])
def test_each_report_requires_complete_unique_case_details(role, damage):
    current = reports()
    report = current[role]
    if damage == "empty":
        report.case_results = []
    elif damage == "duplicate":
        report.case_results *= 2
        report.case_count = 2
    elif damage == "missing":
        report.case_count = 2
    else:
        report.case_results[0].pop("case")
    result = judge(current)
    assert not result["passed"]
    assert any("评测明细" in reason for reason in result["reasons"])


def test_summary_only_review_reproduction_is_rejected():
    baseline = EvalReport("d", "1", True, success_rate=1., case_count=1,
                          pass_count=1, safety_passed=True, safety_cases=1)
    candidate = deepcopy(baseline)
    candidate.baseline = False
    baseline.metadata["dataset_digest"] = candidate.metadata["dataset_digest"] = "x"
    assert not ReleaseGate().judge(baseline, candidate)["passed"]


@pytest.mark.parametrize("role", ["holdout_report", "holdout_baseline"])
@pytest.mark.parametrize("damage", ["unknown_cost", "nan_cost", "unverified_fixture"])
def test_holdout_evidence_uses_same_validation_as_primary(role, damage):
    current = reports()
    report = current[role]
    if damage == "unknown_cost":
        report.metadata["cost_known"] = False
    elif damage == "nan_cost":
        report.cost = float("nan")
    else:
        report.metadata["unverified_fixtures"] = 1
    assert not judge(current)["passed"]


@pytest.mark.parametrize("hook", [False, True], ids=["task_runner", "controller_fixture_hook"])
@pytest.mark.parametrize("known", [None, False, True, 1, "true"])
def test_reproduced_fixture_requires_explicit_known_cost_independently_of_attestation(hook, known):
    case = EvalCase("fixture", "goal", safety=True, failure_fixture={"error": "timeout"})
    evidence = {"cost": .2, "fixture_reproduced": True,
                "fixture_digest": digest(case.failure_fixture)}
    if known is not None:
        evidence["cost_known"] = known
    before = deepcopy(evidence)
    runner = (ControllerTaskRunner(None, fixture_runner=lambda case, budget: (True, .1, evidence))
              if hook else Runner(evidence))
    evaluator = Evaluator(runner)
    dataset = EvalDataset("fixture", cases=[case])
    base = evaluator.run(dataset, baseline=True)
    candidate = evaluator.run(dataset, baseline=False)
    assert candidate.metadata["unverified_fixtures"] == 0
    assert candidate.metadata["cost_known"] is (known is True)
    assert ReleaseGate().judge(base, candidate)["passed"] is (known is True)
    assert evidence == before


def test_ordinary_legacy_runner_keeps_cost_evidence_compatibility():
    evaluator = Evaluator(Runner({"cost": .2}))
    dataset = EvalDataset("legacy", cases=[EvalCase("legacy", "goal", safety=True)])
    base = evaluator.run(dataset, baseline=True)
    candidate = evaluator.run(dataset, baseline=False)
    assert candidate.metadata["cost_known"] is True
    assert ReleaseGate().judge(base, candidate)["passed"]
