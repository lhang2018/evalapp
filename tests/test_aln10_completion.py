"""ALN-10: immutable reproduction assets and independently enforced release evidence."""
import json
import time
from types import SimpleNamespace

import pytest

from evoagent.app import App
from evoagent.acceptance.verify import Verdict
from evoagent.core.task import Task, TaskStatus
from evoagent.evolution.controller import EvolutionController
from evoagent.evolution.candidates import CandidateStatus
from evoagent.evolution.evaluator import EvalCase, EvalDataset, Evaluator, GateThresholds, ReleaseGate
from evoagent.evolution.monitor import MonitorPolicy, PostReleaseMonitor, collect_metrics
from evoagent.evolution.benefit import evaluate_benefit
from evoagent.evolution.regression_store import RegressionStore
from evoagent.evolution.regression_store import digest
from evoagent.evolution.regression_gate import component_impact
from evoagent.tools.contract import ToolResult
from evoagent.assets.repository import AssetRepository
from evoagent.core.budget import BudgetLedger, BudgetController, BudgetLimits
from evoagent.evolution.runner import ControllerTaskRunner


def dataset(identity, *, domain="general", holdout=False, count=1, safety=False):
    return EvalDataset(dataset_id=identity, holdout=holdout, cases=[
        EvalCase(f"{identity}-{i}", f"input-{identity}-{i}", domain=domain,
                 expected=[{"type": "result_nonempty"}], holdout=holdout, safety=safety)
        for i in range(count)])


class Runner:
    def __init__(self, fails=()):
        self.fails = set(fails)
        self.calls = []
        self.baseline = False
        self.budget = None
        self.suite_ref = "outside"

    def set_budget(self, budget): self.budget = budget

    def run_case(self, case):
        self.calls.append((self.baseline, case.case_id, self.suite_ref, self.budget))
        if self.budget:
            self.budget.record_usage(model_calls=1)
        return (self.baseline or case.case_id not in self.fails), .01, {
            "cost": .01, "cost_known": True, "tool_ok": True, "fixture_reproduced": bool(case.failure_fixture),
            "fixture_digest": digest(case.failure_fixture)}


class ObservedEvaluator(Evaluator):
    def run(self, dataset, *, baseline, **kwargs):
        self.runner.baseline = baseline
        return super().run(dataset, baseline=baseline, **kwargs)


def controller(*, store=None, runner=None, extra=None):
    runner = runner or Runner()
    datasets = {"train": dataset("train", count=3), "hold": dataset("hold", holdout=True)}
    datasets.update(extra or {})
    return EvolutionController(evaluator=ObservedEvaluator(runner), datasets=datasets, store=store), runner


def diagnosed(app, identity="source"):
    task = Task(task_id=identity, user_goal="original input", domain="general", intent="time_query",
                constraints=["preserve original"], acceptance_criteria=[{"type": "result_nonempty"}])
    task.version_manifest = {"code_commit": "original-version", "model_ref": "mock/1"}
    app.db.save_task(task)
    result = ToolResult(ok=False, error="timeout", error_class="timeout", submitted=True,
                        executed=True, contract_valid=True, evidence={"tool": "http_get"})
    case = app.diagnostics.record_failure(task, Verdict(passed=False), [result])
    return task, case


def test_frozen_input_expected_versions_and_fixture_survive_real_app_restart_and_source_deletion(tmp_path):
    app = App(data_dir=str(tmp_path))
    task, source = diagnosed(app)
    app.evolution.regression_builder.convert_pending()
    frozen = RegressionStore(app.db).all()[0]
    assert frozen.constraints == ["preserve original"]
    assert frozen.version_manifest["code_commit"] == "original-version"
    assert frozen.failure_fixture["evidence"] and frozen.failure_fixture["diagnostic_case_id"] == source.case_id
    task.user_goal = "mutated input"; task.acceptance_criteria = []
    app.db.save_task(task)
    app.db._conn.execute("DELETE FROM tasks WHERE task_id=?", (task.task_id,))
    app.db._conn.commit(); app.db.close()
    app = App(data_dir=str(tmp_path))
    try:
        out = app.evolution.regression_builder.convert_pending()
        restored = app.evolution.datasets["failures_regression"].cases[0]
        assert restored == frozen and restored.input_goal == "original input"
        assert out["converted"] == [] and out["already"] == 1
    finally: app.db.close()


def test_content_is_immutable_and_tampering_is_not_accepted_as_gate_evidence(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        diagnosed(app); app.evolution.regression_builder.convert_pending()
        store = RegressionStore(app.db); case = store.all()[0]
        case.input_goal = "replacement"
        with pytest.raises(ValueError, match="不可覆盖"): store.freeze(case)
        app.db._conn.execute("UPDATE regression_assets SET payload='{}'"); app.db._conn.commit()
        with pytest.raises(ValueError, match="摘要"): app.evolution.regression_builder.convert_pending()
    finally: app.db.close()


def test_disk_failure_does_not_publish_memory_only_regression(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        diagnosed(app)
        app.db._conn.execute("CREATE TRIGGER block_regression BEFORE INSERT ON regression_assets BEGIN SELECT RAISE(ABORT,'disk failed'); END")
        app.db._conn.commit()
        with pytest.raises(Exception, match="disk failed"): app.evolution.regression_builder.convert_pending()
        assert "failures_regression" not in app.evolution.datasets
        app.db._conn.rollback()
    finally: app.db.close()


def test_legacy_marker_migrates_once_and_does_not_rewrite_snapshot(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        task, source = diagnosed(app)
        app.db.save_evidence("reg_"+source.case_id, task.task_id, "regression_asset", "legacy", "marker only")
        app.evolution.regression_builder.convert_pending()
        task.user_goal = "later"; app.db.save_task(task)
        app.evolution.datasets.pop("failures_regression")
        app.evolution.regression_builder.convert_pending()
        assert app.evolution.datasets["failures_regression"].cases[0].input_goal == "original input"
        assert len(RegressionStore(app.db).all()) == 1
    finally: app.db.close()


def test_restarted_regression_is_default_gate_input_without_manual_convert(tmp_path):
    app = App(data_dir=str(tmp_path)); _, source = diagnosed(app)
    app.evolution.regression_builder.convert_pending(); app.db.close()
    app = App(data_dir=str(tmp_path))
    try:
        c, runner = controller(store=app.db, runner=Runner(["reg_"+source.case_id]))
        candidate = c.propose_candidate("v0", "expert/general", "p", "h")
        out = c.evaluate_candidate(candidate.candidate_id)
        assert not out["passed"] and out["regression"]["degraded"]
        assert any("reg_"+source.case_id == call[1] for call in runner.calls)
        assert any("回归" in reason for reason in out["reasons"])
        assert any(row["candidate_id"] == candidate.candidate_id for row in c.store.list_eval_reports())
    finally: app.db.close()


def test_matching_frozen_regression_can_pass_with_real_suite_evidence(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        diagnosed(app); c, runner = controller(store=app.db)
        candidate = c.propose_candidate("v0", "expert/general", "p", "h")
        out = c.evaluate_candidate(candidate.candidate_id)
        assert out["passed"], out["reasons"]
        assert out["regression"]["case_count"] == 1
        assert runner.suite_ref == "outside"
    finally: app.db.close()


def test_explicit_primary_selection_cannot_skip_mandatory_suites():
    c, runner = controller(extra={"required": dataset("required")})
    candidate = c.propose_candidate("v0", "expert/general", "p", "h")
    candidate.impact_result = {"must_rerun_tests": ["train", "required", "builtin_safety"]}
    out = c.evaluate_candidate(candidate.candidate_id, dataset_id="train")
    assert out["passed"], out["reasons"]
    assert [call[1] for call in runner.calls].count("required-0") == 2
    assert [call[1] for call in runner.calls].count("train-0") == 2
    assert "builtin_safety" in out["regression"]["required_suites"]


@pytest.mark.parametrize("empty", [False, True])
def test_missing_or_empty_mandatory_suite_fails_closed(empty):
    c, _ = controller(extra={"missing": dataset("missing", count=0)} if empty else {})
    candidate = c.propose_candidate("v0", "expert/general", "p", "h")
    candidate.impact_result = {"must_rerun_tests": ["missing"]}
    out = c.evaluate_candidate(candidate.candidate_id)
    assert not out["passed"]
    assert out["regression"]["missing_suites"] == ["missing"]


def test_covered_domain_and_samples_are_required_independently_of_average():
    c, _ = controller()
    candidate = c.propose_candidate("v0", "expert/general", "p", "h")
    candidate.impact_result = {"required_domains": ["uncovered"]}
    out = c.evaluate_candidate(candidate.candidate_id)
    assert not out["passed"] and out["regression"]["coverage"]["uncovered"] == 0


def test_signal_component_maps_to_capability_dependency_closure(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        impact = component_impact(app.experts, "tool/read_file/timeout")
        assert "file_toolset" in impact["mapped_roots"]
        assert "coding_tasks" in impact["affected_closure"]
        assert "builtin_smoke" in impact["must_rerun_tests"]
        unknown = component_impact(app.experts, "new/unmapped-component")
        assert unknown["mapping_status"] == "conservative_all"
        assert set(unknown["must_rerun_tests"]) >= {"builtin_smoke", "builtin_holdout", "builtin_safety"}
    finally: app.db.close()


def test_individual_domain_regression_cannot_be_hidden_by_high_average():
    ds = dataset("multi", count=10)
    ds.cases[-1].domain = "rare"
    runner = Runner([ds.cases[-1].case_id]); ev = ObservedEvaluator(runner)
    base = ev.run(ds, baseline=True); candidate = ev.run(ds, baseline=False)
    gate = ReleaseGate().judge(base, candidate, safety_ok=True)
    assert candidate.success_rate == .9 and not gate["passed"]
    assert any("领域回归: rare" in reason for reason in gate["reasons"])


@pytest.mark.parametrize("purpose", ["training", "tuning"])
def test_holdout_is_unavailable_to_tuning_before_any_runner_call(purpose):
    runner = Runner()
    with pytest.raises(ValueError, match="保留集"):
        Evaluator(runner).run(dataset("held", holdout=True), baseline=False, purpose=purpose)
    assert not runner.calls


def test_renamed_holdout_input_overlap_is_rejected_before_evaluation():
    c, runner = controller()
    c.holdout_dataset.cases[0].input_goal = c.datasets["train"].cases[0].input_goal
    candidate = c.propose_candidate("v0", "expert/general", "p", "h")
    out = c.evaluate_candidate(candidate.candidate_id)
    assert not out["passed"] and not runner.calls


def test_same_dataset_id_with_mutated_contents_is_not_an_equivalent_baseline():
    ds = dataset("same"); ev = Evaluator(Runner())
    base = ev.run(ds, baseline=True)
    ds.cases[0].expected = [{"type": "tool_succeeded"}]
    candidate = ev.run(ds, baseline=False)
    out = ReleaseGate().judge(base, candidate, safety_ok=True)
    assert not out["passed"] and any("摘要" in r for r in out["reasons"])


def test_safety_case_failure_is_not_overridden_by_static_safety_suite():
    ds = dataset("security", safety=True)
    ev = ObservedEvaluator(Runner(["security-0"]))
    out = ReleaseGate().judge(ev.run(ds, baseline=True), ev.run(ds, baseline=False), safety_ok=True)
    assert not out["passed"] and any("安全失败" in r for r in out["reasons"])


def test_malicious_runner_evidence_cannot_replace_case_identity_or_ok():
    class Bad(Runner):
        def run_case(self, case): return False, .01, {"ok": True, "case": "forged"}
    report = Evaluator(Bad()).run(dataset("honest"), baseline=False)
    assert report.case_results[0]["ok"] is False
    assert report.case_results[0]["case"] == "honest-0"


def metrics(**overrides):
    return {"source": "runtime", "samples": 20, "success_rate": .95, "error_rate": .05,
            "window_complete": True, "attribution": "release", "comparison_valid": True, **overrides}


@pytest.mark.parametrize("field,value", [("window_complete", False), ("attribution", "unscoped"),
    ("comparison_valid", False), ("samples", 1), ("source", "manual")])
def test_monitor_cannot_promote_without_complete_attributed_evidence(field, value):
    out = PostReleaseMonitor(None).assess(metrics(**{field: value}))
    assert out["decision"] in ("hold", "insufficient_data")


def test_complete_attributed_window_can_promote_and_bad_safety_rolls_back_early():
    monitor = PostReleaseMonitor(None)
    assert monitor.assess(metrics())["decision"] == "promote"
    assert monitor.assess(metrics(samples=0, window_complete=False, safety_incident=True))["decision"] == "rollback"


def test_monitor_only_counts_requested_release_and_clamps_observation_window():
    now = time.time()
    def task(at, release, source="user"):
        t = Task(created_at=at, task_status=TaskStatus.SUCCEEDED)
        t.updated_at = at + .01; t.version_manifest = {"release_id": release}
        t.runtime_manifest = {"source": source}
        t.result_evidence = [{"budget": {"usage": {"cost": .01}}}]
        return t
    rows = [task(now-50, "new"), task(now-50, "old"), task(now-50, "new", "evaluation"),
            task(now-5, "new"), task(now-150, "old")]
    store = SimpleNamespace(list_tasks=lambda limit: rows)
    monitor = PostReleaseMonitor(store, policy=MonitorPolicy(window_seconds=90, min_samples=1))
    result = monitor.collect(now-100, release_id="new", baseline_release_id="old")
    assert result["samples"] == 1 and result["previous"]["samples"] == 1
    assert result["window_complete"] and result["comparison_valid"]
    assert result["attribution"] == "release"


def test_sample_count_alone_is_not_long_term_benefit_evidence():
    report = Evaluator(Runner()).run(dataset("q"), baseline=False)
    benefit = evaluate_benefit(report, report, monitor={"metrics": {"samples": 100, "success_rate": 1}})
    assert benefit["dimensions"]["long_term"]["status"] == "insufficient_evidence"


def test_evaluation_failures_do_not_leak_back_into_tuning_assets(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        task, source = diagnosed(app)
        task.runtime_manifest = {"source": "evaluation", "evaluation_suite_ref": "builtin_holdout/1.0.0"}
        app.db.save_task(task)
        out = app.evolution.regression_builder.convert_pending()
        assert source.case_id in out["skipped_unknown"]
        assert not RegressionStore(app.db).all()
    finally: app.db.close()


def test_exact_asset_dependents_add_suites_and_freeze_content_refs(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        repo = AssetRepository(app.db._conn, ensure_schema=False, lock=app.db._lock)
        root = repo.register(asset_id="root", version="1", asset_type="skill",
            namespace="domain_private", content={"eval_suite": "root_tests"})
        repo.register(asset_id="child", version="1", asset_type="skill",
            namespace="domain_private", content={"eval_suite": "child_tests"}, dependency_refs=("root@1",))
        impact = component_impact(app.experts, "asset/root@1", app.db)
        assert impact["asset_scope_status"] == "resolved"
        assert {r["asset_id"] for r in impact["asset_refs"]} == {"root", "child"}
        assert {"root_tests", "child_tests"} <= set(impact["must_rerun_tests"])
        assert next(r for r in impact["asset_refs"] if r["asset_id"] == "root")["content_digest"] == root.content_digest
        missing = component_impact(app.experts, "asset/root@2", app.db)
        assert missing["asset_scope_errors"]
    finally: app.db.close()


def test_baseline_and_all_supplemental_runs_share_budget_and_restore_outer_context():
    c, runner = controller(extra={"other": dataset("other")})
    c.budget = BudgetLedger()
    outer = object(); runner.budget = outer
    candidate = c.propose_candidate("v0", "expert/general", "p", "h")
    candidate.impact_result = {"must_rerun_tests": ["other"]}
    out = c.evaluate_candidate(candidate.candidate_id)
    assert out["passed"], out["reasons"]
    assert len({id(call[3]) for call in runner.calls}) == 1
    assert runner.calls[0][0] is True and runner.calls[0][3] is not None
    assert out["experiment_budget"]["usage"]["model_calls"] == len(runner.calls)
    assert runner.budget is outer and runner.suite_ref == "outside"


def test_custom_quality_gate_cannot_override_independent_failed_evidence():
    c, runner = controller(runner=Runner(["train-0", "train-1"]))
    c.gate = SimpleNamespace(judge=lambda *a, **k: {"passed": True, "reasons": []})
    candidate = c.propose_candidate("v0", "expert/general", "p", "h")
    out = c.evaluate_candidate(candidate.candidate_id)
    assert not out["passed"] and not out["independent_gate"]["passed"]


def test_final_gate_and_per_suite_results_are_persisted_without_report_overwrite(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        c, _ = controller(store=app.db, extra={"other": dataset("other")})
        for _ in range(2):
            candidate = c.propose_candidate("v0", "expert/general", "p", "h")
            candidate.impact_result = {"must_rerun_tests": ["other"]}
            assert c.evaluate_candidate(candidate.candidate_id)["passed"]
        rows = c.store.list_eval_reports()
        assert len(rows) == 2 and rows[0]["report_id"] != rows[1]["report_id"]
        raw = [row["data"] for row in rows]
        assert all(p["gate"]["independent_gate"]["passed"] for p in raw)
        assert all(p["regression"]["suites"][0]["candidate_cases"] for p in raw)
    finally: app.db.close()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_metrics_do_not_trigger_promotion(value):
    assert PostReleaseMonitor(None).assess(metrics(success_rate=value))["decision"] == "insufficient_data"


def test_unknown_cost_evidence_rejects_an_otherwise_successful_candidate():
    ds = dataset("q"); ev = Evaluator(Runner())
    base = ev.run(ds, baseline=True); report = ev.run(ds, baseline=False)
    report.metadata["cost_known"] = False
    result = ReleaseGate().judge(base, report, safety_ok=True)
    assert not result["passed"] and any("费用" in r for r in result["reasons"])


def test_input_replay_alone_is_not_proof_that_original_failure_was_reproduced():
    ds = dataset("replay"); ds.cases[0].failure_fixture = {"error_class": "timeout"}
    class NoReplay(Runner):
        def run_case(self, case): return True, .01, {"cost": .01}
    ev = Evaluator(NoReplay())
    out = ReleaseGate().judge(ev.run(ds, baseline=True), ev.run(ds, baseline=False), safety_ok=True)
    assert not out["passed"] and any("故障夹具" in r for r in out["reasons"])


def test_fixture_hook_is_budgeted_and_a_budget_failure_prevents_new_execution():
    calls = []
    case = dataset("fixture").cases[0]; case.failure_fixture = {"error_class": "timeout"}
    def replay(case, *, budget):
        if budget:
            budget.check_model_call()
        calls.append(case.case_id)
        return True, .01, {"cost": .01, "model_calls": 1, "fixture_reproduced": True,
                           "fixture_digest": digest(case.failure_fixture)}
    runner = ControllerTaskRunner(None, fixture_runner=replay)
    runner.set_budget(BudgetController(BudgetLimits(max_model_calls=1)))
    assert runner.run_case(case)[0]
    assert runner.budget.usage.model_calls == 1
    assert not runner.run_case(case)[0]
    assert calls == [case.case_id]


def test_evaluation_neither_reads_nor_writes_production_memory(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        app.submit("现在几点了", intent="time_query")
        before = app.memory.list_items()
        assert before
        task = app.controller.submit_task("现在几点了", intent="time_query", source="evaluation")
        assert task.runtime_manifest["context"]["memory_candidates"] == 0
        assert app.memory.list_items() == before
        ordinary = app.submit("现在几点了", intent="time_query")
        assert ordinary.runtime_manifest["context"]["memory_candidates"] > 0
    finally: app.db.close()


def test_supplied_monitor_metrics_cannot_impersonate_automatic_runtime_collection():
    c, _ = controller()
    candidate = c.propose_candidate("v0", "expert/general", "p", "h")
    candidate.status = CandidateStatus.MONITORING
    out = c.monitor(candidate, metrics())
    assert out["metrics"]["source"] == "manual"
    assert out["decision"] == "hold"
    assert candidate.status == CandidateStatus.MONITORING
