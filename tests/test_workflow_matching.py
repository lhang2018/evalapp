"""工作流匹配与执行模式选择（ALN-07-W）回归。

夹具先证缺口的纪律：本文件从 `evoagent.planning.contracts` /
`evoagent.planning.matching` / `ExecutionPlan.execution_mode` 直接 import——
若匹配层被删除或 plan 不记录模式，这些测试会在 import/属性层面立即红掉，
而非静默通过。

覆盖任务卡 §验收 1-6：
1. 五项检查任一不满足即拒绝且原因可查；
2. 三模式中显式选一并留痕，无隐式默认；
3. 结果与候选顺序、输入顺序无关；
4. 匹配阶段只读额度、不扣账、不持有预算权威；
5. 路由评测集新增工作流/模式两维度，能给出准确率；
6. planner 接入记录模式与理由，且序列化往返保持。
"""
import pytest

from evoagent.planning.contracts import (
    CHECK_NAMES, EXECUTION_MODES, CandidateVerdict, MatchContext,
    ModeSelection, WorkflowCandidate)
from evoagent.planning.matching import WorkflowMatcher, selection_from_dict
from evoagent.planning.planner import ExecutionPlan, PlanningEngine
from evoagent.evolution.routing_eval import (
    WorkflowModeEvaluator, builtin_workflow_mode_dataset, routing_coverage)


def _c(**kw):
    return WorkflowCandidate(workflow_id=kw.pop("wid", "w"), **kw)


def _matcher(cands):
    return WorkflowMatcher(cands)


# ---------------------------------------------------------------- 1. 五项检查拒绝且原因可查
def test_checks_inputs_missing():
    c = _c(required_inputs=("a", "b"))
    v = _matcher([c]).evaluate(c, MatchContext.build(inputs=("a",)))
    assert not v.applicable
    chk = {ch.name: ch for ch in v.checks}
    assert not chk["inputs"].ok
    assert "b" in chk["inputs"].missing


def test_checks_permissions_missing():
    c = _c(required_permissions=("read",))
    v = _matcher([c]).evaluate(c, MatchContext.build(permissions=()))
    assert not v.applicable
    assert not {ch.name: ch for ch in v.checks}["permissions"].ok


def test_checks_dependencies_missing():
    c = _c(dependencies=("skill:pdf@1",))
    v = _matcher([c]).evaluate(c, MatchContext.build(dependencies=()))
    assert not v.applicable
    assert not {ch.name: ch for ch in v.checks}["dependencies"].ok


def test_checks_version_unsupported():
    c = _c(supported_model_versions=("gpt-4o",))
    v = _matcher([c]).evaluate(c, MatchContext.build(model_version="gpt-3.5"))
    assert not v.applicable
    chk = {ch.name: ch for ch in v.checks}
    assert not chk["versions"].ok
    assert "gpt-3.5" in chk["versions"].detail


def test_checks_budget_exceeded():
    c = _c(cost_estimate=100)
    v = _matcher([c]).evaluate(c, MatchContext.build(remaining_budget=10))
    assert not v.applicable
    assert not {ch.name: ch for ch in v.checks}["budget"].ok


def test_budget_unknown_not_assumed_sufficient():
    # 预算不可知时，绝不能假设充足 → 必须拒绝，且原因可查
    c = _c(cost_estimate=10)
    v = _matcher([c]).evaluate(
        c, MatchContext.build(remaining_budget=0, budget_known=False))
    assert not v.applicable
    chk = {ch.name: ch for ch in v.checks}
    assert not chk["budget"].ok
    assert "不可知" in chk["budget"].detail


def test_version_undeclared_passes():
    # 候选未声明版本约束 → 不卡版本
    c = _c()
    v = _matcher([c]).evaluate(c, MatchContext.build())
    assert {ch.name: ch for ch in v.checks}["versions"].ok


# ---------------------------------------------------------------- 2. 模式显式 + 不隐式默认
def test_stable_when_all_pass():
    c = _c(required_inputs=("a",), cost_estimate=5)
    sel = _matcher([c]).select(MatchContext.build(inputs=("a",), remaining_budget=10))
    assert sel.mode == "stable_workflow"
    assert sel.workflow_id == "w"
    assert "稳定" in sel.rationale


def test_hybrid_when_single_tolerated_gap():
    c = _c(required_inputs=("a", "b"),
           dynamic_tolerates=("inputs",), dynamic_nodes=("retrieve",))
    sel = _matcher([c]).select(MatchContext.build(inputs=("a",), remaining_budget=10))
    assert sel.mode == "hybrid_agent"
    assert "retrieve" in sel.rationale


def test_dynamic_when_no_candidate():
    sel = _matcher([]).select(MatchContext.build())
    assert sel.mode == "dynamic_planning"
    assert "动态" in sel.rationale
    assert sel.workflow_id == ""  # 无隐式默认工作流


def test_dynamic_when_multi_gap_no_tolerate():
    c = _c(required_inputs=("a", "b"))
    sel = _matcher([c]).select(MatchContext.build(inputs=("a",), remaining_budget=10))
    assert sel.mode == "dynamic_planning"
    assert "b" in sel.rationale  # 理由列出缺什么


# ---------------------------------------------------------------- 3. 确定性
def test_candidate_order_independent():
    c1 = _c(wid="w1", required_inputs=("a",), cost_estimate=5)
    c2 = _c(wid="w2", required_inputs=("a",), cost_estimate=7)
    ctx = MatchContext.build(inputs=("a",), remaining_budget=20)
    sel1 = _matcher([c1, c2]).select(ctx)
    sel2 = _matcher([c2, c1]).select(ctx)
    assert sel1.workflow_id == sel2.workflow_id == "w1"  # 取最低成本
    assert sel1.rationale == sel2.rationale


def test_input_order_independent():
    c = _c(required_inputs=("a", "b"))
    v1 = _matcher([c]).evaluate(c, MatchContext.build(inputs=("a", "b")))
    v2 = _matcher([c]).evaluate(c, MatchContext.build(inputs=("b", "a")))
    assert v1.applicable == v2.applicable


# ---------------------------------------------------------------- 4. 预算不越权
def test_matching_read_only_no_deduction():
    c = _c(cost_estimate=10)
    ctx = MatchContext.build(remaining_budget=50)
    m = _matcher([c])
    before = ctx.remaining_budget
    m.select(ctx)
    assert ctx.remaining_budget == before  # 匹配阶段不扣账
    # 匹配器不持有任何预算权威
    assert not hasattr(m, "budget_controller")


# ---------------------------------------------------------------- 5. 评测维度
def test_workflow_mode_eval_builtin():
    cases = builtin_workflow_mode_dataset()
    report = WorkflowModeEvaluator().run(cases)
    assert report.case_count == len(cases)
    assert report.mode_accuracy == 1.0
    assert report.workflow_case_count >= 1
    assert report.workflow_accuracy == 1.0
    assert set(report.by_mode.keys()) >= {
        "stable_workflow", "hybrid_agent", "dynamic_planning"}
    assert report.summary()["case_count"] == len(cases)


def test_workflow_mode_eval_catches_wrong_mode():
    # 故意给错期望模式 → 该用例判错，准确率下降，证明评测有判别力
    cases = builtin_workflow_mode_dataset()
    cases[0] = cases[0].__class__(
        case_id=cases[0].case_id, context=cases[0].context,
        expected_mode="dynamic_planning", expected_workflow_id="",
        candidates=cases[0].candidates, difficulty=cases[0].difficulty,
        holdout=cases[0].holdout, tags=list(cases[0].tags))
    report = WorkflowModeEvaluator().run(cases)
    assert report.mode_accuracy < 1.0


# ---------------------------------------------------------------- 6. planner 接入
def test_make_plan_records_execution_mode():
    c = _c(required_inputs=("a",), cost_estimate=5)
    ctx = MatchContext.build(inputs=("a",), remaining_budget=10)
    eng = PlanningEngine()
    plan = eng.make_plan("做点事", match_context=ctx, workflow_candidates=[c])
    assert plan.execution_mode == "stable_workflow"
    assert plan.selection_rationale
    d = plan.to_dict()
    assert d["execution_mode"] == "stable_workflow"
    p2 = ExecutionPlan.from_dict(d)
    assert p2.execution_mode == "stable_workflow"
    assert p2.selection_rationale == plan.selection_rationale


def test_make_plan_without_context_leaves_mode_empty():
    eng = PlanningEngine()
    plan = eng.make_plan("做点事")
    assert plan.execution_mode == ""
    assert plan.selection_rationale == ""


def test_engine_select_execution_mode_passthrough():
    c = _c(required_inputs=(), cost_estimate=0)
    eng = PlanningEngine(workflow_candidates=[c])
    sel = eng.select_execution_mode(MatchContext.build())
    assert sel.mode == "stable_workflow"
    assert eng.last_selection is sel


def test_execution_plan_has_mode_fields():
    # 回归护栏：plan 必须记录模式字段（缺口证明点）
    assert "execution_mode" in ExecutionPlan.__dataclass_fields__
    assert "selection_rationale" in ExecutionPlan.__dataclass_fields__


# ---------------------------------------------------------------- 7. routing_coverage 标记
def test_routing_coverage_marks_workflow_and_mode_evaluated():
    cov = {c["layer"]: c for c in routing_coverage()}
    assert cov["workflow"]["evaluated"] is True
    assert cov["workflow"]["gap"] == ""
    assert cov["execution_mode"]["evaluated"] is True
    assert cov["execution_mode"]["metrics"]


# ---------------------------------------------------------------- 序列化往返
def test_selection_from_dict_roundtrip():
    c = _c(required_inputs=("a",), cost_estimate=5)
    ctx = MatchContext.build(inputs=("a",), remaining_budget=10)
    sel = _matcher([c]).select(ctx)
    back = selection_from_dict(sel.to_dict())
    assert back.mode == sel.mode
    assert back.workflow_id == sel.workflow_id
    assert back.rationale == sel.rationale
    assert back.verdicts and back.verdicts[0].checks


def test_execution_modes_enum_complete():
    assert set(EXECUTION_MODES) == {
        "stable_workflow", "hybrid_agent", "dynamic_planning"}


def test_check_names_enum_complete():
    assert tuple(CHECK_NAMES) == (
        "inputs", "permissions", "dependencies", "versions", "budget")
