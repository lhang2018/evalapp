"""ALN-10-R: the release entry point consumes bound durable gate evidence."""
import json
import threading
import urllib.request

import pytest

from evoagent.app import App
from evoagent.api.server import ApiServer
from evoagent.core.persistence import Database
from evoagent.evolution.candidates import CandidateStatus
from evoagent.evolution.controller import EvolutionController
from evoagent.evolution.evaluator import EvalCase, EvalDataset, Evaluator
from evoagent.release.manager import ReleaseManager


class Runner:
    def __init__(self):
        self.callback = None

    def run_case(self, case):
        if self.callback:
            self.callback()
        return True, .01, {"cost": .01, "cost_known": True}


def datasets():
    return {name: EvalDataset(name, holdout=holdout, cases=[
        EvalCase(f"{name}-{i}", f"input {name} {i}", holdout=holdout)
        for i in range(3)]) for name, holdout in (("primary", False), ("hold", True))}


def make_controller(db):
    releases = ReleaseManager(store=db, signing_secret="test-signing-key")
    if not releases.current_release_id:
        initial = releases.build_manifest(version="initial")
        assert releases.deploy(initial.release_id, gate_passed=True)["ok"]
    return EvolutionController(store=db, releases=releases,
        evaluator=Evaluator(Runner()), datasets=datasets())


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "state.db"))
    c = make_controller(db)
    cand = c.propose_candidate(c.releases.current_release_id, "expert/general", "problem", "hypothesis")
    yield c, cand
    db.close()


def evaluate(c, cand):
    result = c.evaluate_candidate(cand.candidate_id)
    assert result["passed"], result
    return result


def ready(c, cand):
    evaluate(c, cand)
    assert c.approve(cand, approver="reviewer")["ok"]


def test_release_uses_saved_decision_and_signed_approval(env):
    c, cand = env
    ready(c, cand)
    baseline = c.releases.current_release_id
    result = c.deploy_candidate(cand, actor="publisher", gray_percent=10)
    assert result["ok"] and cand.status == CandidateStatus.MONITORING
    assert cand.deployed_release_id == c.releases.current_release_id
    manifest = c.releases.manifests[result["release_id"]]
    assert manifest.rollback_target == baseline
    assert manifest.eval_report["report_id"] == cand.evaluation_report_id
    assert manifest.eval_report["evidence"]["gate"]["independent_gate"]["passed"] is True
    assert manifest.eval_report["approval"]["approver"] == "reviewer"
    assert c.releases.verify(manifest.release_id)["ok"]
    assert not c.deploy_candidate(cand, actor="publisher")["ok"]


@pytest.mark.parametrize("suggestion", ["", "release_ready", "rejected"])
def test_external_true_cannot_replace_missing_evaluation_or_approval(env, suggestion):
    c, cand = env
    cand.status = CandidateStatus.RELEASE_READY
    cand.publish_suggestion = suggestion
    baseline = c.releases.current_release_id
    assert not c.deploy_candidate(cand, actor="publisher", gate_passed=True)["ok"]
    assert c.releases.current_release_id == baseline and len(c.releases.manifests) == 1


def test_passed_evaluation_without_bound_approval_cannot_release(env):
    c, cand = env
    evaluate(c, cand)
    cand.status = CandidateStatus.RELEASE_READY
    assert not c.deploy_candidate(cand, actor="publisher", gate_passed=True)["ok"]


def test_explicit_false_can_veto_passed_evidence(env):
    c, cand = env
    ready(c, cand)
    assert not c.deploy_candidate(cand, actor="publisher", gate_passed=False)["ok"]
    assert cand.status == CandidateStatus.RELEASE_READY


@pytest.mark.parametrize("field,value", [
    ("risk_level", "L1"), ("hypothesis", "different change"),
    ("change_scope", "shared"), ("compatible_requirements", ["new-tool"]),
    ("target_component", "tool/http"), ("rollback_version", "different"),
    ("quality_result", {"success_rate": 1}),
])
def test_candidate_edits_after_approval_require_new_evaluation(env, field, value):
    c, cand = env
    ready(c, cand)
    setattr(cand, field, value)
    result = c.deploy_candidate(cand, actor="publisher", gate_passed=True)
    assert not result["ok"] and "候选内容" in result["error"]


@pytest.mark.parametrize("change", ["threshold", "dataset", "holdout"])
def test_changed_gate_inputs_invalidate_previous_approval(env, change):
    c, cand = env
    ready(c, cand)
    if change == "threshold":
        c.gate_thresholds.min_success_rate = .9
    elif change == "dataset":
        c.datasets["primary"].cases[0].expected = [{"type": "result_nonempty"}]
    else:
        c.holdout_dataset = None
    result = c.deploy_candidate(cand, actor="publisher")
    assert not result["ok"] and "阈值" in result["error"]


@pytest.mark.parametrize("change", ["delete", "tamper", "wrong_candidate"])
def test_report_unavailable_or_altered_blocks_deployment(env, change):
    c, cand = env
    ready(c, cand)
    row = c.store.get_eval_report(cand.evaluation_report_id)
    if change == "delete":
        c.store._conn.execute("DELETE FROM eval_reports WHERE report_id=?", (row["report_id"],))
        c.store._conn.commit()
    else:
        row["data"]["candidate"]["success_rate"] = .01
        c.store.save_eval_report(row["report_id"], row["dataset_id"],
            "other-candidate" if change == "wrong_candidate" else cand.candidate_id, row["data"])
    assert not c.deploy_candidate(cand, actor="publisher")["ok"]


def test_failed_decision_cannot_be_relabelled_as_release_ready(env):
    c, cand = env
    c.evaluator.runner.run_case = lambda case: (False, .01, {"cost": .01})
    assert not c.evaluate_candidate(cand.candidate_id)["passed"]
    cand.status = CandidateStatus.APPROVAL_PENDING
    cand.publish_suggestion = "release_ready"
    result = c.approve(cand, approver="reviewer")
    assert not result["ok"] and "门禁" in result["error"]


@pytest.mark.parametrize("risk", ["L2", "L3", "L4", "unknown"])
def test_auto_approval_cannot_bypass_risk_policy(env, risk):
    c, cand = env
    cand.risk_level = risk
    evaluate(c, cand)
    assert not c.approve(cand, approver=" Auto ")["ok"]
    assert cand.status == CandidateStatus.APPROVAL_PENDING
    if risk in ("L4", "unknown"):
        assert not c.approve(cand, approver="reviewer")["ok"]


@pytest.mark.parametrize("risk,actor", [("L2", "auto"), ("L3", " Auto "),
                                        ("L2", " "), ("L4", "publisher"), ("unknown", "publisher")])
def test_release_entry_point_enforces_risk_and_named_actor(env, risk, actor):
    c, cand = env
    cand.risk_level = risk
    cand.status = CandidateStatus.RELEASE_READY
    out = c.deploy_candidate(cand, actor=actor, gate_passed=True)
    assert not out["ok"] and len(c.releases.manifests) == 1


@pytest.mark.parametrize("risk", ["L0", "L1"])
def test_low_risk_auto_release_still_needs_bound_evaluation(env, risk):
    c, cand = env
    cand.risk_level = risk
    evaluate(c, cand)
    assert c.approve(cand)["ok"]
    assert c.deploy_candidate(cand)["ok"]


@pytest.mark.parametrize("change", ["new_release", "corrupt_baseline"])
def test_stable_baseline_and_rollback_artifact_are_rechecked(env, change):
    c, cand = env
    ready(c, cand)
    if change == "new_release":
        other = c.releases.build_manifest(version="someone-else")
        assert c.releases.deploy(other.release_id, gate_passed=True)["ok"]
    else:
        c.releases.manifests[c.releases.current_release_id].code_commit = "tampered"
    result = c.deploy_candidate(cand, actor="publisher")
    assert not result["ok"] and "基线" in result["error"]


def test_approval_and_exact_report_survive_restart_and_more_than_fifty_reports(env):
    c, cand = env
    ready(c, cand)
    for i in range(60):
        c.store.save_eval_report(f"unrelated-{i}", "other", "other", {})
    restarted = make_controller(c.store)
    restored = restarted.candidates[cand.candidate_id]
    assert restored.approval_result == cand.approval_result
    assert restarted.deploy_candidate(restored, actor="publisher")["ok"]


def test_failed_report_write_never_advertises_release_evidence(env, monkeypatch):
    c, cand = env
    cand.publish_suggestion = "release_ready"
    cand.evaluation_report_id = "stale"
    cand.approval_result = {"approver": "old"}
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(c.store, "save_eval_report", fail)
    with pytest.raises(OSError):
        c.evaluate_candidate(cand.candidate_id)
    assert not cand.evaluation_report_id and not cand.approval_result and not cand.publish_suggestion
    assert not c.approve(cand, approver="reviewer")["ok"]


def test_candidate_mutation_during_model_calls_is_rejected(env):
    c, cand = env
    c.evaluator.runner.callback = lambda: setattr(cand, "hypothesis", "changed during run")
    result = c.evaluate_candidate(cand.candidate_id)
    assert not result["passed"] and not cand.evaluation_report_id
    assert cand.status == CandidateStatus.REJECTED


def test_legacy_candidate_without_receipt_requires_reevaluation(env):
    c, cand = env
    cand.status = CandidateStatus.APPROVAL_PENDING
    cand.publish_suggestion = "release_ready"
    assert not c.approve(cand, approver="reviewer")["ok"]


def test_dataset_mutation_during_calls_cannot_be_sealed_as_evaluated(env):
    c, cand = env
    c.evaluator.runner.callback = lambda: setattr(c.datasets["primary"].cases[0], "input_goal", "new input")
    result = c.evaluate_candidate(cand.candidate_id)
    assert not result["passed"] and not cand.evaluation_report_id


def test_dependency_drift_since_approval_requires_new_evaluation(env, monkeypatch):
    c, cand = env
    c.experts = object()
    impact = {"must_rerun_tests": ["primary"], "required_domains": [], "asset_refs": []}
    monkeypatch.setattr(c, "_impact_for", lambda component: dict(impact))
    ready(c, cand)
    impact["asset_refs"] = [{"asset_id": "changed", "version": "2"}]
    result = c.deploy_candidate(cand, actor="publisher")
    assert not result["ok"] and "影响范围" in result["error"]


def test_release_manager_failure_preserves_ready_status_and_stable_pointer(env, monkeypatch):
    c, cand = env
    ready(c, cand)
    stable = c.releases.current_release_id
    monkeypatch.setattr(c.releases, "deploy", lambda *args, **kwargs: {"ok": False, "error": "rejected by release manager"})
    assert not c.deploy_candidate(cand, actor="publisher")["ok"]
    assert c.releases.current_release_id == stable and cand.status == CandidateStatus.RELEASE_READY
    assert not cand.deployed_release_id and not cand.deployed_at


def test_custom_gate_cannot_change_thresholds_while_judging(env):
    c, cand = env
    class MutatingGate:
        def judge(self, *args, **kwargs):
            c.gate_thresholds.min_success_rate = .1
            return {"passed": True, "reasons": []}
    c.gate = MutatingGate()
    result = c.evaluate_candidate(cand.candidate_id)
    assert not result["passed"] and cand.status == CandidateStatus.REJECTED
    assert "判定期间" in result["reasons"][-1]


def test_candidate_changed_before_approval_does_not_get_a_receipt(env):
    c, cand = env
    evaluate(c, cand)
    cand.hypothesis = "new change"
    assert not c.approve(cand, approver="reviewer")["ok"]
    assert not cand.approval_result and cand.status == CandidateStatus.APPROVAL_PENDING


@pytest.mark.parametrize("has_evidence", [True, False])
def test_http_approval_reports_and_audits_the_actual_decision(tmp_path, has_evidence):
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "operator-token"
    app.config.api_admin_token = "admin-token"
    app.evolution = c = make_controller(app.db)
    cand = c.propose_candidate(c.releases.current_release_id, "expert/general", "p", "h")
    if has_evidence:
        evaluate(c, cand)
    else:
        cand.status = CandidateStatus.APPROVAL_PENDING
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/evolution/candidates/{cand.candidate_id}/approve",
            data=json.dumps({"approver": "reviewer"}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer admin-token"})
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read())
        assert result["ok"] is has_evidence
        record = next(row for row in app.audit.list() if row["action"] == "evolution.approve")
        assert record["outcome"] == ("ok" if has_evidence else "blocked")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        app.db.close()
