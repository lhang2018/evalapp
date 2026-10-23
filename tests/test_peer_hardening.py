"""Regression probes for post-review recovery and workflow boundaries."""
import hashlib
import json
from pathlib import Path
import pytest
from evoagent.tools import coding
from evoagent.planning.contracts import MatchContext, WorkflowCandidate
from evoagent.planning.matching import WorkflowMatcher
from evoagent.evolution.routing_eval import WorkflowModeCase, WorkflowModeEvaluator


def _deleted(root):
    target = root / "gone.txt"
    row = dict(path="gone.txt", original="before\n", sha256_before=hashlib.sha256(b"before\n").hexdigest(), sha256_after="", existed=True, deleted=True)
    directory = root / coding._JOURNAL_DIR
    directory.mkdir()
    journal = directory / "pending.json"
    journal.write_text(json.dumps(dict(version=1, status="pending", files=[row])), encoding="utf-8")
    return target, journal, row


def _create_at_last_open(monkeypatch, target):
    original = Path.open
    def race(path, mode="r", *args, **kw):
        if path == target and mode in {"w", "x"}:
            with original(target, "w", encoding="utf-8") as out:
                out.write("other writer\n")
        return original(path, mode, *args, **kw)
    monkeypatch.setattr(Path, "open", race)


def test_recovery_does_not_overwrite_file_created_at_restore(monkeypatch, tmp_path):
    target, journal, _ = _deleted(tmp_path)
    _create_at_last_open(monkeypatch, target)
    result = coding.coding_patch_recover_tool(str(tmp_path))[1]({})
    assert not result.ok
    assert target.read_text(encoding="utf-8") == "other writer\n"
    assert journal.exists()


def test_rollback_does_not_overwrite_file_created_at_restore(monkeypatch, tmp_path):
    target, _, row = _deleted(tmp_path)
    _create_at_last_open(monkeypatch, target)
    failed = coding._rollback_files([dict(row, path=target, rel="gone.txt")])
    assert failed == ["gone.txt"]
    assert target.read_text(encoding="utf-8") == "other writer\n"


def test_explicit_empty_eval_candidates_do_not_inherit_defaults():
    evaluator = WorkflowModeEvaluator([WorkflowCandidate("default")])
    case = WorkflowModeCase("empty", MatchContext.build(), "dynamic_planning", candidates=[])
    assert evaluator.run([case]).case_results[0].ok


def test_omitted_eval_candidates_inherit_defaults():
    evaluator = WorkflowModeEvaluator([WorkflowCandidate("default")])
    case = WorkflowModeCase("inherited", MatchContext.build(), "stable_workflow", "default")
    assert evaluator.run([case]).case_results[0].ok


def test_valid_integer_budget_and_zero_cost_are_supported():
    candidate = WorkflowCandidate("free", cost_estimate=0)
    result = WorkflowMatcher([candidate]).select(MatchContext.build(remaining_budget=0))
    assert result.mode == "stable_workflow"


def test_bad_candidate_case_does_not_abort_batch():
    c = WorkflowCandidate("same")
    cases = [WorkflowModeCase("bad", MatchContext.build(), "stable_workflow", candidates=[c,c]),
             WorkflowModeCase("good", MatchContext.build(), "dynamic_planning", candidates=[])]
    report = WorkflowModeEvaluator().run(cases)
    assert report.case_count == 2
    assert report.case_results[0].error
    assert report.case_results[1].ok


def test_all_preconditions_pass_but_dynamic_nodes_still_require_hybrid():
    candidate = WorkflowCandidate("dynamic", dynamic_nodes=("decide",))
    result = WorkflowMatcher([candidate]).select(MatchContext.build())
    assert result.mode == "hybrid_agent"
    assert result.dynamic_nodes == ("decide",)
    assert len(result.checks) == 5 and all(c.ok for c in result.checks)


@pytest.mark.parametrize("cost", [-1, True, 1.5, "10", float("nan")])
def test_invalid_candidate_cost_rejected(cost):
    with pytest.raises(ValueError):
        WorkflowCandidate("invalid", cost_estimate=cost)


@pytest.mark.parametrize("remaining", [-1, True, 1.5, "10", float("inf")])
def test_invalid_budget_rejected_without_coercion(remaining):
    with pytest.raises(ValueError):
        MatchContext.build(remaining_budget=remaining)


@pytest.mark.parametrize("known", ["false", 0, 1, None])
def test_budget_known_is_boolean(known):
    with pytest.raises(ValueError):
        MatchContext.build(budget_known=known)


def test_candidate_and_context_defensively_snapshot_mutable_inputs():
    required = ["input"]
    granted = {"read"}
    candidate = WorkflowCandidate("w", required_inputs=required)
    context = MatchContext(granted_permissions=granted)
    required.append("other")
    granted.add("write")
    assert candidate.required_inputs == ("input",)
    assert context.granted_permissions == frozenset({"read"})


@pytest.mark.parametrize("field,value", [("workflow_id", ""), ("version", ""), ("required_permissions", "read"), ("dependencies", [""])])
def test_malformed_candidate_identity_and_collections_rejected(field, value):
    with pytest.raises(ValueError):
        WorkflowCandidate(**dict({"workflow_id":"w"}, **{field:value}))
