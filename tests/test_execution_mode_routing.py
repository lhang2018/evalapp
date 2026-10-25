"""ALN-08-W：工作流与执行模式主链路由测试。

覆盖验收：入口与评测一致；不可用默认不旁路使用；决策与真实执行归因一致。
"""

import pytest

from evoagent.app import App
from evoagent.core.task import Task
from evoagent.experts.registry import Expert
from evoagent.planning.matching import WorkflowMatcher
from evoagent.routing.mode_router import (DYNAMIC, STABLE, ModeRouter,
                                          candidate_from_workflow)
from evoagent.workflows.models import WorkflowDef, WorkflowNode


@pytest.fixture
def app(tmp_path):
    application = App(data_dir=str(tmp_path))
    yield application
    application.db.close()


def register_expert(app, expert_id, workflow_ids):
    expert = Expert(expert_id=expert_id, applicable_intents=[expert_id],
                    workflow_ids=list(workflow_ids), tool_whitelist=None)
    app.experts.register_expert(expert)
    return expert


def simple_workflow(workflow_id="wf_ok"):
    return WorkflowDef(
        workflow_id=workflow_id, version="1.0.0", name="两数相加",
        nodes=[WorkflowNode(node_id="calc", tool="calculate",
                            args={"expression": "1+1"})],
        acceptance_criteria=[{"type": "result_nonempty"}])


def workflow_requiring_input(workflow_id="wf_need_theme"):
    return WorkflowDef(
        workflow_id=workflow_id, version="1.0.0", name="需要主题的工作流",
        input_schema={"type": "object", "required": ["theme"]},
        nodes=[WorkflowNode(node_id="calc", tool="calculate",
                            args={"expression": "2+2"})],
        acceptance_criteria=[{"type": "result_nonempty"}])


def test_main_chain_selects_workflow_and_records_consistent_attribution(app):
    app._workflows["wf_ok"] = simple_workflow()
    register_expert(app, "flowtest", ["wf_ok"])
    task = app.controller.submit_task("算一下 1+1", intent="flowtest", wait=True)
    manifest = task.runtime_manifest
    selection = manifest["mode_selection"]
    assert selection["mode"] == STABLE
    assert selection["workflow_id"] == "wf_ok"
    assert manifest["execution_mode"] == STABLE
    assert manifest["mode_rejections"] == []
    attribution = manifest["mode_attribution"]
    assert attribution["consistent"] is True
    assert attribution["decision"] == STABLE and attribution["actual"] == STABLE
    # 真实执行确实走了工作流（节点执行事件/工作流引用都在）
    assert task.workflow_ref == "wf_ok"
    events = [e for e in app.bus.query(task_id=task.task_id)
              if e.event_type == "execution_mode_selected"]
    assert events and events[-1].payload["workflow_id"] == "wf_ok"


def test_unavailable_workflow_is_not_silently_bypassed(app):
    app._workflows["wf_need_theme"] = workflow_requiring_input()
    register_expert(app, "flowreq", ["wf_need_theme"])
    task = app.controller.submit_task("做个东西", intent="flowreq", wait=True)
    manifest = task.runtime_manifest
    selection = manifest["mode_selection"]
    assert selection["mode"] == DYNAMIC
    assert manifest["execution_mode"] == DYNAMIC
    # 不可用原因可查（缺 inputs:theme），不是隐式默认
    rejections = manifest["mode_rejections"]
    assert rejections and rejections[0]["workflow_id"] == "wf_need_theme"
    assert "inputs:theme" in rejections[0]["missing"]
    assert task.workflow_ref is None
    assert manifest["mode_attribution"]["consistent"] is True
    assert manifest["mode_attribution"]["actual"] == DYNAMIC


def test_pinned_unavailable_workflow_is_rejected_explicitly(app):
    from evoagent.core.task import TaskStatus

    app._workflows["wf_need_theme"] = workflow_requiring_input()
    register_expert(app, "flowreq", ["wf_need_theme"])
    task = Task(user_goal="按指定工作流执行", intent="flowreq", domain="flowreq")
    task.task_status = TaskStatus.CLASSIFYING
    task.workflow_ref = "wf_need_theme"   # 上游钉住的工作流当前缺输入
    app.db.save_task(task)                # 运行时预算绑定要求任务已登记
    budget = app.controller._make_budget(task)
    planned = app.controller._plan(task, budget)
    manifest = planned.runtime_manifest
    assert manifest["workflow_ref_rejected"]["workflow_ref"] == "wf_need_theme"
    assert planned.workflow_ref is None
    assert manifest["execution_mode"] == DYNAMIC
    assert manifest["mode_selection"]["mode"] == DYNAMIC


def test_decision_uses_same_matcher_entry_as_evaluation(app):
    app._workflows["wf_ok"] = simple_workflow()
    router = ModeRouter(app._wf_lookup)
    candidates = router.candidates(["wf_ok"])
    # 与评测相同的入口：同一上下文必须得到同一选择结果
    selection = router.decide(goal="1+1", intent="flowtest",
                              workflow_ids=["wf_ok"], budget=None)
    context = router.context(goal="1+1", intent="flowtest",
                             candidates=candidates, budget=None)
    direct = WorkflowMatcher(candidates).select(context)
    assert selection.to_dict() == direct.to_dict()
    assert selection.mode == DYNAMIC  # 预算不可知 → 不隐式当成充足


def test_budget_unknown_is_not_treated_as_sufficient(app):
    app._workflows["wf_ok"] = simple_workflow()
    router = ModeRouter(app._wf_lookup)
    selection = router.decide(goal="1+1", intent="flowtest",
                              workflow_ids=["wf_ok"], budget=None)
    assert selection.mode == DYNAMIC
    assert "预算不可知" in selection.rationale


def test_attribution_mismatch_is_flagged(app):
    task = Task(user_goal="mismatch")
    task.runtime_manifest["mode_selection"] = {
        "mode": STABLE, "workflow_id": "wf_ok", "rationale": "test"}
    app.controller._record_mode_attribution(task, DYNAMIC)
    attribution = task.runtime_manifest["mode_attribution"]
    assert attribution["consistent"] is False
    assert attribution["decision"] == STABLE and attribution["actual"] == DYNAMIC
    events = [e for e in app.bus.query(task_id=task.task_id)
              if e.event_type == "execution_mode_mismatch"]
    assert events, "决策与实际不一致必须发出显式告警事件"


def test_candidate_mapping_matches_workflow_definition(app):
    workflow = workflow_requiring_input()
    candidate = candidate_from_workflow(workflow)
    assert candidate.workflow_id == "wf_need_theme"
    assert candidate.version == "1.0.0"
    assert candidate.required_inputs == ("theme",)
    assert candidate.required_permissions == ("tool:calculate",)
    assert candidate.cost_estimate == 1
