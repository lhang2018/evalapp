"""Independent ALN-07-W contract probes."""
import pytest
from evoagent.planning.contracts import WorkflowCandidate, MatchContext
from evoagent.planning.matching import WorkflowMatcher
from evoagent.evolution.routing_eval import WorkflowModeCase, WorkflowModeEvaluator

@pytest.mark.parametrize("gap,fields", [
    ("permissions", {"required_permissions": ("write",)}),
    ("dependencies", {"dependencies": ("missing",)}),
    ("versions", {"supported_model_versions": ("v2",)}),
    ("budget", {"cost_estimate": 10}),
])
def test_hybrid_does_not_waive_hard_preconditions(gap, fields):
    c = WorkflowCandidate("w", dynamic_tolerates=(gap,), dynamic_nodes=("choose",), **fields)
    result = WorkflowMatcher([c]).select(MatchContext.build())
    assert result.mode == "dynamic_planning"
    assert result.workflow_id == ""


def test_unknown_budget_even_when_cost_not_declared():
    result = WorkflowMatcher([WorkflowCandidate("w")]).select(MatchContext.build(budget_known=False))
    assert result.mode == "dynamic_planning"


def test_duplicate_identity_rejected_instead_of_order_dependent_binding():
    a = WorkflowCandidate("w", description="first")
    b = WorkflowCandidate("w", description="second", required_inputs=("missing",))
    with pytest.raises(ValueError, match="duplicate"):
        WorkflowMatcher([a, b])


def test_tolerated_input_gap_requires_dynamic_node():
    c = WorkflowCandidate("w", required_inputs=("data",), dynamic_tolerates=("inputs",))
    assert WorkflowMatcher([c]).select(MatchContext.build()).mode == "dynamic_planning"


def test_workflow_accuracy_includes_hybrid_selection():
    c = WorkflowCandidate("w", required_inputs=("data",), dynamic_tolerates=("inputs",), dynamic_nodes=("get",))
    case = WorkflowModeCase("hybrid", MatchContext.build(), "hybrid_agent", "w", candidates=[c])
    report = WorkflowModeEvaluator().run([case])
    assert report.mode_accuracy == 1.0
    assert report.workflow_accuracy == 1.0


def test_mode_metric_independent_of_wrong_workflow():
    c = WorkflowCandidate("actual")
    case = WorkflowModeCase("wrong_wf", MatchContext.build(), "stable_workflow", "expected", candidates=[c])
    report = WorkflowModeEvaluator().run([case])
    assert report.mode_accuracy == 1.0
    assert report.workflow_accuracy == 0.0
    assert not report.case_results[0].ok
