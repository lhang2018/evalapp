"""Execution-boundary regressions from the design alignment audit.

All runners are inert and all persistent state is isolated in temp paths.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from evoagent.app import App
from evoagent.core.budget import BudgetController, BudgetLimits
from evoagent.core.permissions import PermissionController, PermissionDecision
from evoagent.core.persistence import Database
from evoagent.core.side_effects import SideEffectLedger
from evoagent.core.task import Task, TaskStatus
from evoagent.models.base import Action, ModelProfile
from evoagent.react.engine import ReactEngine
from evoagent.release.manager import ReleaseManager, ReleaseManifest
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.tools.registry import ToolRegistry
from evoagent.workflows.engine import WorkflowEngine
from evoagent.workflows.models import WorkflowDef, WorkflowNode, WorkflowNodeState


def ok(data="done"):
    return ToolResult(ok=True, data=data, executed=True, submitted=True,
                      contract_valid=True, goal_reached=True,
                      independently_verified=True)


class FixedModel:
    def __init__(self, action, flag=None):
        self.action, self.flag, self.calls = action, flag, 0

    def profile(self):
        return ModelProfile(model_id="boundary-test")

    def estimate_cost(self, *_args):
        return 0.0

    def suggest_action(self, *_args):
        self.calls += 1
        if self.flag:
            self.flag.cancelled = True
        return self.action if self.calls == 1 else Action(kind="finish", answer="done")


@pytest.fixture
def gateway(tmp_path):
    db = Database(str(tmp_path / "main.db"))
    registry = ToolRegistry(PermissionController(high_risk_mode="allow"), db)
    yield registry
    db.close()


def test_empty_allowlist_rejects_model_tool(gateway):
    calls = []
    gateway.register(ToolContract(tool_id="hidden"), lambda _: calls.append(1) or ok())
    react = ReactEngine(FixedModel(Action(kind="tool_call", tool="hidden")), gateway)
    outcome = react.run([], BudgetController(BudgetLimits()), allowed_tools=[])
    assert calls == []
    assert outcome.steps[0].tool_result.error_class == "auth_or_permission"


def test_substitute_cannot_escape_allowlist(gateway):
    calls = []
    gateway.register(ToolContract(tool_id="primary", retry_policy=0, substitutes=["hidden"]),
                     lambda _: (_ for _ in ()).throw(ConnectionError("offline")))
    gateway.register(ToolContract(tool_id="hidden"), lambda _: calls.append(1) or ok())
    result = gateway.execute("primary", {}, allowed_tools=["primary"])
    assert not result.ok
    assert calls == []


def test_workflow_confirmation_is_high_risk():
    permission = PermissionController(high_risk_mode="deny")
    assert permission.check_op("workflow_node_confirm", {}) == PermissionDecision.DENY


def test_external_side_effect_requires_confirmation():
    calls = []
    registry = ToolRegistry(PermissionController(high_risk_mode="confirm"))
    registry.register(ToolContract(tool_id="arbitrary_external", side_effect_level="external"),
                      lambda _: calls.append(1) or ok())
    assert not registry.execute("arbitrary_external", {}).ok
    assert calls == []


def test_cancel_while_model_running_never_executes_tool(gateway):
    calls, flag = [], SimpleNamespace(cancelled=False)
    gateway.register(ToolContract(tool_id="t"), lambda _: calls.append(1) or ok())
    react = ReactEngine(FixedModel(Action(kind="tool_call", tool="t"), flag), gateway)
    result = react.run([], BudgetController(BudgetLimits()), cancel_flag=flag)
    assert result.stop_reason == "cancel"
    assert calls == []


def test_ledger_is_submitted_before_runner(gateway):
    observed = []
    def runner(_):
        observed.extend(gateway.effects.pending("task-prewrite"))
        return ok()
    gateway.register(ToolContract(tool_id="write", side_effect_level="external"), runner)
    result = gateway.execute("write", {}, task_id="task-prewrite", run_id="run-one")
    assert result.ok
    assert len(observed) == 1 and observed[0]["status"] == "submitted"
    assert observed[0]["run_id"] == "run-one"
    assert gateway.effects.history("task-prewrite")[0]["status"] == "confirmed"


def test_unknown_timeout_is_durable_and_not_retried(gateway):
    calls = []
    def runner(_):
        calls.append(1)
        raise TimeoutError("unknown")
    gateway.register(ToolContract(tool_id="send", side_effect_level="external",
                                  idempotency_policy="key", retry_policy=4), runner)
    budget = BudgetController(BudgetLimits())
    first = gateway.execute("send", {}, task_id="unknown", budget=budget)
    second = gateway.execute("send", {}, task_id="unknown")
    assert first.side_effect_uncertain and second.side_effect_uncertain
    assert first.evidence["side_effect_level"] == "external"
    assert len(calls) == 1
    assert len(gateway.effects.pending("unknown")) == 1
    assert gateway.effects.pending("unknown")[0]["status"] == "unknown"
    assert budget.usage.tool_calls == 1


def test_unknown_cannot_be_bypassed_with_new_key(gateway):
    calls = []
    gateway.effects.record("blocked", "other", status="unknown")
    gateway.register(ToolContract(tool_id="write", side_effect_level="external"),
                     lambda _: calls.append(1) or ok())
    result = gateway.execute("write", {}, task_id="blocked", idempotency_key="brand-new")
    assert result.side_effect_uncertain
    assert calls == []


def test_submitted_cannot_be_bypassed_with_another_operation(gateway):
    calls = []
    gateway.effects.begin("crashed", "old", "old-key", "args")
    gateway.register(ToolContract(tool_id="write", side_effect_level="external"),
                     lambda _: calls.append(1) or ok())
    result = gateway.execute("write", {}, task_id="crashed", idempotency_key="new-key")
    assert result.side_effect_uncertain and calls == []


def test_invalid_risk_declaration_is_rejected(gateway):
    with pytest.raises(ValueError):
        gateway.register(ToolContract(tool_id="invalid", risk_class="invalid"), lambda _: ok())


def test_ledger_write_failure_blocks_runner(gateway, monkeypatch):
    calls = []
    gateway.register(ToolContract(tool_id="write", side_effect_level="external"),
                     lambda _: calls.append(1) or ok())
    def fail(*_args, **_kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(gateway.effects, "begin", fail)
    result = gateway.execute("write", {}, task_id="disk")
    assert result.error_class == "side_effect_ledger"
    assert calls == []


def test_confirmed_operation_replays_result_without_duplicate_ledger(gateway):
    calls = []
    gateway.register(ToolContract(tool_id="write", side_effect_level="external"),
                     lambda _: calls.append(1) or ok({"receipt": "r1"}))
    first = gateway.execute("write", {}, task_id="confirmed")
    second = gateway.execute("write", {}, task_id="confirmed")
    assert first.data == second.data == {"receipt": "r1"}
    assert second.evidence["replayed"]
    assert calls == [1]
    effect = gateway.effects.record_from_tool_result("confirmed", "write", first)
    assert effect == first.evidence["effect_id"]
    assert len(gateway.effects.history("confirmed")) == 1


def test_submitted_operation_survives_new_connection(tmp_path):
    path = str(tmp_path / "restart.db")
    db = Database(path)
    effect, _ = SideEffectLedger(db).begin("crash", "write", "key", "args")
    db.close()
    db2 = Database(path)
    try:
        ledger = SideEffectLedger(db2)
        other, claimed = ledger.begin("crash", "write", "key", "args")
        assert other == effect and not claimed
        assert ledger.get(effect)["status"] == "submitted"
    finally:
        db2.close()


def test_concurrent_operation_has_only_one_runner(gateway):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def runner(_):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return ok()
    gateway.register(ToolContract(tool_id="write", side_effect_level="external"), runner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(gateway.execute, "write", {}, task_id="race")
        try:
            assert entered.wait(3)
            other_db = Database(gateway.store.path)
            try:
                other_registry = ToolRegistry(gateway.permissions, other_db)
                other_registry.register(gateway.contract("write"), runner)
                second = other_registry.execute("write", {}, task_id="race")
                assert second.side_effect_uncertain
            finally:
                other_db.close()
        finally:
            release.set()
        assert first.result().ok
    assert calls == [1]


def test_dag_unknown_does_not_retry_or_compensate(gateway):
    calls, compensated = [], []
    def runner(_):
        calls.append(1)
        return ToolResult(ok=False, side_effect_uncertain=True, error="unknown")
    gateway.register(ToolContract(tool_id="send", side_effect_level="external"), runner)
    gateway.register(ToolContract(tool_id="compensate"), lambda _: compensated.append(1) or ok())
    wf = WorkflowDef(workflow_id="wf", created_from_task="dag", nodes=[
        WorkflowNode(node_id="n", tool="send", max_retries=5)],
        compensation_actions={"n": "compensate"})
    result = WorkflowEngine(gateway).run(wf, {})
    assert result.status == WorkflowNodeState.WAITING_EXTERNAL
    assert calls == [1] and compensated == []
    assert gateway.effects.pending("dag")[0]["status"] == "unknown"


def test_react_stops_on_unknown_without_second_model_call(gateway):
    gateway.register(ToolContract(tool_id="send", side_effect_level="external"),
                     lambda _: ToolResult(ok=False, side_effect_uncertain=True))
    model = FixedModel(Action(kind="tool_call", tool="send"))
    result = ReactEngine(model, gateway).run([], BudgetController(BudgetLimits()), run_id="react")
    assert result.stop_reason == "side_effect_unknown" and model.calls == 1


def test_gateway_result_write_failure_remains_pending(gateway, monkeypatch):
    calls = []
    gateway.register(ToolContract(tool_id="write", side_effect_level="external"),
                     lambda _: calls.append(1) or ok())
    def fail(*_args):
        raise OSError("disk full")
    monkeypatch.setattr(gateway.effects, "finish", fail)
    result = gateway.execute("write", {}, task_id="finish-error")
    assert result.side_effect_uncertain and not result.ok
    assert gateway.effects.pending("finish-error")[0]["status"] == "submitted"
    assert gateway.execute("write", {}, task_id="finish-error").side_effect_uncertain
    assert calls == [1]


def test_same_arguments_in_distinct_tasks_are_distinct_operations(gateway):
    calls = []
    gateway.register(ToolContract(tool_id="write", side_effect_level="external",
                                  idempotency_policy="key"),
                     lambda _: calls.append(1) or ok())
    assert gateway.execute("write", {}, task_id="one").ok
    assert gateway.execute("write", {}, task_id="two").ok
    assert calls == [1, 1]


def test_changed_arguments_cannot_reuse_operation_key(gateway):
    calls = []
    gateway.register(ToolContract(tool_id="write", side_effect_level="external"),
                     lambda _: calls.append(1) or ok())
    assert gateway.execute("write", {"target": "one"}, task_id="same", idempotency_key="k").ok
    result = gateway.execute("write", {"target": "two"}, task_id="same", idempotency_key="k")
    assert not result.ok and calls == [1]


def test_uncertainty_overrides_success_claim(gateway):
    def contradictory(_):
        result = ok()
        result.side_effect_uncertain = True
        return result
    gateway.register(ToolContract(tool_id="write", side_effect_level="external"), contradictory)
    result = gateway.execute("write", {}, task_id="contradiction")
    assert result.side_effect_uncertain
    assert gateway.effects.pending("contradiction")[0]["status"] == "unknown"


def test_runtime_unknown_workflow_can_only_continue_after_reconciliation(tmp_path):
    app = App(data_dir=str(tmp_path), confirm_callback=lambda *_: True)
    calls = []
    try:
        def runner(_):
            calls.append(1)
            return ToolResult(ok=False, side_effect_uncertain=True)
        app.tools.register(ToolContract(tool_id="send", side_effect_level="external"), runner)
        wf = WorkflowDef(workflow_id="unknown-flow", nodes=[WorkflowNode(node_id="n", tool="send")])
        app._workflows[wf.workflow_id] = wf
        task = app.controller.execute(Task(user_goal="send", workflow_ref=wf.workflow_id), intent_hint="general")
        assert task.task_status == TaskStatus.SUSPENDED
        assert len(app.controller.cp_mgr.latest(task.task_id).pending_side_effects) == 1
        with pytest.raises(PermissionError):
            app.controller.resume_task(task.task_id)
        forced = app.controller.resume_task(task.task_id, force=True)
        assert forced.task_status == TaskStatus.SUSPENDED
        assert calls == [1]
        effect = app.controller.side_fx.pending(task.task_id)[0]
        assert app.controller.side_fx.reconcile(effect["effect_id"], "failed", receipt_ref="checked-not-sent")
        app.tools.register(ToolContract(tool_id="send", side_effect_level="external"), lambda _: ok())
        assert app.controller.resume_task(task.task_id).task_status == TaskStatus.SUCCEEDED
    finally:
        app.db.close()


def test_runtime_waits_for_workflow_authorization_and_preserves_definition(tmp_path):
    app = App(data_dir=str(tmp_path))
    calls = []
    try:
        app.tools.register(ToolContract(tool_id="read"), lambda _: calls.append(1) or ok())
        wf = WorkflowDef(workflow_id="needs-auth", nodes=[
            WorkflowNode(node_id="n", tool="read", requires_confirmation=True)])
        app._workflows[wf.workflow_id] = wf
        task = Task(user_goal="read", workflow_ref=wf.workflow_id)
        task = app.controller.execute(task, intent_hint="general")
        assert task.task_status == TaskStatus.SUSPENDED
        assert calls == [] and wf.nodes[0].state == WorkflowNodeState.PENDING
        assert app.approvals.list_for_task(task.task_id)[0]["status"] == "denied"
        app.permissions.confirm_callback = lambda *_: True
        resumed = app.controller.resume_task(task.task_id)
        assert resumed.task_status == TaskStatus.SUCCEEDED
        assert calls == [1] and wf.nodes[0].state == WorkflowNodeState.PENDING
    finally:
        app.db.close()


def test_failed_task_with_unknown_cannot_create_retry(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        task = Task(task_id="failed-unknown", task_status=TaskStatus.FAILED)
        app.db.save_task(task)
        app.controller.side_fx.record(task.task_id, "send", status="unknown")
        with pytest.raises(PermissionError):
            app.controller.resume_task(task.task_id, force=True)
    finally:
        app.db.close()


def test_release_body_and_file_must_match(tmp_path):
    manager = ReleaseManager(artifact_dir=str(tmp_path))
    manifest = manager.build_manifest(version="v1")
    path = tmp_path / f"{manifest.release_id}.json"
    original = path.read_text(encoding="utf-8")
    assert manager.verify(manifest.release_id)["ok"]
    payload = json.loads(original)
    payload["min_app_version"] = "999.0.0"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert not manager.verify(manifest.release_id)["ok"]
    path.write_text(original, encoding="utf-8")
    manifest.version = "tampered-in-memory"
    assert not manager.verify(manifest.release_id)["ok"]
    manifest.version = "v1"
    path.unlink()
    assert not manager.verify(manifest.release_id)["ok"]


@pytest.mark.parametrize("field,value", [
    ("permission_requirements", None), ("data_schema", None),
    ("model_capabilities", None), ("offline_support", None),
    ("target_platforms", []), ("required_tool_contracts", None)])
def test_incomplete_stable_compatibility_rejected(field, value):
    manager = ReleaseManager()
    manifest = manager.build_manifest(**{field: value})
    result = manager.deploy(manifest.release_id, gate_passed=True)
    assert not result["ok"] and manager.current_release_id is None


def test_tampered_rollback_target_is_rejected():
    manager = ReleaseManager()
    first = manager.build_manifest(version="v1")
    assert manager.deploy(first.release_id, gate_passed=True)["ok"]
    second = manager.build_manifest(version="v2")
    assert manager.deploy(second.release_id, gate_passed=True)["ok"]
    first.version = "tampered"
    assert not manager.rollback("test")["ok"]
    assert manager.current_release_id == second.release_id


def test_artifact_type_change_is_not_hidden_by_python_equality(tmp_path):
    manager = ReleaseManager(artifact_dir=str(tmp_path))
    manifest = manager.build_manifest(offline_support=False)
    path = tmp_path / f"{manifest.release_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["offline_support"] = 0  # Python dict equality considers 0 == False.
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert not manager.verify(manifest.release_id)["ok"]


def test_legacy_signature_is_readable_but_cannot_deploy():
    manifest = ReleaseManifest.from_dict({"release_id": "legacy", "artifact_digest": "old"})
    manager = ReleaseManager()
    manager.manifests[manifest.release_id] = manifest
    assert not manager.verify(manifest.release_id)["ok"]
    assert not manager.deploy(manifest.release_id, gate_passed=True)["ok"]
