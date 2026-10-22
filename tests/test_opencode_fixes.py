"""Regression coverage for OC-09-01/02/03/04 and OC-INT-01."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from evoagent.config import BudgetConfig
from evoagent.core import contracts as C
from evoagent.core.budget import BudgetLedger
from evoagent.core.budget import BudgetExceeded
from evoagent.core.persistence import Database
from evoagent.evolution.controller import EvolutionController, ProblemTicket
from evoagent.evolution.candidates import CandidateStatus
from evoagent.evolution.experiment_budget import finish_candidate_budget
from evoagent.evolution.evaluator import EvalCase, EvalDataset, Evaluator
from evoagent.release.manager import ReleaseManager


OBSERVATIONS = {}


class Runner:
    def __init__(self):
        self.budget = None
        self.callback = None

    def set_budget(self, budget):
        self.budget = budget

    def run_case(self, case):
        if self.callback:
            self.callback()
        return True, .01, {"cost": .01, "cost_known": True}


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "review.db"))
    releases = ReleaseManager(store=db, signing_secret="review-key")
    initial = releases.build_manifest(version="stable-v1")
    assert releases.deploy(initial.release_id, gate_passed=True)["ok"]
    datasets = {name: EvalDataset(name, holdout=held, cases=[
        EvalCase(name, name + " input", holdout=held)
    ]) for name, held in (("primary", False), ("hold", True))}
    controller = EvolutionController(store=db, releases=releases,
        evaluator=Evaluator(Runner()), datasets=datasets)
    yield controller
    db.close()


def candidate(c):
    return c.propose_candidate(c.releases.current_release_id, "expert/general", "problem", "hypothesis")


def audits(c):
    return [row for row in c.store.list_evidence(limit=100) if row["kind"] == "evolution_round"]


def test_signal_generated_candidate_can_follow_current_release_contract(env):
    c = env
    c.register_problem(ProblemTicket("signal", "problem", signal_type=C.EVOLUTION_SIGNAL_FAILURE,
                                    target_component="expert/general", hypothesis="hypothesis"))
    [identity] = c.run_signal_pipeline()
    cand = c.get_candidate(identity)
    assert c.evaluate_candidate(identity)["passed"]
    assert c.approve(cand, approver="reviewer")["ok"]
    result = c.deploy_candidate(cand, actor="publisher")
    OBSERVATIONS["baseline"] = {"current_release_id": c.releases.current_release_id,
        "candidate_baseline": cand.baseline_version, "rollback": cand.rollback_version,
        "deploy": result}
    assert result["ok"], result


def test_live_evaluation_is_not_recovered_by_another_round(env):
    c = env
    cand = candidate(c)
    entered, unblock, attempted = threading.Event(), threading.Event(), threading.Event()

    def wait_for_release():
        entered.set()
        assert unblock.wait(10), "test synchronisation timeout"

    c.evaluator.runner.callback = wait_for_release
    evaluate = c.evaluate_candidate

    def intercepted(identity, *args, **kwargs):
        attempted.set()
        return evaluate(identity, *args, **kwargs)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            active = pool.submit(evaluate, cand.candidate_id)
            assert entered.wait(5), "evaluation did not start"
            c.evaluate_candidate = intercepted
            second = pool.submit(c.run_evolution_round)
            observed_attempt = attempted.wait(2)
            unblock.set()
            original_result = active.result(timeout=10)
            report = second.result(timeout=10)
    finally:
        unblock.set()
        c.evaluate_candidate = evaluate
    assert original_result["passed"], original_result
    OBSERVATIONS["live_recovery"] = {"attempted_recovery_of_live_candidate": observed_attempt,
                                    "round": report, "candidate_status": cand.status.value}
    assert not report["errors"], report
    assert not observed_attempt


def test_recovery_retains_candidate_budget_after_partial_failure(env):
    c = env
    c.budget = BudgetLedger(c.store)
    c.budget_config = BudgetConfig(candidate_max_model_calls=2, candidate_max_cost=.25)
    cand = candidate(c)
    runner = c.evaluator.runner

    def partial_failure():
        runner.budget.check_model_call()
        runner.budget.record_usage(model_calls=1, cost=.1)
        raise RuntimeError("evaluation interrupted after a charged call")

    runner.callback = partial_failure
    rounds = [c.run_evolution_round() for _ in range(4)]
    usage = c.budget.usage("candidate:" + cand.candidate_id)
    OBSERVATIONS["recovery_budget"] = {"limits": {"calls": 2, "cost": .25},
        "usage": usage, "candidate_status": cand.status.value, "rounds": rounds}
    assert usage["model_calls"] <= 2 and usage["cost"] <= .25, usage


def test_failed_evaluation_creates_one_round_audit(env):
    c = env
    candidate(c)

    def fail():
        raise RuntimeError("evaluation unavailable")

    c.evaluator.runner.callback = fail
    report = c.run_evolution_round()
    rows = audits(c)
    OBSERVATIONS["duplicate_audit"] = {"audit_count": len(rows), "round": report,
                                       "summaries": [row["summary"] for row in rows]}
    assert len(rows) == 1


def test_audit_storage_failure_is_visible_in_round_result(env):
    c = env
    # Real SQLite write failure limited to round audit rows.
    c.store._conn.execute("CREATE TRIGGER deny_round_audit BEFORE INSERT ON diagnostic_evidence "
        "WHEN NEW.kind='evolution_round' BEGIN SELECT RAISE(ABORT,'audit disk failed'); END")
    c.store._conn.commit()
    try:
        report = c.run_evolution_round()
        OBSERVATIONS["audit_failure"] = {"round": report, "persisted_round_audits": len(audits(c))}
        assert report["errors"] or report.get("audit_error") or report.get("audit_persisted") is False
    finally:
        c.store._conn.rollback()


def test_candidate_budget_survives_database_close_and_controller_restart(env):
    c = env
    c.budget = BudgetLedger(c.store)
    c.budget_config = BudgetConfig(candidate_max_model_calls=2, candidate_max_cost=.25)
    cand = candidate(c)
    def fail_after_charge():
        c.evaluator.runner.budget.record_usage(model_calls=1, cost=.1, seconds=3)
        raise RuntimeError("crash after charge")
    c.evaluator.runner.callback = fail_after_charge
    c.run_evolution_round()
    state = c.store.get_candidate(cand.candidate_id).experiment_budget
    assert state["usage"]["model_calls"] == 1 and state["usage"]["seconds"] >= 3
    path = c.store._conn.execute("PRAGMA database_list").fetchone()[2]
    c.store.close()
    db = Database(path)
    try:
        restored = EvolutionController(store=db, budget=BudgetLedger(db),
            budget_config=c.budget_config, evaluator=Evaluator(Runner()), datasets=c.datasets)
        same = restored.get_candidate(cand.candidate_id)
        assert same.status == CandidateStatus.EVALUATING
        calls = []
        def charged_again():
            b = restored.evaluator.runner.budget
            b.check_model_call()
            b.record_usage(model_calls=1, cost=.1)
            calls.append(1)
            raise RuntimeError("interrupted again")
        restored.evaluator.runner.callback = charged_again
        for _ in range(3):
            restored.run_evolution_round()
        assert calls == [1]
        assert same.status == CandidateStatus.REJECTED
        assert restored.budget.usage("candidate:" + same.candidate_id)["model_calls"] == 2
        assert same.experiment_budget["usage"]["seconds"] >= 3
    finally:
        db.close()


def test_legacy_budget_migration_counts_exact_candidate_days_once(env):
    c = env
    c.budget = BudgetLedger(c.store)
    cand = candidate(c)
    scope = "candidate:" + cand.candidate_id
    for suffix in (":day:2001-01-01", ":day:2001-02-01", ":month:2001-01", ":month:2001-02"):
        c.store.record_budget(scope + suffix, model_calls=1, cost=.1)
    c.store.record_budget(scope + "0:day:2001-01-01", model_calls=999, cost=999)
    budget = c._make_candidate_budget(cand)
    assert budget.usage.model_calls == 2
    assert budget.usage.cost == pytest.approx(.2)


def test_in_memory_candidate_budget_does_not_reset_without_database():
    c = EvolutionController(budget=BudgetLedger())
    cand = c.propose_candidate("v0", "expert/general", "p", "h")
    first = c._make_candidate_budget(cand)
    first.record_usage(model_calls=1, cost=.1)
    finish_candidate_budget(c, cand, first)
    second = c._make_candidate_budget(cand)
    assert second.usage.model_calls == 1 and second.usage.cost == pytest.approx(.1)


def test_unknown_costs_and_call_violation_survive_recovery(env):
    c = env
    c.budget = BudgetLedger(c.store)
    cand = candidate(c)
    first = c._make_candidate_budget(cand)
    first.record_model_call(.3, "test-model")  # Above per-call .25, below candidate 2.
    first.mark_unknown_model_cost("other-model")
    finish_candidate_budget(c, cand, first)
    restored = c._make_candidate_budget(cand)
    assert restored.usage.unknown_model_costs == 1
    with pytest.raises(BudgetExceeded):
        restored.check_alive()


def test_unfinished_attempt_counts_time_since_checkpoint(env):
    c = env
    c.budget = BudgetLedger(c.store)
    cand = candidate(c)
    cand.experiment_budget = {"usage": {"seconds": 5}, "active": True,
                               "updated_at": __import__("time").time() - 10}
    budget = c._make_candidate_budget(cand)
    assert budget.usage.seconds >= 15


def test_snapshot_write_failure_prevents_new_execution(env, monkeypatch):
    c = env
    c.budget = BudgetLedger(c.store)
    candidate(c)
    original = c._persist
    calls = []
    def fail_budget_save(cand):
        if cand.experiment_budget is not None:
            raise OSError("snapshot storage unavailable")
        original(cand)
    monkeypatch.setattr(c, "_persist", fail_budget_save)
    c.evaluator.runner.callback = lambda: calls.append(1)
    report = c.run_evolution_round()
    assert report["errors"] and calls == []
    assert report["audit_persisted"] is True


def test_reentrant_round_never_restarts_active_candidate(env):
    c = env
    cand = candidate(c)
    reports = []
    c.evaluator.runner.callback = lambda: reports.append(c.run_evolution_round())
    assert c.evaluate_candidate(cand.candidate_id)["passed"]
    assert reports and all(C.EVOLUTION_STOP_CONCURRENCY_LIMIT in r["stop_reasons"] for r in reports)
    assert all(not r["errors"] for r in reports)


def test_conversion_failure_still_creates_one_audited_error(env, monkeypatch):
    c = env
    def fail():
        raise ValueError("corrupt regression snapshot")
    monkeypatch.setattr(c.regression_builder, "convert_pending", fail)
    report = c.run_evolution_round()
    assert report["errors"] and report["audit_persisted"]
    assert len(audits(c)) == 1
    assert json.loads(audits(c)[0]["summary"])["errors"] == report["errors"]


def test_audit_retry_uses_same_round_id_and_reports_recovery(env):
    c = env
    c.store._conn.execute("CREATE TRIGGER deny_audit BEFORE INSERT ON diagnostic_evidence "
        "WHEN NEW.kind='evolution_round' BEGIN SELECT RAISE(ABORT,'disk failed'); END")
    c.store._conn.commit()
    report = c.run_evolution_round()
    assert report["audit_persisted"] is False and report["audit_error"]
    c.store._conn.rollback()
    c.store._conn.execute("DROP TRIGGER deny_audit")
    c.store._conn.commit()
    identity = report["round_id"]
    c._audit_round(report)
    c._audit_round(report)
    assert report["audit_persisted"] and "audit_error" not in report
    assert [row["evidence_id"] for row in audits(c)] == [identity]


def test_budget_stop_and_empty_round_have_distinct_single_audits(env, monkeypatch):
    c = env
    first = c.run_evolution_round()
    monkeypatch.setattr(c, "_evolution_pool_problem", lambda: "budget exhausted")
    second = c.run_evolution_round()
    assert first["round_id"] != second["round_id"]
    assert len(audits(c)) == 2
    assert first["audit_persisted"] and second["audit_persisted"]


def test_absent_audit_storage_is_explicit():
    report = EvolutionController().run_evolution_round()
    assert report["audit_persisted"] is False and report["audit_error"]
