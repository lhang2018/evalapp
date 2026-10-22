"""ALN-01: task scope, expert whitelist and system policy intersect at execution."""
from pathlib import Path
import threading
import urllib.request
import urllib.error
import json

import pytest

from evoagent.app import App
from evoagent.api.server import ApiServer
from evoagent.core.persistence import Database
from evoagent.core.permissions import PermissionController, PermissionDecision, ToolPolicy
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.tools.registry import ToolRegistry
from evoagent.workflows.models import WorkflowDef, WorkflowNode
from evoagent.workflows.engine import WorkflowEngine


def ok(data=None):
    return ToolResult(True, data=data, submitted=True, executed=True,
                      contract_valid=True, independently_verified=True, goal_reached=True)


@pytest.fixture
def gateway(tmp_path):
    db = Database(str(tmp_path / "authorization.db"))
    tools = ToolRegistry(PermissionController(high_risk_mode="allow"), db)
    yield tools
    db.close()


def grant(gateway, **overrides):
    policy = {"tools": ["action"], "permissions": []}
    policy.update(overrides)
    return gateway.authorizations.issue("task", policy, actor="test-admin")


@pytest.mark.parametrize("args,scope,allowed", [
    ({"target": "a"}, {"targets": ["a"]}, True),
    ({"target": "b"}, {"targets": ["a"]}, False),
    ({"payload": {"device_id": "b"}}, {"device_ids": ["a"]}, False),
    ({"device_id": "a"}, {"device_ids": ["a"]}, True),
    ({"target": "a"}, {"targets": []}, False),
    ({}, {"tools": []}, False),
    ({}, {"max_risk": "low"}, False),
])
def test_task_scope_blocks_runner_before_side_effects(gateway, args, scope, allowed):
    calls = []
    gateway.register(ToolContract("action", side_effect_level="external"), lambda a: calls.append(a) or ok())
    grant(gateway, **scope)
    result = gateway.execute("action", args, task_id="task", allowed_tools=["action"])
    assert result.ok is allowed
    assert len(calls) == int(allowed)
    if not allowed:
        assert not gateway.effects.history("task")


@pytest.mark.parametrize("location,allowed", [("inside/file", True), ("inside/../outside", False), ("inside-other/file", False)])
def test_paths_use_resolved_containment(gateway, tmp_path, location, allowed):
    calls = []
    gateway.register(ToolContract("action", resource_bindings={"path": "path"}), lambda a: calls.append(a) or ok())
    grant(gateway, path_roots=[str(tmp_path / "inside")])
    result = gateway.execute("action", {"path": str(tmp_path / location)}, task_id="task")
    assert result.ok is allowed and len(calls) == int(allowed)


def test_custom_resource_binding_and_defaults_are_checked(gateway, tmp_path):
    calls = []
    gateway.register(ToolContract("action", resource_bindings={"payload.destination": "path"},
        resource_defaults={"payload.destination": str(tmp_path / "outside")}), lambda a: calls.append(a) or ok())
    grant(gateway, path_roots=[str(tmp_path / "allowed")])
    assert not gateway.execute("action", {}, task_id="task").ok
    assert not calls


def test_permission_requirement_is_not_a_grant(gateway):
    gateway.register(ToolContract("action", permission_requirements=["device.admin"]), lambda a: ok())
    grant(gateway)
    assert not gateway.execute("action", {}, task_id="task").ok
    grant(gateway, permissions=["device.admin"])
    assert gateway.execute("action", {}, task_id="task").ok


def test_expert_whitelist_cannot_be_expanded_by_task_grant(gateway):
    calls = []
    gateway.register(ToolContract("action"), lambda a: calls.append(1) or ok())
    grant(gateway)
    assert not gateway.execute("action", {}, task_id="task", allowed_tools=[]).ok
    assert not calls


@pytest.mark.parametrize("change", ["revoke", "expire", "restrict", "deny", "version"])
def test_confirmation_cannot_outlive_scope_or_policy(gateway, change):
    calls = []
    contract = ToolContract("action", side_effect_level="external")
    gateway.register(contract, lambda a: calls.append(a) or ok())
    ref = grant(gateway)
    gateway.permissions.high_risk_mode = "confirm"
    def confirmation(op, args):
        if change == "revoke":
            gateway.authorizations.revoke(ref)
        elif change == "expire":
            gateway.store._conn.execute("UPDATE action_authorizations SET expires_at=0")
            gateway.store._conn.commit()
        elif change == "restrict":
            grant(gateway, tools=[])
        elif change == "deny":
            gateway.permissions.register_tool_policy(ToolPolicy("action", PermissionDecision.DENY))
        else:
            contract.tool_version = "2"
        return True
    gateway.permissions.confirm_callback = confirmation
    result = gateway.execute("action", {"target": "a"}, task_id="task")
    assert not result.ok and not calls


def test_denial_wins_over_explicit_allow_and_workflow_confirmation(gateway):
    gateway.permissions.high_risk_mode = "deny"
    gateway.permissions.register_tool_policy(ToolPolicy("action", PermissionDecision.ALLOW))
    calls = []
    gateway.register(ToolContract("action", side_effect_level="external"), lambda a: calls.append(1) or ok())
    wf = WorkflowDef("wf", nodes=[WorkflowNode("n", tool="action", requires_confirmation=True)])
    result = WorkflowEngine(gateway).run(wf, {}, confirm=lambda n: True)
    assert not result.ok and not calls


def test_confirmation_callback_cannot_change_the_frozen_action(gateway):
    calls = []
    gateway.permissions.high_risk_mode = "confirm"
    gateway.permissions.confirm_callback = lambda op, args: args.update(target="changed") or True
    gateway.register(ToolContract("action", side_effect_level="external"), lambda a: calls.append(a) or ok())
    grant(gateway, targets=["original"])
    source = {"target": "original"}
    assert gateway.execute("action", source, task_id="task").ok
    assert source == {"target": "original"} and calls == [source]


def test_revocation_survives_database_restart(gateway):
    ref = grant(gateway)
    gateway.authorizations.revoke(ref)
    db = Database(gateway.store.path)
    try:
        fresh = ToolRegistry(PermissionController(high_risk_mode="allow"), db)
        fresh.register(ToolContract("action"), lambda a: ok())
        assert not fresh.execute("action", {}, task_id="task").ok
    finally:
        db.close()


def test_real_app_freezes_authority_separately_from_required_fields(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        task = app.controller.submit_task("现在几点了", intent="time_query", required_tools=["not-a-grant"],
            authorization_scope={"tools": [], "permissions": []})
        assert task.authorization_ref
        row = app.tools.authorizations.current(task.task_id)
        assert row["policy"]["tools"] == []
        assert not task.result_evidence or not any(e.get("tool") == "current_time" for e in task.result_evidence)
    finally:
        app.db.close()


def test_authorization_api_is_registered_in_production_and_requires_admin(tmp_path):
    app = App(data_dir=str(tmp_path))
    app.config.api_token, app.config.api_admin_token = "reader", "admin"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from evoagent.core.task import Task
        app.db.save_task(Task(task_id="task"))
        url = f"http://127.0.0.1:{server.port}/tasks/task/authorization"
        data = json.dumps({"policy": {"tools": [], "permissions": []}}).encode()
        request = urllib.request.Request(url, data=data, headers={"Authorization": "Bearer reader"})
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request)
        assert denied.value.code == 403
        request = urllib.request.Request(url, data=data, headers={"Authorization": "Bearer admin"})
        with urllib.request.urlopen(request) as response:
            assert response.status == 201
            ref = json.load(response)["authorization_ref"]
        request = urllib.request.Request(f"http://127.0.0.1:{server.port}/authorizations/{ref}/revoke",
            data=b"{}", headers={"Authorization": "Bearer admin"})
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["revoked"]
    finally:
        server.shutdown()
        thread.join(3)
        app.db.close()
