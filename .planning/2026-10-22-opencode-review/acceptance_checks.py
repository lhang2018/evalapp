"""Independent review probes; failures are the recorded acceptance findings."""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from evoagent.config import BudgetConfig
from evoagent.core import contracts as C
from evoagent.core.budget import BudgetLedger
from evoagent.core.persistence import Database
from evoagent.evolution.controller import EvolutionController, ProblemTicket
from evoagent.evolution import controller as controller_module
from evoagent.evolution.evaluator import EvalCase, EvalDataset, Evaluator
from evoagent.release.manager import ReleaseManager


OBSERVATIONS = {}


@pytest.fixture(scope="session", autouse=True)
def record_observations():
    source_file = str(Path(controller_module.__file__).resolve())
    expected_root = os.environ.get("REVIEW_SOURCE_ROOT")
    if expected_root:
        assert source_file.startswith(expected_root), source_file
    OBSERVATIONS["source_file"] = source_file
    yield
    Path(__file__).with_name("observations.json").write_text(
        json.dumps(OBSERVATIONS, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")


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
