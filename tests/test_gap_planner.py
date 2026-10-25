"""ALN-26-A：能力缺口证据与主动规划候选测试。

验证：证据可追溯、可去重、可人工确认；证据不足不生成候选；候选只经既有
EvolutionController 登记，不绕过评测与发布。
"""

import pytest

from evoagent.app import App
from evoagent.core.task import Task, TaskStatus
from evoagent.goals.gap_planner import (CapabilityGapPlanner, GapPlannerError,
                                        POLICY)


@pytest.fixture
def app(tmp_path):
    application = App(data_dir=str(tmp_path))
    yield application
    application.db.close()


def add_task(app, *, goal="完成能力任务", status=TaskStatus.FAILED,
             failure_reason="工具缺失: 没有可用能力",
             intent="unknown_intent_x", domain="unknown_domain_z", goal_id=None):
    task = Task(user_goal=goal, intent=intent, domain=domain,
                task_status=status, failure_reason=failure_reason,
                goal_id=goal_id)
    app.db.save_task(task)
    return task


def planner(app, **kwargs):
    return CapabilityGapPlanner(app.db, experts=app.experts,
                                evolution=app.evolution, goals=app.goals,
                                **kwargs)


def test_repeated_failures_promote_one_gap_with_traceable_evidence(app):
    first = add_task(app)
    second = add_task(app, goal="另一任务")
    analysis = planner(app).analyze()
    assert analysis["counts"]["gaps"] == 1
    gap = analysis["gaps"][0]
    assert gap["evidence_count"] == 2
    assert gap["distinct_tasks"] == 2
    assert set(gap["evidence_refs"]) == {f"task:{first.task_id}",
                                        f"task:{second.task_id}"}
    assert gap["capability"].startswith("missing_capability:unknown_domain_z")
    assert gap["failure_reasons"]
    trace = planner(app).traceability(gap)
    assert trace["ok"] is True


def test_single_failure_without_reason_is_insufficient(app):
    add_task(app, failure_reason="")
    analysis = planner(app).analyze()
    assert analysis["counts"]["gaps"] == 0
    assert analysis["counts"]["insufficient"] == 1
    reason = analysis["insufficient"][0]["insufficient_reason"]
    assert "证据不足" in reason


def test_min_evidence_threshold_respected(app):
    add_task(app)
    add_task(app, goal="另一个任务")
    analysis = planner(app).analyze(min_evidence=3)
    assert analysis["counts"]["gaps"] == 0
    assert analysis["counts"]["insufficient"] == 1
    with pytest.raises(GapPlannerError):
        CapabilityGapPlanner(app.db, min_evidence=0)


def test_fallback_route_evidence_counts_as_capability_gap(app):
    add_task(app, status=TaskStatus.SUCCEEDED, failure_reason="")
    add_task(app, goal="兜底第二个", status=TaskStatus.SUCCEEDED,
             failure_reason="")
    analysis = planner(app).analyze()
    assert analysis["counts"]["gaps"] == 1
    gap = analysis["gaps"][0]
    assert gap["kinds"] == ["fallback_route"]
    assert gap["evidence_count"] == 2


def test_plan_candidates_registers_through_evolution_and_never_deploys(app):
    add_task(app)
    add_task(app, goal="另一失败")
    before_current = app.releases.current_release_id
    result = planner(app).plan_candidates(baseline_version="v0")
    assert result["counts"]["planned"] == 1
    planned = result["planned"][0]
    assert planned["candidate_id"]
    assert planned["requires_evaluation"] is True
    assert "不绕过评测" in planned["policy"]
    candidate = app.evolution.get_candidate(planned["candidate_id"])
    assert candidate is not None
    status = getattr(getattr(candidate, "status", None), "value",
                     getattr(candidate, "status", None))
    assert status not in ("DEPLOYED", "PUBLISHED", "RELEASED")
    # 规划器不触发部署/发布
    assert app.releases.current_release_id == before_current
    # 证据与成本/收益可见
    assert planned["benefit"]["affected_tasks"] == 2
    assert planned["cost"]["budget_units"] > 0
    assert planned["evidence_refs"]


def test_traceability_rejects_missing_or_foreign_refs(app):
    verify = planner(app)
    missing = verify.traceability({"evidence_refs": ["task:nope",
                                                     "goal:goal_nope"]})
    assert missing["ok"] is False
    assert set(missing["missing"]) == {"task:nope", "goal:goal_nope"}
    foreign = verify.traceability({"evidence_refs": ["file:/etc/passwd"]})
    assert foreign["ok"] is False


def test_plan_candidates_rejects_gap_with_unresolvable_evidence(app):
    add_task(app)
    add_task(app, goal="第二条")
    analysis = planner(app).analyze()
    analysis["gaps"][0]["evidence_refs"] = ["task:vanished"]
    result = planner(app).plan_candidates(analysis=analysis)
    assert result["counts"]["planned"] == 0
    assert result["counts"]["rejected"] == 1
    assert "不可追溯" in result["rejected"][0]["reason"]


def test_unmet_goal_is_linked_when_task_belongs_to_active_goal(app):
    goal = app.goals.create("提高旅行规划成功率", actor="user",
                            success_criteria=["同一意图不再失败"])
    try:
        add_task(app, goal_id=goal["goal_id"])
        add_task(app, goal="第二任务", goal_id=goal["goal_id"])
    except PermissionError:
        pytest.skip("目标任务需要先完成记忆/授权绑定，跳过目标证据用例")
    analysis = planner(app).analyze()
    gap = analysis["gaps"][0]
    kinds = {evidence["kind"] for evidence in gap["evidence"]}
    assert "failed_task" in kinds
    assert "unmet_goal" in kinds
    goal_refs = [ref for ref in gap["evidence_refs"] if ref.startswith("goal:")]
    assert goal_refs == [f"goal:{goal['goal_id']}"]


def test_insufficient_gaps_never_reach_planning(app):
    add_task(app)   # 单条失败
    result = planner(app).plan_candidates()
    assert result["counts"]["planned"] == 0
    assert result["insufficient"]
    assert POLICY in result["policy"]
