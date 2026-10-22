"""ALN-13: persistent purpose, authority intersection and restart boundaries."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from evoagent.app import App
from evoagent.api.server import ApiServer
from evoagent.core.contracts import GOAL_FIELDS, GOAL_STATUS_SPEC
from evoagent.core.persistence import Database
from evoagent.core.permissions import PermissionController
from evoagent.core.task import Task
from evoagent.goals import GoalConflict, GoalStore
from evoagent.goals.store import LEVELS, TRANSITIONS
from evoagent.evolution.sandbox import SandboxRunner
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.tools.registry import ToolRegistry


POLICY = {"tools": ["read", "write"], "permissions": [], "max_risk": "critical"}


@pytest.fixture
def store(tmp_path):
    db = Database(str(tmp_path / "goals.db"))
    clock = [time.time()]
    store = GoalStore(db, clock=lambda: clock[0])
    store.test_clock = clock
    yield store
    db.close()


def create(store, **kwargs):
    return store.create("purpose", actor="user", success_criteria=["verified outcome"],
                        deadline=store.clock() + 3600, **kwargs)


def authorize(store, goal, *, level="execution", policy=None, budget=None, expires_at=None):
    return store.authorize(goal["goal_id"], policy or POLICY, level=level,
        actor="admin", reason="explicit grant", expected_revision=goal["revision"],
        expires_at=expires_at or store.clock() + 1800, budget_policy=budget)


def change(store, goal, status, **kwargs):
    return store.transition(goal["goal_id"], status, actor="user", reason="test decision",
                             expected_revision=goal["revision"], **kwargs)


def ready(store, **kwargs):
    goal = authorize(store, create(store, **kwargs))
    return change(store, goal, "ready")


def task_for(store, goal, action="execute", **kwargs):
    task = Task(user_goal="one attempt", goal_id=goal["goal_id"])
    return store.bind_task(task, action, **kwargs)


def gateway(store, confirm=None):
    permissions = PermissionController(high_risk_mode="confirm" if confirm else "allow",
                                       confirm_callback=confirm)
    tools = ToolRegistry(permissions, store=store.db)
    calls = []
    for name, side in [("read", "none"), ("write", "local")]:
        tools.register(ToolContract(name, side_effect_level=side),
            lambda args: calls.append(args) or ToolResult(True, data="ok", submitted=True,
                executed=True, contract_valid=True, independently_verified=True, goal_reached=True))
    return tools, calls


def test_goal_ten_states_and_durable_separate_task_identity(store):
    goal = ready(store)
    assert set(GOAL_FIELDS) <= set(goal)
    assert set(TRANSITIONS) == set(GOAL_STATUS_SPEC)
    for status in ["running", "waiting", "paused", "blocked", "ready", "running", "review"]:
        goal = change(store, goal, status)
    with pytest.raises(ValueError, match="证据"):
        change(store, goal, "completed")
    goal = change(store, goal, "completed", evidence_refs=["task:accepted-result"])
    assert goal["status"] == "completed"
    assert len(store.evidence(goal["goal_id"])) == goal["revision"]
    assert store.db.list_tasks_by_goal(goal["goal_id"]) == []


@pytest.mark.parametrize("terminal", ["cancelled", "expired", "completed"])
def test_terminal_goals_cannot_be_resurrected(store, terminal):
    goal = ready(store)
    if terminal == "expired":
        store.test_clock[0] += 4000
        goal = store.get(goal["goal_id"])
    elif terminal == "completed":
        goal = change(store, change(store, goal, "running"), "review")
        goal = change(store, goal, "completed", evidence_refs=["evidence:1"])
    else:
        goal = change(store, goal, terminal)
    assert goal["status"] == terminal
    with pytest.raises(GoalConflict):
        change(store, goal, "ready")
    with pytest.raises(PermissionError):
        authorize(store, goal)


def test_create_and_transition_commands_replay_once_and_reject_changed_payload(store):
    first = create(store, command_id="create-01")
    assert create(store, command_id="create-01") == first
    with pytest.raises(GoalConflict):
        store.create("different", actor="user", success_criteria=["ok"], command_id="create-01")
    goal = authorize(store, first)
    moved = change(store, goal, "ready", command_id="move-001")
    assert change(store, goal, "ready", command_id="move-001") == moved
    with pytest.raises(GoalConflict):
        change(store, goal, "paused", command_id="move-001")


def test_custom_goal_ids_do_not_collide_with_create_command_namespace(store):
    goal = create(store, goal_id="create", command_id="shared-001")
    goal = authorize(store, goal)
    assert change(store, goal, "ready", command_id="shared-001")["status"] == "ready"


def test_cross_connection_state_cas_has_one_winner(store):
    goal = ready(store)
    other = Database(store.db.path)
    peers = [store, GoalStore(other)]
    barrier = threading.Barrier(2)
    def attempt(index):
        barrier.wait()
        try:
            change(peers[index], goal, "paused")
            return "ok"
        except GoalConflict:
            return "stale"
    try:
        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(attempt, [0, 1])) == ["ok", "stale"]
    finally:
        other.close()


def test_registered_opportunity_does_not_implicitly_authorize_a_task(store):
    goal = create(store, goal_type="opportunity")
    with pytest.raises(PermissionError):
        change(store, goal, "ready")
    with pytest.raises(PermissionError):
        task_for(store, goal)
    assert not store.db.list_tasks_by_goal(goal["goal_id"])


@pytest.mark.parametrize("level,action,allowed", [
    ("suggestion", "prepare", False), ("preparation", "prepare", True),
    ("preparation", "execute", False), ("sandbox", "execute", False),
    ("execution", "execute", True), ("execution", "advance", False),
    ("continuous", "advance", True), ("continuous", "sandbox", False),
])
def test_autonomy_action_matrix(store, level, action, allowed):
    goal = change(store, authorize(store, create(store), level=level), "ready")
    if allowed:
        assert task_for(store, goal, action).goal_id == goal["goal_id"]
    else:
        with pytest.raises(PermissionError):
            task_for(store, goal, action)


def test_preparation_has_no_host_side_effects_even_with_broad_tool_grant(store):
    goal = change(store, authorize(store, create(store), level="preparation"), "ready")
    task = task_for(store, goal, "prepare")
    tools, calls = gateway(store)
    assert tools.execute("read", {}, task_id=task.task_id).ok
    assert not tools.execute("write", {}, task_id=task.task_id).ok
    assert len(calls) == 1


@pytest.mark.parametrize("dimension", ["tools", "permissions", "targets", "device_ids", "path_roots", "max_risk", "level", "budget", "expiry"])
def test_child_grants_cannot_expand_parent_authority(store, tmp_path, dimension):
    policy = {**POLICY, "targets": ["known"], "device_ids": ["device1"],
              "path_roots": [str(tmp_path / "inside")], "max_risk": "high"}
    parent = change(store, authorize(store, create(store), policy=policy), "ready")
    child = create(store, goal_type="derived", parent_goal_id=parent["goal_id"])
    child_policy = dict(policy)
    kwargs = {"policy": child_policy}
    if dimension == "level": kwargs["level"] = "continuous"
    elif dimension == "budget": kwargs["budget"] = {"max_cost": 2}
    elif dimension == "expiry": kwargs["expires_at"] = store.clock() + 1900
    elif dimension == "max_risk": child_policy[dimension] = "critical"
    elif dimension == "path_roots": child_policy[dimension] = [str(tmp_path / "outside")]
    else: child_policy[dimension] = ["not-authorized"]
    with pytest.raises(PermissionError):
        authorize(store, child, **kwargs)
    assert store.get(child["goal_id"])["authorization_ref"] is None


@pytest.mark.parametrize("operation", ["paused", "cancelled", "revoke", "deadline", "rotate"])
def test_parent_stop_and_authority_changes_block_child_actions_after_restart(store, operation):
    parent = ready(store)
    child = ready(store, parent_goal_id=parent["goal_id"], goal_type="derived")
    task = task_for(store, child)
    tools, calls = gateway(store)
    assert tools.execute("read", {}, task_id=task.task_id).ok
    if operation == "revoke":
        store.revoke(parent["goal_id"], actor="user", reason="stop", expected_revision=parent["revision"])
    elif operation == "deadline":
        row = store.db._conn.execute("SELECT * FROM goal_grants WHERE goal_id=?", (parent["goal_id"],)).fetchone()
        grant = json.loads(row["data"])
        grant["expires_at"] = time.time() - 1
        store.db._conn.execute("UPDATE goal_grants SET data=? WHERE authorization_ref=?",
                              (json.dumps(grant), row["authorization_ref"]))
        store.db._conn.commit()
    elif operation == "rotate": authorize(store, parent)
    else: change(store, parent, operation)
    restarted = Database(store.db.path)
    try:
        fresh_tools = ToolRegistry(PermissionController(high_risk_mode="allow"), store=restarted)
        fresh_tools.register(ToolContract("read"), lambda args: calls.append("bad") or ToolResult(True))
        assert not fresh_tools.execute("read", {}, task_id=task.task_id).ok
        assert len(calls) == 1
    finally:
        restarted.close()


def test_deadline_expires_durably_and_no_duplicate_expiry_event(store):
    goal = ready(store)
    store.test_clock[0] += 4000
    first = store.get(goal["goal_id"])
    assert first["status"] == "expired"
    assert store.get(goal["goal_id"])["revision"] == first["revision"]
    assert store.evidence(goal["goal_id"])[-1]["event_type"] == "goal_expired"


def test_grant_expiry_blocks_goal_before_its_deadline(store):
    goal = ready(store)
    store.test_clock[0] += 1900
    current = store.get(goal["goal_id"])
    assert current["status"] == "blocked"
    assert store.get(goal["goal_id"])["revision"] == current["revision"]
    assert store.evidence(goal["goal_id"])[-1]["event_type"] == "goal_authorization_expired"


def test_derived_goal_requires_a_registered_parent(store):
    with pytest.raises(ValueError, match="父目标"):
        create(store, goal_type="derived")
    with pytest.raises(ValueError, match="parent_goal_id"):
        create(store, parent_goal_id=["bad"])


def test_scope_is_rechecked_after_interactive_confirmation(store):
    goal = ready(store)
    task = task_for(store, goal)
    def confirm(*_):
        current = store.get(goal["goal_id"])
        store.revoke(goal["goal_id"], actor="user", reason="stop now", expected_revision=current["revision"])
        return True
    tools, calls = gateway(store, confirm=confirm)
    result = tools.execute("write", {}, task_id=task.task_id)
    assert not result.ok
    assert not calls


def test_task_policy_and_goal_scope_both_apply(store):
    goal = ready(store)
    task = task_for(store, goal, request_policy={"tools": [], "permissions": []})
    tools, calls = gateway(store)
    tools.authorizations.issue(task.task_id, POLICY, actor="admin")
    assert not tools.execute("read", {}, task_id=task.task_id).ok
    assert not calls


def test_goal_task_deduplication_survives_database_restart(store):
    goal = ready(store)
    first = task_for(store, goal, command_id="task-001")
    db = Database(store.db.path)
    try:
        second = task_for(GoalStore(db), goal, command_id="task-001")
        assert second.task_id == first.task_id
        assert len(db.list_tasks_by_goal(goal["goal_id"])) == 1
        with pytest.raises(GoalConflict):
            task_for(GoalStore(db), goal, "prepare", command_id="task-001")
    finally: db.close()


def test_forged_manifest_does_not_authorize_legacy_goal_task(store):
    goal = ready(store)
    task = Task(goal_id=goal["goal_id"], runtime_manifest={"goal": {"authorization_ref": goal["authorization_ref"]}})
    store.db.save_task(task)
    with pytest.raises(PermissionError): store.check_task(task.task_id)


@pytest.mark.parametrize("origin", ["scheduled", "derived", "opportunity"])
def test_directly_persisted_autonomous_tasks_cannot_bypass_goal_binding(store, origin):
    task = Task(origin=origin)
    store.db.save_task(task)
    tools, calls = gateway(store)
    assert not tools.execute("read", {}, task_id=task.task_id).ok
    assert not calls


def test_sandbox_level_uses_real_isolated_runner(store):
    goal = change(store, authorize(store, create(store), level="sandbox"), "ready")
    assert store.sandbox(goal["goal_id"], "print('sandbox-ok')", SandboxRunner(), actor="admin")["status"] == "passed"
    result = store.sandbox(goal["goal_id"], "import socket; socket.create_connection(('127.0.0.1', 9))", SandboxRunner(), actor="admin")
    assert result["status"] == "violation"
    assert store.evidence(goal["goal_id"])[-1]["event_type"] == "goal_sandbox_finished"
    assert store.get(goal["goal_id"])["evidence_refs"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "later"])
def test_invalid_deadlines_are_rejected_without_persistence(store, bad):
    with pytest.raises(ValueError):
        store.create("bad", actor="admin", success_criteria=["ok"], deadline=bad)
    assert store.list() == []


@pytest.fixture
def app(tmp_path):
    app = App(data_dir=str(tmp_path / "app"))
    yield app
    for component in (app.db, app.bus, app.memory):
        close = getattr(component, "close", None)
        if close: close()


def test_runtime_preserves_goal_reference_and_rechecks_model_budget_authority(app):
    goal = ready(app.goals)
    task = task_for(app.goals, goal)
    budget = app.controller._make_budget(task)
    budget.limits.max_cost = 1000
    budget.check_model_call()
    assert budget.limits.max_cost == 1
    current = app.goals.get(goal["goal_id"])
    app.goals.revoke(goal["goal_id"], actor="user", reason="stop", expected_revision=current["revision"])
    with pytest.raises(PermissionError): budget.check_model_call()
    with pytest.raises(PermissionError): app.controller._build_retry(task)


def test_real_runtime_executes_registered_goal_task_and_records_manifest(app):
    goal = create(app.goals)
    goal = authorize(app.goals, goal, policy={"tools": ["calculate"], "permissions": []})
    goal = change(app.goals, goal, "ready")
    task = app.submit("计算 3+5", goal_id=goal["goal_id"], intent="computation")
    assert task.task_status.value == "SUCCEEDED"
    saved = app.get_task(task.task_id)
    assert saved.goal_id == goal["goal_id"]
    assert saved.runtime_manifest["goal"]["authorization_refs"][goal["goal_id"]] == goal["authorization_ref"]
    assert app.goals.get(goal["goal_id"])["status"] == "running"
    assert app.db.list_tasks_by_goal(goal["goal_id"])[0].task_id == task.task_id


def test_retry_does_not_widen_original_or_later_narrowed_task_policy(store):
    goal = ready(store)
    first = task_for(store, goal, request_policy={"tools": ["read"], "permissions": []})
    tools, calls = gateway(store)
    tools.authorizations.issue(first.task_id, {"tools": [], "permissions": []}, actor="admin")
    retry = Task(user_goal="retry", goal_id=goal["goal_id"])
    store.bind_retry(first.task_id, retry)
    assert not tools.execute("read", {}, task_id=retry.task_id).ok
    assert not tools.execute("write", {}, task_id=retry.task_id).ok
    assert not calls


def test_created_goal_task_can_resume_without_relaxing_persisted_task_scope(app):
    goal = ready(app.goals)
    task = task_for(app.goals, goal, request_policy={"tools": [], "permissions": []})
    restored = app.controller.resume_task(task.task_id)
    assert restored.task_id == task.task_id
    assert app.get_task(task.task_id).goal_id == goal["goal_id"]
    row = app.db._conn.execute("SELECT task_policy FROM goal_tasks WHERE task_id=?", (task.task_id,)).fetchone()
    assert json.loads(row[0])["tools"] == []


def test_invalid_task_policy_has_no_orphan_task_or_binding(store):
    goal = ready(store)
    with pytest.raises(ValueError):
        task_for(store, goal, command_id="invalid-command", request_policy={"magic": "allow"})
    assert store.db.list_tasks_by_goal(goal["goal_id"]) == []


def test_api_admin_goal_flow_and_operator_denial(app):
    app.config.api_admin_token, app.config.api_token = "admin-token", "op-token"
    server = ApiServer(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.httpd.server_address[1]
    def request(method, path, body=None, token="admin-token"):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
            data=json.dumps(body).encode() if body is not None else None, method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)
    try:
        payload = dict(title="api purpose", success_criteria=["accepted"], deadline=time.time()+3600)
        assert request("POST", "/goals", payload, token="op-token")[0] == 403
        status, goal = request("POST", "/goals", payload)
        assert status == 201
        gid = goal["goal_id"]
        status, goal = request("POST", f"/goals/{gid}/authorization", dict(policy={"tools":["calculate"],"permissions":[]},
            level="execution", reason="allow", expires_at=time.time()+1800, expected_revision=goal["revision"]))
        assert status == 201
        status, goal = request("POST", f"/goals/{gid}/transition", dict(target="ready", reason="ready", expected_revision=goal["revision"]))
        assert status == 200
        assert request("GET", "/goals", token="op-token")[0] == 200
        assert request("POST", "/tasks", {"goal":"x", "goal_id":"missing-goal"})[0] == 404
        assert request("POST", "/tasks", {"goal":"x", "origin":"opportunity"})[0] == 403
        assert request("POST", f"/goals/{gid}/transition", dict(target="paused", actor="spoofed", reason="x", expected_revision=goal["revision"]))[0] == 400
        assert request("GET", f"/goals/{gid}/evidence")[1]["evidence"]
        status, first = request("POST", f"/goals/{gid}/tasks",
            dict(goal="计算 3+5", command_id="api-task-001"))
        assert status == 202
        status, second = request("POST", f"/goals/{gid}/tasks",
            dict(goal="计算 3+5", command_id="api-task-001"))
        assert status == 202 and first["task_id"] == second["task_id"]
        deadline = time.time() + 5
        while not app.get_task(first["task_id"]).is_terminal() and time.time() < deadline:
            time.sleep(0.01)
        assert app.get_task(first["task_id"]).is_terminal()
        assert len(request("GET", f"/goals/{gid}/tasks")[1]["tasks"]) == 1
        assert request("POST", f"/goals/{gid}/tasks",
            dict(goal="other", command_id="api-task-001"))[0] == 409
    finally:
        server.shutdown()
