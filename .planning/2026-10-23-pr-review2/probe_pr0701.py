"""ALN-07-W / ALN-07-P 修复第三方复核独立探针（claude-main，2026-10-23）。

不依赖被审方测试文件，从行为契约出发逐条复现：

PR-07（ALN-07-W，修复 1ff11c1 + 34b5fac）：
  P1 硬门禁可被 dynamic_tolerates 放宽——缺权限/依赖/版本/预算的候选
     只要声明 tolerates 就混进 hybrid；
  P2 cost_estimate=0（未声明成本）时预算不可知也通过——静默假设充足；
  P3 重复 (workflow_id, version) 不报错，选择结果随候选顺序漂移；
  P4 评测器把「模式对但工作流错」「wrong-workflow」的用例计入 mode_accuracy。

PR-01 延伸（ALN-07-P，修复 7132180）：
  P5 append 级修订改写既有步骤仍被接受（只查 id 存在，不查内容）；
  P6 done/running 步骤被换内容重发仍被接受（completed_work 只查 state）。

34b5fac 契约加固：
  P7 WorkflowCandidate 恶意字段（空 id、负成本、字符串混入集合）被拒绝；
  P8 全过但带 dynamic_nodes 的候选选 hybrid 而非 stable（语义修正）。

判别力要求：每根探针在 a2e08a3（修复前）失败、在 34b5fac（修复后）通过。
"""
import sys

from evoagent.planning.contracts import MatchContext, WorkflowCandidate
from evoagent.planning.matching import WorkflowMatcher
from evoagent.planning.planner import ExecutionPlan, PlanRevisionError, PlanStep
from evoagent.evolution.routing_eval import WorkflowModeEvaluator

results = []


def probe(name, fn):
    try:
        fn()
        results.append((name, "PASS"))
    except (AssertionError, ValueError, TypeError) as exc:
        results.append((name, f"FAIL: {type(exc).__name__}: {exc}"))


def c(wid="w", **kw):
    return WorkflowCandidate(workflow_id=wid, **kw)


# ---- P1: 硬门禁不得被 dynamic_tolerates 放宽 ----
def p1():
    cand = c(required_permissions=("admin",),
             dynamic_tolerates=("permissions", "dependencies", "versions",
                                "budget"))
    sel = WorkflowMatcher([cand]).select(MatchContext.build(remaining_budget=100))
    assert sel.mode == "dynamic_planning", (
        f"缺权限候选混进 {sel.mode}（dynamic_tolerates 放宽了硬门禁）")


# ---- P2: cost=0 且预算不可知 → 不得静默通过 ----
def p2():
    cand = c(cost_estimate=0)
    ctx = MatchContext.build(remaining_budget=0, budget_known=False)
    sel = WorkflowMatcher([cand]).select(ctx)
    assert sel.mode == "dynamic_planning", (
        f"预算不可知但 cost=0 候选被选为 {sel.mode}")
    # 且理由可查
    assert "预算" in sel.rationale


# ---- P3: 重复身份必须拒绝，而不是绑定随顺序漂移 ----
def p3():
    a = c("dup", required_inputs=("x",), cost_estimate=5)
    b = c("dup", required_inputs=(), cost_estimate=99)
    try:
        WorkflowMatcher([a, b])
    except ValueError:
        return  # 修复后：构造即拒绝
    raise AssertionError("重复 (workflow_id,version) 未被拒绝")


# ---- P4: 评测指标不得混算（模式对≠整例对；wrong-workflow 不计入 mode） ----
def p4():
    cand = c("wf1", required_inputs=("a",))
    ctx = MatchContext.build(inputs=("a",), remaining_budget=10)
    from evoagent.evolution.routing_eval import WorkflowModeCase
    # 期望模式 stable_workflow 但期望工作流指错 → mode 应计 1、wf 应计 0
    case = WorkflowModeCase(case_id="x", context=ctx,
                            expected_mode="stable_workflow",
                            expected_workflow_id="OTHER",
                            candidates=[cand])
    rep = WorkflowModeEvaluator().run([case])
    assert rep.mode_accuracy == 1.0, "模式选择正确却未计入 mode_accuracy"
    assert rep.workflow_accuracy == 0.0, "工作流指错却计入 workflow_accuracy"


# ---- P5: append 级修订改写既有步骤必须拒绝 ----
def p5():
    plan = ExecutionPlan(
        plan_id="p", goal="g",
        steps=[PlanStep(step_id="s1", description="原始描述")])
    try:
        plan.adjust([PlanStep(step_id="s1", description="被偷换的内容")],
                    "append 偷改", level="append")
    except PlanRevisionError:
        return
    raise AssertionError("append 级修订改写了既有步骤 s1 的内容")


# ---- P6: done 步骤换内容重发必须拒绝（即使 state 仍是 done） ----
def p6():
    plan = ExecutionPlan(
        plan_id="p", goal="g", completed_work=["s1"],
        steps=[PlanStep(step_id="s1", description="已完成的真实步骤",
                        state="done")])
    try:
        plan.adjust([PlanStep(step_id="s1", description="篡改后的描述",
                              state="done")], "改写已完成步骤")
    except PlanRevisionError:
        return
    raise AssertionError("done 步骤内容被改写仍被接受")


# ---- P7: 契约防御——恶意字段拒绝 ----
def p7():
    bad = [
        lambda: c(""),                                    # 空 workflow_id
        lambda: c(cost_estimate=-1),                      # 负成本
        lambda: c(required_inputs="ab"),                  # 字符串冒充集合
        lambda: c(required_inputs=("a", "")),             # 集合含空串
    ]
    for i, make in enumerate(bad):
        try:
            make()
        except ValueError:
            continue
        raise AssertionError(f"恶意候选 #{i} 未被契约拒绝")


# ---- P8: 全过但带 dynamic_nodes → hybrid（语义修正，不是 stable） ----
def p8():
    cand = c("wf_full", dynamic_nodes=("decide",))
    sel = WorkflowMatcher([cand]).select(MatchContext.build(remaining_budget=10))
    assert sel.mode == "hybrid_agent", (
        f"带 dynamic_nodes 的全过候选被选为 {sel.mode}")
    assert "decide" in sel.rationale


probe("P1 hard-gates-not-tolerated", p1)
probe("P2 unknown-budget-not-assumed", p2)
probe("P3 duplicate-identity-rejected", p3)
probe("P4 mode/wf-metrics-separated", p4)
probe("P5 append-rewrite-rejected", p5)
probe("P6 done-content-rewrite-rejected", p6)
probe("P7 contract-defends-bad-fields", p7)
probe("P8 dynamic-nodes-require-hybrid", p8)

print("=" * 56)
for name, status in results:
    print(f"{status:8} {name}")
failed = [n for n, s in results if s.startswith("FAIL")]
print("=" * 56)
print(f"total={len(results)} pass={len(results)-len(failed)} fail={len(failed)}")
sys.exit(1 if failed else 0)
