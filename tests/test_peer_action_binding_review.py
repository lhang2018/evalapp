"""Independent probes for leased writes and runtime binding provenance."""
from concurrent.futures import ThreadPoolExecutor
import copy
import threading

import pytest

from evoagent.app import App
from evoagent.core.budget import BudgetController, BudgetLimits
from evoagent.core.leases import LeaseLost
from evoagent.core.permissions import PermissionController
from evoagent.core.persistence import Database
from evoagent.core.runs import RunManager
from evoagent.core.task import Task, TaskStatus
from evoagent.models.base import Action
from evoagent.models.mock import MockModel
from evoagent.planning.binding import ActionBindings, BindingConflict
from evoagent.planning.planner import ExecutionPlan, PlanStep
from evoagent.planning.reconcile import reconcile_plan
from evoagent.react.engine import ReactEngine
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.tools.registry import ToolRegistry


class ScriptModel(MockModel):
    def __init__(self, actions):
        super().__init__()
        self.actions = list(actions)

    def suggest_action(self, messages, tools):
        return self.actions.pop(0) if self.actions else Action("finish", answer="done")


def good(**evidence):
    return ToolResult(True, submitted=True, executed=True, contract_valid=True, evidence=evidence)


def plan(*steps):
    return ExecutionPlan("plan", "binding review", list(steps), acceptance_criteria=[{"type": "tool_succeeded"}])


def step(sid, tool=None, deps=()):
    return PlanStep(sid, sid, list(deps), tool_hint=tool)


@pytest.fixture
def db(tmp_path):
    value = Database(str(tmp_path / "state.db"))
    yield value
    value.close()


@pytest.mark.parametrize("mode", ["idempotent", "conflict", "invalid_timestamp"])
def test_leased_binding_paths_finish_their_transaction(db, mode):
    bindings = ActionBindings(db)
    task = Task(user_goal="binding")
    db.save_task(task)
    runs = RunManager(db)
    rid, started = runs.begin(task, {})
    try:
        bindings.bind("e", "s", task_id=task.task_id)
        if mode == "idempotent":
            bindings.bind("e", "s", task_id=task.task_id)
        elif mode == "conflict":
            with pytest.raises(BindingConflict):
                bindings.bind("e", "another", task_id=task.task_id)
        else:
            with pytest.raises((TypeError, ValueError)):
                bindings.bind("new", "s", task_id=task.task_id, now="bad")
        assert not db._conn.in_transaction
        db.save_task(task)  # Another real guarded write must remain usable.
        assert bindings.count(task.task_id) == 1
    finally:
        db._conn.rollback()  # Clean up the original defect before finishing the lease.
        runs.finish(task, rid, started)


def test_binding_conflict_uses_same_domain_error_across_connections(tmp_path):
    first = Database(str(tmp_path / "shared.db"))
    second = Database(first.path)
    barrier = threading.Barrier(2)

    class CoordinatedBindings(ActionBindings):
        def _select(self, effect_id):
            # Make the old unlocked read/insert race reproducible. Correct
            # code starts an immediate transaction before consulting the row.
            row = super()._select(effect_id)
            if not self.db._conn.in_transaction:
                barrier.wait(timeout=3)
            return row

    def attempt(db, sid):
        try:
            CoordinatedBindings(db).bind("effect", sid, task_id="task")
            return "bound"
        except BindingConflict:
            return "conflict"

    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(attempt, first, "s1"), pool.submit(attempt, second, "s2")]
            assert sorted(f.result() for f in futures) == ["bound", "conflict"]
        assert not first._conn.in_transaction and not second._conn.in_transaction
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("changed_tool", ["different", 123])
def test_tool_identity_conflict_cannot_silently_succeed(db, changed_tool):
    bindings = ActionBindings(db)
    before = bindings.bind("e", "s", task_id="task", tool_id="send")
    with pytest.raises(ValueError):
        bindings.bind("e", "s", task_id="task", tool_id=changed_tool)
    assert bindings.get("e") == before


def registry(db):
    return ToolRegistry(PermissionController(high_risk_mode="allow"), db)


def run(db, p, actions, *, tools=None, bindings=None, **kwargs):
    tools = tools or registry(db)
    bindings = bindings if bindings is not None else ActionBindings(db)
    engine = ReactEngine(ScriptModel(actions), tools, effects=tools.effects, bindings=bindings)
    outcome = engine.run([], BudgetController(BudgetLimits()), plan=p, **kwargs)
    return outcome, bindings, tools


@pytest.mark.parametrize("hint", [None, "expected"])
def test_unmatched_or_generic_step_does_not_export_false_reconciler_binding(db, hint):
    tools = registry(db)
    tools.register(ToolContract("send", side_effect_level="external"), lambda _: good())
    p = plan(step("current", hint))
    outcome, bindings, _ = run(db, p, [Action("tool_call", tool="send")], tools=tools, task_id="task", run_id="run")
    assert len(tools.effects.history("task")) == 1
    assert bindings.for_task("task") == {}
    assert outcome.steps[0].tool_result.evidence["step_binding_skipped"]


def test_run_identifier_alone_is_not_an_explicit_task_scope(db):
    tools = registry(db)
    tools.register(ToolContract("send", side_effect_level="external"), lambda _: good())
    outcome, bindings, _ = run(db, plan(step("s", "send")), [Action("tool_call", tool="send")], tools=tools, run_id="run-only")
    assert len(tools.effects.history("run-only")) == 1
    assert bindings.for_task("run-only") == {}
    assert outcome.steps[0].tool_result.evidence["step_binding_skipped"] == "no_task_scope"


@pytest.mark.parametrize("scope", ["foreign", "missing"])
def test_read_only_runner_cannot_claim_an_unrelated_effect_id(db, scope):
    tools = registry(db)
    eid = tools.effects.record("other-task", "lookup") if scope == "foreign" else "made-up"
    before = copy.deepcopy(tools.effects.history("other-task"))
    tools.register(ToolContract("lookup"), lambda _: good(effect_id=eid))
    outcome, bindings, _ = run(db, plan(step("s", "lookup")), [Action("tool_call", tool="lookup")], tools=tools, task_id="task", run_id="run")
    assert bindings.for_task("task") == {}
    assert tools.effects.history("other-task") == before
    assert outcome.steps[0].tool_result.evidence["step_binding_skipped"]


@pytest.mark.parametrize("existing", [False, True])
def test_cached_receipt_is_not_new_step_attribution(db, existing):
    tools = registry(db)
    calls = []
    tools.register(ToolContract("send", side_effect_level="external"), lambda _: calls.append(1) or good())
    first = tools.execute("send", {"x": 1}, task_id="task", run_id="old-run")
    eid = first.evidence["effect_id"]
    bindings = ActionBindings(db)
    if existing:
        bindings.bind(eid, "original", task_id="task", tool_id="send")
    before = bindings.for_task("task")
    outcome, _, _ = run(db, plan(step("new", "send")), [Action("tool_call", tool="send", args={"x": 1})], tools=tools,
                        bindings=bindings, task_id="task", run_id="new-run")
    result = outcome.steps[0].tool_result
    assert len(calls) == 1 and result.evidence["replayed"]
    assert bindings.for_task("task") == before
    assert result.evidence["step_binding_skipped"]
    assert "step_binding_error" not in result.evidence


def test_same_step_receipt_replay_preserves_its_original_binding(db):
    tools = registry(db)
    tools.register(ToolContract("send", side_effect_level="external"), lambda _: good())
    bindings = ActionBindings(db)
    _, _, _ = run(db, plan(step("s", "send")), [Action("tool_call", tool="send")], tools=tools, bindings=bindings, task_id="task", run_id="r1")
    before = bindings.for_task("task")
    outcome, _, _ = run(db, plan(step("s", "send")), [Action("tool_call", tool="send")], tools=tools, bindings=bindings, task_id="task", run_id="r2")
    assert before and bindings.for_task("task") == before
    assert "step_binding_error" not in outcome.steps[0].tool_result.evidence


def test_lease_loss_is_not_a_best_effort_binding_error(db):
    tools = registry(db)
    tools.register(ToolContract("send", side_effect_level="external"), lambda _: good())

    class FencedBindings(ActionBindings):
        def bind(self, *args, **kwargs):
            raise LeaseLost("binding lease lost")

    with pytest.raises(LeaseLost):
        run(db, plan(step("s", "send")), [Action("tool_call", tool="send")], tools=tools,
            bindings=FencedBindings(db), task_id="task", run_id="run")


def test_real_sequential_same_tool_effects_have_distinct_step_provenance(db):
    tools = registry(db)
    calls = []
    tools.register(ToolContract("send", side_effect_level="external"), lambda args: calls.append(args) or good())
    p = plan(step("a", "send"), step("b", "send", ["a"]))
    outcome, bindings, _ = run(db, p, [Action("tool_call", tool="send", args={"x": 1}),
        Action("tool_call", tool="send", args={"x": 2})], tools=tools, task_id="task", run_id="run")
    rows = tools.effects.history("task")
    assert outcome.finished and len(calls) == 2 and p.completed_work == ["a", "b"]
    assert {bindings.get(r["effect_id"])["step_id"] for r in rows} == {"a", "b"}
    assert reconcile_plan(p, rows, task_id="task", step_bindings=bindings.for_task("task")).ok


def test_unknown_effect_is_bound_but_not_confirmed_or_replayed(db):
    tools = registry(db)
    def uncertain(_):
        result = ToolResult.error_result("external outcome unknown", "timeout")
        result.side_effect_uncertain = True
        return result
    tools.register(ToolContract("send", side_effect_level="external"), uncertain)
    p = plan(step("s", "send"))
    outcome, bindings, _ = run(db, p, [Action("tool_call", tool="send")], tools=tools, task_id="task", run_id="run")
    rows = tools.effects.history("task")
    assert outcome.stop_reason == "side_effect_unknown" and p.completed_work == []
    assert len(rows) == 1 and rows[0]["status"] == "unknown"
    assert bindings.for_task("task") == {rows[0]["effect_id"]: "s"}


def test_binding_storage_failure_preserves_true_execution_outcome(db):
    tools = registry(db)
    tools.register(ToolContract("send", side_effect_level="external"), lambda _: good())
    class UnavailableBindings(ActionBindings):
        def bind(self, *args, **kwargs):
            raise OSError("binding storage unavailable")
    outcome, _, _ = run(db, plan(step("s", "send")), [Action("tool_call", tool="send")], tools=tools,
                        bindings=UnavailableBindings(db), task_id="task", run_id="run")
    result = outcome.steps[0].tool_result
    assert result.ok and result.executed and outcome.finished
    assert "OSError" in result.evidence["step_binding_error"]
    assert tools.effects.history("task")[0]["status"] == "confirmed"


def test_real_app_wires_bindings_and_persists_them_for_reconciliation(tmp_path):
    app = App(data_dir=str(tmp_path / "app"))
    try:
        app.permissions.high_risk_mode = "allow"
        app.tools.register(ToolContract("current_time", side_effect_level="external"), lambda _: good())
        app.controller.planner.make_plan = lambda *a, **k: plan(step("s", "current_time"))
        app.controller.router.route = lambda *a, **k: ScriptModel([Action("tool_call", tool="current_time")])
        task = app.submit("现在几点了", intent="time_query", required_tools=["current_time"], acceptance=[{"type": "tool_succeeded"}])
        assert task.task_status == TaskStatus.SUCCEEDED, task.failure_reason
        rows = app.controller.side_fx.history(task.task_id)
        exported = ActionBindings(app.db).for_task(task.task_id)
        assert exported == {row["effect_id"]: "s" for row in rows} and exported
        persisted = app.controller._plan_from_task(app.db.get_task(task.task_id))
        assert reconcile_plan(persisted, rows, task_id=task.task_id, step_bindings=exported).ok
    finally:
        app.db.close()
    reopened = Database(str(tmp_path / "app/evoagent.db"))
    try:
        assert ActionBindings(reopened).for_task(task.task_id) == exported
    finally:
        reopened.close()
