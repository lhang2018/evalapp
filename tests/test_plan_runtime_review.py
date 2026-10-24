"""Real ReAct/gateway probes for plan dependency and resume safety."""
import copy
import json

import pytest

from evoagent.app import App
from evoagent.core.budget import BudgetController, BudgetLimits, BudgetUsage
from evoagent.core.controller import RuntimeController
from evoagent.core.permissions import PermissionController
from evoagent.core.persistence import Database
from evoagent.core.task import Task, TaskStatus
from evoagent.models.base import Action
from evoagent.models.mock import MockModel
from evoagent.planning.planner import ExecutionPlan, PlanRevisionError, PlanStep
from evoagent.react.engine import ReactEngine
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.tools.registry import ToolRegistry


class ScriptModel(MockModel):
    def __init__(self, actions):
        super().__init__()
        self.actions = list(actions)
        self.calls = 0

    def suggest_action(self, messages, tools):
        self.calls += 1
        return self.actions.pop(0) if self.actions else Action("finish", answer="done")


def good():
    return ToolResult(ok=True, submitted=True, executed=True, contract_valid=True)


def step(sid, tool=None, deps=(), state="pending"):
    return PlanStep(sid, sid, deps=list(deps), tool_hint=tool, state=state)


def plan(*steps, completed=()):
    return ExecutionPlan("p", "g", list(steps), completed_work=list(completed),
                         acceptance_criteria=[{"type": "tool_succeeded"}])


@pytest.fixture
def gateway(tmp_path):
    db = Database(str(tmp_path / "state.db"))
    registry = ToolRegistry(PermissionController(high_risk_mode="allow"), db)
    yield registry
    db.close()


def run(registry, p, actions, **kwargs):
    model = ScriptModel(actions)
    outcome = ReactEngine(model, registry).run([], BudgetController(BudgetLimits()),
        plan=p, task_id="task", run_id="run", **kwargs)
    return outcome, model


def test_execution_progress_respects_dependencies_not_storage_order(gateway):
    calls = []
    for tool in ("root", "child"):
        gateway.register(ToolContract(tool), lambda _, name=tool: calls.append(name) or good())
    p = plan(step("c", "child", ["r"]), step("r", "root"))
    outcome, _ = run(gateway, p, [Action("tool_call", tool="root"), Action("tool_call", tool="child")])
    assert outcome.finished and calls == ["root", "child"]
    assert p.completed_work == ["r", "c"]


@pytest.mark.parametrize("state", ["failed", "skipped"])
def test_blocked_dependency_stops_before_model_and_runner(gateway, state):
    calls = []
    gateway.register(ToolContract("child", side_effect_level="external"), lambda _: calls.append(1) or good())
    p = plan(step("r", state=state), step("c", "child", ["r"]))
    outcome, model = run(gateway, p, [Action("tool_call", tool="child")])
    assert calls == [] and model.calls == 0
    assert outcome.stop_reason == "plan_blocked" and not outcome.finished
    assert gateway.effects.history("task") == []


def test_completed_plan_does_not_authorize_additional_tool(gateway):
    calls = []
    gateway.register(ToolContract("write", side_effect_level="external"), lambda _: calls.append(1) or good())
    p = plan(step("s", "write", state="done"), completed=["s"])
    outcome, _ = run(gateway, p, [Action("tool_call", tool="write", args={"new": "operation"})])
    assert not calls and outcome.stop_reason == "plan_complete"
    assert p.completed_work == ["s"]


def test_unrelated_tool_failure_does_not_fail_current_step(gateway):
    gateway.register(ToolContract("other", retry_policy=0), lambda _: ToolResult.error_result("no", "invalid_arguments"))
    p = plan(step("s", "expected", state="running"))
    before = copy.deepcopy(p.to_dict())
    actions = [Action("tool_call", tool="other", args={"n": i}) for i in range(3)]
    outcome, _ = run(gateway, p, actions)
    assert outcome.stop_reason == "repeated_failure"
    assert p.to_dict() == before


def test_previous_step_failures_do_not_fail_next_step(gateway):
    gateway.register(ToolContract("t", retry_policy=0), lambda args: good() if args["n"] == 2 else ToolResult.error_result("no", "invalid_arguments"))
    p = plan(step("a", "t"), step("b", "t", ["a"]))
    outcome, _ = run(gateway, p, [Action("tool_call", tool="t", args={"n": i}) for i in range(4)])
    assert outcome.stop_reason == "normal"
    assert p.completed_work == ["a"] and p.steps[1].state == "running"


@pytest.mark.parametrize("p", [
    plan(), plan(step("x"), step("x")),
    plan(step("a", deps=["b"]), step("b", deps=["a"])),
    plan(step("x", state="invalid")), plan(step("x"), completed=["x"]),
    plan(step("x", tool=False)),
])
def test_invalid_execution_state_is_rejected_before_dispatch(gateway, p):
    model = ScriptModel([Action("tool_call", tool="t")])
    with pytest.raises(PlanRevisionError):
        ReactEngine(model, gateway).run([], BudgetController(BudgetLimits()), plan=p)
    assert model.calls == 0


@pytest.mark.parametrize("raw", [None, [], {"steps": "corrupt"},
    {"plan_id": "p", "steps": [{"step_id": "s", "description": "s"}, None]}])
def test_persisted_corrupt_plan_cannot_become_unplanned_execution(raw):
    task = Task(user_goal="g")
    task.runtime_manifest["execution_plan"] = raw
    with pytest.raises(PlanRevisionError):
        RuntimeController._plan_from_task(task)


def test_wrong_plan_reference_is_rejected():
    task = Task(user_goal="g")
    task.execution_plan_ref = "expected"
    task.runtime_manifest["execution_plan"] = plan(step("s")).to_dict()
    with pytest.raises(PlanRevisionError):
        RuntimeController._plan_from_task(task)


def test_submitted_only_success_does_not_become_completed_work(gateway):
    gateway.register(ToolContract("t"), lambda _: ToolResult(ok=True, submitted=True))
    p = plan(step("s", "t"))
    run(gateway, p, [Action("tool_call", tool="t")])
    assert p.completed_work == [] and p.steps[0].state == "running"


def test_recovery_checkpoint_preserves_evidence_and_blocks_failed_branch(gateway, tmp_path):
    calls = []
    gateway.register(ToolContract("t", retry_policy=0), lambda _: calls.append("failed") or ToolResult.error_result("no", "invalid_arguments"))
    gateway.register(ToolContract("child"), lambda _: calls.append("child") or good())
    p = plan(step("done", state="done"), step("broken", "t", ["done"]), step("child", "child", ["broken"]), completed=["done"])
    file = tmp_path / "checkpoint.json"
    budget = BudgetController(BudgetLimits())
    def checkpoint(state):
        file.write_text(json.dumps({"plan": p.to_dict(), "react": state, "budget": budget.snapshot()}), encoding="utf-8")
    first = ReactEngine(ScriptModel([Action("tool_call", tool="t", args={"n": i}) for i in range(3)]), gateway).run(
        [], budget, plan=p, task_id="task", run_id="run", on_checkpoint=checkpoint)
    assert first.stop_reason == "repeated_failure" and p.revisions[-1]["level"] == "append"
    saved = json.loads(file.read_text(encoding="utf-8"))
    restored = ExecutionPlan.from_dict(saved["plan"])
    model = ScriptModel([Action("tool_call", tool="child")])
    restored_budget = BudgetController(BudgetLimits(), initial_usage=BudgetUsage.from_dict(saved["budget"]["usage"]))
    before_calls = restored_budget.usage.model_calls
    second = ReactEngine(model, gateway).run([], restored_budget, plan=restored,
        task_id="task", run_id="resumed", restored_state=saved["react"])
    assert second.stop_reason == "plan_blocked" and model.calls == 0
    assert restored_budget.usage.model_calls == before_calls
    assert calls == ["failed"] * 3 and restored.completed_work == ["done"]


def test_real_controller_suspends_blocked_plan_instead_of_claiming_success(tmp_path):
    app = App(data_dir=str(tmp_path / "app"))
    try:
        p = plan(step("bad", state="failed"), step("time", "current_time", ["bad"]))
        app.controller.planner.make_plan = lambda *a, **kw: p
        task = app.submit("现在几点了", intent="time_query", acceptance=[{"type": "tool_succeeded"}])
        assert task.task_status == TaskStatus.SUSPENDED
        assert app.controller.cp_mgr.latest(task.task_id).react_state["stop_reason"] == "plan_blocked"
        assert app.db.list_side_effects_by_task(task.task_id) == []
    finally:
        app.db.close()


def test_real_controller_rejects_corruption_instead_of_dropping_plan(tmp_path):
    app = App(data_dir=str(tmp_path / "app"))
    try:
        p = plan(step("time", "current_time"))
        p.to_dict = lambda: {"plan_id": "p", "steps": "corrupt"}
        app.controller.planner.make_plan = lambda *a, **kw: p
        task = app.submit("现在几点了", intent="time_query", acceptance=[{"type": "tool_succeeded"}])
        assert task.task_status == TaskStatus.FAILED
        assert "PlanRevisionError" in task.failure_reason
    finally:
        app.db.close()


def test_legacy_absent_plan_and_normal_finish_remain_supported(gateway):
    assert RuntimeController._plan_from_task(Task(user_goal="legacy")) is None
    p = plan(step("s", state="done"), completed=["s"])
    outcome, _ = run(gateway, p, [Action("finish", answer="done")])
    assert outcome.finished


def test_resumes_refresh_one_plan_context_slot_without_accumulating_stale_text(gateway):
    p = plan(step("bad", state="failed"), step("child", deps=["bad"]))
    state = {"messages": [{"role": "system", "content": "execution_plan:stale"}], "next_iteration": 1}
    for _ in range(3):
        outcome, model = run(gateway, p, [], restored_state=state)
        assert model.calls == 0 and outcome.stop_reason == "plan_blocked"
        state = outcome.continuation
        contexts = [m["content"] for m in state["messages"] if m["content"].startswith("execution_plan:")]
        assert len(contexts) == 1 and "stale" not in contexts[0] and "[failed] bad" in contexts[0]


def test_recovery_step_still_uses_gateway_whitelist(gateway):
    calls = []
    gateway.register(ToolContract("restricted"), lambda _: calls.append(1) or good())
    p = plan(step("recover", "restricted"))
    outcome, _ = run(gateway, p, [Action("tool_call", tool="restricted", args={"n": i}) for i in range(3)], allowed_tools=[])
    assert calls == [] and outcome.stop_reason == "repeated_failure"
    assert all(s.tool_result.error_class == "auth_or_permission" for s in outcome.steps)


def test_legacy_unscoped_failure_counts_do_not_condemn_current_step(gateway):
    gateway.register(ToolContract("t", retry_policy=0), lambda _: ToolResult.error_result("no", "invalid_arguments"))
    p = plan(step("s", "t", state="running"))
    outcome, _ = run(gateway, p, [Action("tool_call", tool="t")],
                     restored_state={"failure_counts": {"t:invalid_arguments": 99}})
    assert outcome.stop_reason == "normal" and outcome.repeated_failures == 1
    assert p.steps[0].state == "running" and p.revisions == []


@pytest.mark.parametrize("state", ["failed", "skipped", "pending", "running"])
def test_ready_independent_branch_cannot_unlock_unready_branch_tool(gateway, state):
    calls = []
    gateway.register(ToolContract("child", side_effect_level="external"), lambda _: calls.append(1) or good())
    p = plan(step("bad", "root", state=state), step("independent", "prep"), step("child", "child", ["bad"]))
    outcome, model = run(gateway, p, [Action("tool_call", tool="child")])
    assert outcome.stop_reason == "plan_blocked" and model.calls == 1
    assert calls == [] and gateway.effects.history("task") == []
    assert p.completed_work == [] and p.steps[-1].state == "pending"


def test_ready_step_sharing_tool_with_blocked_step_is_still_executable(gateway):
    calls = []
    gateway.register(ToolContract("shared"), lambda _: calls.append(1) or good())
    p = plan(step("bad", state="failed"), step("ready", "shared"), step("blocked", "shared", ["bad"]))
    outcome, _ = run(gateway, p, [Action("tool_call", tool="shared")])
    assert calls == [1] and p.completed_work == ["ready"]
    assert outcome.stop_reason == "plan_blocked" and p.steps[-1].state == "pending"


def test_generic_preparation_can_use_tool_also_hinted_in_later_step(gateway):
    calls = []
    gateway.register(ToolContract("lookup"), lambda _: calls.append(1) or good())
    p = plan(step("prepare"), step("core", "lookup", ["prepare"]))
    outcome, _ = run(gateway, p, [Action("tool_call", tool="lookup")])
    assert calls == [1] and p.completed_work == ["prepare"] and outcome.finished
