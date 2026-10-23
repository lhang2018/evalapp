"""可执行恢复策略（ALN-07-R）回归。

夹具先证缺口：`RecoveryPolicy` / `plan.recovery_policies` /
`plan.recovery_for_failure` 不存在即 import/属性失败。
"""
import pytest

from evoagent.planning.planner import (ExecutionPlan, PlanningEngine,
                                       PlanStep, RecoveryPolicy)


# ---- 验收 1：已知意图策略与原文案语义一致（文案由策略渲染） ----
def test_note_taking_policy():
    eng = PlanningEngine()
    plan = eng.make_plan("记笔记", intent="note_taking")
    assert len(plan.recovery_policies) == 1
    p = plan.recovery_policies[0]
    assert p.category == "side_effect_requires_approval"
    assert p.action == "require_approval"
    assert "副作用" in p.rationale and "权限" in p.rationale
    # 兼容读法：risks 文案来自策略渲染
    assert plan.risks == [p.rationale]


def test_comic_drama_policy():
    plan = PlanningEngine().make_plan("做漫剧", intent="comic_drama")
    (p,) = plan.recovery_policies
    assert p.action == "partial_retry"
    assert "局部重试" in p.rationale
    assert plan.risks == [p.rationale]


def test_general_intent_no_policy_no_risks():
    plan = PlanningEngine().make_plan("你好", intent="general")
    assert plan.recovery_policies == []
    assert plan.risks == []


# ---- 验收 2：未知意图空策略，不隐式默认 ----
def test_unknown_intent_empty():
    plan = PlanningEngine().make_plan("随便", intent="unknown_x")
    assert plan.recovery_policies == []
    assert plan.risks == []


# ---- 验收 3：recovery_for_failure 只对失败步骤返回 ----
def _plan_with_policies():
    plan = ExecutionPlan(
        plan_id="p", goal="g",
        steps=[PlanStep(step_id="done1", description="完成", state="done"),
               PlanStep(step_id="fail1", description="失败", state="failed"),
               PlanStep(step_id="pend1", description="待定")],
        completed_work=["done1"],
        recovery_policies=[RecoveryPolicy(
            category="high_cost_partial_retry", action="partial_retry")])
    return plan


def test_failure_step_gets_policy():
    plan = _plan_with_policies()
    got = plan.recovery_for_failure("fail1")
    assert len(got) == 1 and got[0].action == "partial_retry"


def test_done_step_never_gets_policy():
    # 不得重放成功副作用
    plan = _plan_with_policies()
    assert plan.recovery_for_failure("done1") == []


def test_pending_and_unknown_steps_empty():
    plan = _plan_with_policies()
    assert plan.recovery_for_failure("pend1") == []
    assert plan.recovery_for_failure("nope") == []


# ---- 验收 4：序列化往返 + 旧计划兼容 ----
def test_roundtrip():
    plan = _plan_with_policies()
    d = plan.to_dict()
    assert d["recovery_policies"][0]["action"] == "partial_retry"
    back = ExecutionPlan.from_dict(d)
    assert back.recovery_for_failure("fail1")[0].action == "partial_retry"


def test_legacy_plan_without_field():
    d = {"plan_id": "p", "goal": "g",
         "steps": [{"step_id": "s1", "description": "x"}]}
    plan = ExecutionPlan.from_dict(d)
    assert plan.recovery_policies == []
    assert plan.recovery_for_failure("s1") == []


# ---- 验收 5：确定性 ----
def test_deterministic():
    a = PlanningEngine().make_plan("记笔记", intent="note_taking")
    b = PlanningEngine().make_plan("记笔记", intent="note_taking")
    assert [p.to_dict() for p in a.recovery_policies] == \
        [p.to_dict() for p in b.recovery_policies]


# ---- 纯读：查询不改计划 ----
def test_query_is_read_only():
    plan = _plan_with_policies()
    before = plan.to_dict()
    plan.recovery_for_failure("fail1")
    assert plan.to_dict() == before
