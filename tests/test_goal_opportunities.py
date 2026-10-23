"""ALN-14: real runtime consumers, durable stopping and recursive boundaries."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import threading
import time
import urllib.request
import urllib.error

import pytest

from evoagent.app import App
from evoagent.core.persistence import Database
from evoagent.core.task import Task, TaskStatus
from evoagent.goals import GoalStore, GoalConflict
from evoagent.goals.governance import GoalGovernor, digest
from evoagent.goals.opportunities import OpportunityService


@pytest.fixture
def app(tmp_path):
    application = App(data_dir=str(tmp_path))
    yield application
    application.db.close()


def ready(app, *, title="answer a general question", parent=None, ref="bounded/v1", rules=None):
    governor = app.goal_governor
    if ref != "manual-review/v1":
        try:
            governor.policy(ref)
        except PermissionError:
            governor.install_policy(ref, rules or {}, actor="admin", reason="fixed thresholds")
        if rules is not None:
            governor.install_policy(ref, rules, actor="admin", reason="fixed thresholds")
    deadline = time.time() + 3600
    expiry = deadline - 100
    if parent:
        deadline = app.goals.get(parent)["deadline"]
        grant = app.db._conn.execute("SELECT data FROM goal_grants WHERE goal_id=? ORDER BY rowid DESC", (parent,)).fetchone()
        expiry = json.loads(grant[0])["expires_at"]
    goal = app.goals.create(title, actor="user", success_criteria=["verified improvement"],
        parent_goal_id=parent, goal_type="derived" if parent else "long_term", deadline=deadline)
    goal = app.goals.authorize(goal["goal_id"], {"tools": [], "permissions": [], "max_risk": "low"},
        actor="admin", reason="explicit continuous grant", level="continuous", expected_revision=goal["revision"],
        expires_at=expiry, no_progress_policy_ref=ref)
    return app.goals.transition(goal["goal_id"], "ready", actor="user", reason="start", expected_revision=goal["revision"])


def finish(app, goal, *, success=True, checks=None, answer="same result", unknown=False, reason=""):
    task = Task(user_goal="bounded attempt", goal_id=goal["goal_id"])
    task = app.goals.bind_task(task, "advance")
    task.task_status = TaskStatus.SUCCEEDED if success else TaskStatus.FAILED
    task.result, task.failure_reason = answer, reason
    task.updated_at = time.time()
    task.result_evidence = [{"checks": checks or [], "passed": success}]
    app.db.save_task(task)
    app.db.save_execution_budget(task.task_id, "test-run", {"usage": {"cost": 0, "model_calls": 1,
        "tool_calls": 0, "unknown_model_costs": int(unknown)}})
    return task


def objective(value="verified"):
    return {"type": "result_contains", "value": value}


def configure(app, goal, specs):
    return app.goal_governor.configure(goal["goal_id"], specs, actor="admin", reason="objective mapping",
                                      expected_revision=app.goals.get(goal["goal_id"])["revision"])


def test_rule_version_is_immutable_and_replay_does_not_change_authority(app):
    first = app.goal_governor.install_policy("rules/v1", {"max_attempts": 4}, actor="admin", reason="rules")
    assert app.goal_governor.install_policy("rules/v1", {"max_attempts": 4}, actor="other", reason="replay") == first
    with pytest.raises(GoalConflict):
        app.goal_governor.install_policy("rules/v1", {"max_attempts": 5}, actor="admin", reason="change")


@pytest.mark.parametrize("rules", [{"max_attempts": True}, {"max_no_progress": 0}, {"max_depth": 17},
    {"idle_seconds": math.nan}, {"risk_weight": math.inf}, {"unknown_rule": 1}, {"max_children": 1.5}])
def test_policy_invalid_inputs_are_rejected(app, rules):
    with pytest.raises(ValueError):
        app.goal_governor.install_policy("invalid/v1", rules, actor="admin", reason="test")


def test_no_progress_pauses_and_survives_restart_and_cannot_resume_by_transition(app):
    goal = ready(app, rules={"max_no_progress": 2})
    first, second = finish(app, goal), finish(app, goal)
    report = app.goal_governor.reconcile(goal["goal_id"])[0]
    assert report["reason"] == "no_improvement"
    assert report["task_refs"] == [first.task_id, second.task_id]
    paused = app.goals.get(goal["goal_id"])
    assert paused["status"] == "paused"
    count = len(app.goals.evidence(goal["goal_id"]))
    app.goal_governor.reconcile(goal["goal_id"])
    assert len(app.goals.evidence(goal["goal_id"])) == count
    other = Database(app.db.path)
    try:
        assert GoalGovernor(other).report(goal["goal_id"])["reason"] == "no_improvement"
        resumed = app.goals.transition(goal["goal_id"], "ready", actor="user", reason="try bypass",
                                       expected_revision=paused["revision"])
        with pytest.raises(PermissionError, match="no_improvement"):
            app.goals.bind_task(Task(user_goal="new", goal_id=resumed["goal_id"]), "advance")
    finally:
        other.close()


def test_repeated_failures_stop_before_next_task_is_created(app):
    goal = ready(app, rules={"max_failures": 2, "max_no_progress": 8})
    finish(app, goal, success=False)
    finish(app, goal, success=False)
    with pytest.raises(PermissionError, match="repeated_failure"):
        finish(app, goal)
    assert len(app.db.list_tasks_by_goal(goal["goal_id"])) == 2


def test_repeated_success_output_is_not_unbounded_progress(app):
    goal = ready(app, rules={"max_repeated_states": 2, "max_no_progress": 8})
    finish(app, goal)
    finish(app, goal)
    assert app.goal_governor.report(goal["goal_id"])["reason"] == "repeated_state"


def test_idle_threshold_stops_existing_task_without_waiting_for_scan(app):
    goal = ready(app, rules={"idle_seconds": 30})
    task = app.goals.bind_task(Task(user_goal="first", goal_id=goal["goal_id"]), "advance")
    now = time.time() + 31
    goals = GoalStore(app.db, clock=lambda: now)
    with pytest.raises(PermissionError, match="idle_timeout"):
        goals.check_task(task.task_id)
    GoalGovernor(app.db, clock=lambda: now).reconcile(goal["goal_id"])
    assert goals.get(goal["goal_id"])["status"] == "paused"


def test_only_new_registered_verified_criterion_counts_as_improvement(app):
    goal = ready(app)
    a, b = objective("a"), objective("b")
    configure(app, goal, [a, b])
    finish(app, goal, checks=[{"spec": a, "passed": True}])
    report = app.goal_governor.report(goal["goal_id"])
    assert report["no_progress"] == 0 and report["fulfilled"] == [digest(a)]
    finish(app, goal, checks=[{"spec": a, "passed": True}, {"spec": objective("invented"), "passed": True}])
    assert app.goal_governor.report(goal["goal_id"])["no_progress"] == 1
    finish(app, goal, checks=[{"spec": b, "passed": True, "uncertain": True}])
    assert app.goal_governor.report(goal["goal_id"])["no_progress"] == 2


def test_unknown_charges_cannot_count_as_improvement_or_be_cleared_by_review(app):
    goal = ready(app)
    criterion = objective()
    configure(app, goal, [criterion])
    finish(app, goal, checks=[{"spec": criterion, "passed": True}], unknown=True)
    report = app.goal_governor.reconcile(goal["goal_id"])[0]
    assert report["reason"] == "unknown_evidence" and report["fulfilled"] == []
    paused = app.goals.get(goal["goal_id"])
    with pytest.raises(GoalConflict):
        app.goal_governor.review(goal["goal_id"], actor="admin", reason="cannot erase debt",
                                 expected_revision=paused["revision"], evidence_refs=["review:test"])


def test_pending_physical_effect_is_unknown_even_if_task_says_success(app):
    from evoagent.core.side_effects import SideEffectLedger
    goal = ready(app)
    task = finish(app, goal)
    ledger = SideEffectLedger(app.db)
    ledger.begin(task.task_id, "external", "operation", "digest", run_id="run")
    assert app.goal_governor.report(goal["goal_id"])["reason"] == "unknown_evidence"


def test_configure_is_frozen_before_work_and_rejects_model_self_evaluation(app):
    goal = ready(app)
    with pytest.raises(ValueError):
        configure(app, goal, [{"type": "result_nonempty"}])
    finish(app, goal)
    with pytest.raises(GoalConflict):
        configure(app, goal, [objective()])


def test_child_depth_and_duplicate_ancestor_purpose_cannot_bypass(app):
    root = ready(app, title="root", rules={"max_depth": 1})
    child = ready(app, title="child", parent=root["goal_id"])
    with pytest.raises(GoalConflict, match="depth"):
        ready(app, title="grandchild", parent=child["goal_id"])
    with pytest.raises(GoalConflict, match="duplicate"):
        ready(app, title=" ＲＯＯＴ ", parent=root["goal_id"])


def test_child_count_includes_cancelled_and_total_descendant_limit(app):
    root = ready(app, title="root", rules={"max_children": 1})
    child = ready(app, title="child", parent=root["goal_id"])
    app.goals.transition(child["goal_id"], "cancelled", actor="user", reason="stop", expected_revision=child["revision"])
    with pytest.raises(GoalConflict, match="child count"):
        ready(app, title="different child", parent=root["goal_id"])
    other = ready(app, title="other root", ref="family/v1", rules={"max_descendants": 1})
    ready(app, title="one child", parent=other["goal_id"], ref="family/v1", rules={"max_descendants": 1})
    with pytest.raises(GoalConflict, match="total count"):
        ready(app, title="second child", parent=other["goal_id"], ref="family/v1", rules={"max_descendants": 1})


def test_parent_stop_blocks_child_task_and_new_descendant(app):
    root = ready(app, title="root", rules={"max_no_progress": 1})
    child = ready(app, title="child", parent=root["goal_id"], rules={"max_no_progress": 1})
    finish(app, child)
    app.goal_governor.reconcile(child["goal_id"])
    with pytest.raises(PermissionError):
        app.goals.bind_task(Task(user_goal="bypass", goal_id=child["goal_id"]), "execute")
    with pytest.raises(PermissionError):
        ready(app, title="bypass new child", parent=root["goal_id"], rules={"max_no_progress": 1})


def test_cross_connection_advance_claim_has_one_winner_and_no_reset(app):
    goal = ready(app)
    other = Database(app.db.path)
    stores = [app.goals, GoalStore(other)]
    barrier = threading.Barrier(2)
    def claim(i):
        barrier.wait()
        try:
            stores[i].bind_task(Task(user_goal="one", goal_id=goal["goal_id"]), "advance")
            return "claimed"
        except GoalConflict:
            return "busy"
    try:
        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(claim, [0, 1])) == ["busy", "claimed"]
        assert len(other.list_tasks_by_goal(goal["goal_id"])) == 1
    finally:
        other.close()


def test_real_runtime_manual_policy_runs_once_then_pauses(app):
    goal = ready(app, ref="manual-review/v1")
    result = app.goal_governor.advance(goal["goal_id"], command_id="step", max_steps=20)
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["task_status"] == "SUCCEEDED"
    assert result["goal"]["status"] == "paused"
    task_id = result["tasks"][0]["task_id"]
    assert app.db.get_execution_budget(task_id)["usage"]["model_calls"] > 0
    rows = app.db._conn.execute("SELECT data FROM budget_reservations").fetchall()
    assert rows and all(json.loads(row[0])["state"] == "settled" for row in rows)
    replay = app.goal_governor.advance(goal["goal_id"], command_id="step", max_steps=20)
    assert replay == result
    assert len(app.db.list_tasks_by_goal(goal["goal_id"])) == 1
    with pytest.raises(GoalConflict):
        app.goal_governor.advance(goal["goal_id"], command_id="step", max_steps=1)


def test_real_runtime_objective_satisfaction_goes_to_review_not_completed(app):
    goal = ready(app)
    configure(app, goal, [objective("已按通用流程处理")])
    result = app.goal_governor.advance(goal["goal_id"], command_id="objective", max_steps=4)
    assert len(result["tasks"]) == 1
    assert result["goal"]["status"] == "review"
    assert result["goal"]["governance_stop"]["reason"] == "objectives_satisfied"


def test_real_budget_exhaustion_stops_repeated_advancement(app):
    app.config.budget.runtime_daily_model_calls = 0
    goal = ready(app)
    result = app.goal_governor.advance(goal["goal_id"], command_id="budget", max_steps=10)
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["task_status"] == "FAILED"
    assert result["goal"]["governance_stop"]["reason"] == "budget_exhausted"
    assert app.db._conn.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 0


def test_human_review_retains_history_and_does_not_refill_budget(app):
    goal = ready(app, ref="manual-review/v1")
    first = app.goal_governor.advance(goal["goal_id"], command_id="first")
    paused = first["goal"]
    app.goal_governor.review(goal["goal_id"], actor="admin", reason="reviewed actual result",
        expected_revision=paused["revision"], evidence_refs=["review:first"])
    app.goal_governor.advance(goal["goal_id"], command_id="second")
    assert len(app.db.list_tasks_by_goal(goal["goal_id"])) == 2
    assert app.db._conn.execute("SELECT COUNT(*) FROM goal_observations").fetchone()[0] == 2
    from evoagent.core.runtime_budget import identity
    balance = app.ledger.reservation_store().balance(identity("goal", goal["goal_id"]))
    assert balance["spent"]["model_calls"] >= 2


def test_discovery_is_idempotent_and_promotion_never_authorizes(app):
    goal = ready(app)
    service = app.opportunities
    candidate = service.discover("goal", goal["goal_id"], actor="system", reason="goal opportunity", expires_at=time.time()+600)
    assert service.discover("goal", goal["goal_id"], actor="other", reason="retry", expires_at=time.time()+500) == candidate
    registered = service.register(candidate["candidate_id"], actor="admin")
    assert registered["status"] == "proposed" and registered["authorization_ref"] is None
    with pytest.raises(PermissionError):
        app.controller.submit_task("try", goal_id=registered["goal_id"], origin="opportunity")
    assert len(app.db.list_tasks_by_goal(registered["goal_id"])) == 0


def assessed(service, candidate, **kwargs):
    values = dict(expected_benefit=10, evidence_confidence=1, estimated_cost=1,
                  opportunity_cost=0, risk="low", actor="admin", reason="host estimate",
                  expected_revision=candidate["revision"])
    values.update(kwargs)
    return service.assess(candidate["candidate_id"], **values)


def test_ranking_uses_cost_risk_freshness_and_stable_ties(app):
    clock = [time.time()]
    service = OpportunityService(app.db, clock=lambda: clock[0])
    candidates = []
    for name in ["a", "b", "c", "d"]:
        goal = ready(app, title=name)
        candidates.append(service.discover("goal", goal["goal_id"], actor="system", reason="source", expires_at=clock[0]+100))
    assessed(service, candidates[0])
    assessed(service, candidates[1], risk="high", opportunity_cost=1)
    assessed(service, candidates[2], estimated_cost=None)
    assessed(service, candidates[3])
    ranked = service.ranked()
    assert [c["candidate_id"] for c in ranked[:2]] == sorted([candidates[0]["candidate_id"], candidates[3]["candidate_id"]])
    assert ranked[0]["score"] > ranked[2]["score"]
    assert ranked[-1]["score"] is None
    clock[0] += 50
    assert service.ranked()[0]["score"] < ranked[0]["score"]
    clock[0] += 51
    assert all(c["ranking_state"] == "expired" for c in service.ranked())


def test_unknown_task_cost_or_missing_source_cannot_create_positive_rank(app):
    goal = ready(app)
    task = finish(app, goal, unknown=True)
    candidate = app.opportunities.discover("task", task.task_id, actor="system", reason="observed", expires_at=time.time()+300)
    assessed(app.opportunities, candidate)
    assert app.opportunities.ranked()[0]["score"] is None
    with pytest.raises((ValueError, KeyError)):
        app.opportunities.discover("task", "missing", actor="system", reason="fake", expires_at=time.time()+300)


def test_advance_limits_are_strict_and_do_not_create_tasks(app):
    goal = ready(app)
    for steps in [0, True, 33, 1.5]:
        with pytest.raises(ValueError):
            app.goal_governor.advance(goal["goal_id"], command_id="bad", max_steps=steps)
    assert app.db.list_tasks_by_goal(goal["goal_id"]) == []


def test_pending_advance_request_recovers_references_without_sending_again(app, monkeypatch):
    goal = ready(app, ref="manual-review/v1")
    original = app.controller.submit_task
    def crash_after_task(*args, **kwargs):
        task = original(*args, **kwargs)
        raise RuntimeError("process stopped after task was committed")
    monkeypatch.setattr(app.controller, "submit_task", crash_after_task)
    with pytest.raises(RuntimeError):
        app.goal_governor.advance(goal["goal_id"], command_id="crash")
    other = Database(app.db.path)
    try:
        replay = GoalGovernor(other, controller=app.controller).advance(goal["goal_id"], command_id="crash")
        assert replay["pending_request"] and len(replay["tasks"]) == 1
        assert len(other.list_tasks_by_goal(goal["goal_id"])) == 1
    finally:
        other.close()


def test_parent_authorization_expiry_also_blocks_new_child_registration(app):
    root = ready(app, title="root")
    now = time.time() + 3550
    store = GoalStore(app.db, clock=lambda: now)
    with pytest.raises(PermissionError, match="授权失效"):
        store.create("child", actor="system", success_criteria=["proof"], parent_goal_id=root["goal_id"])


def test_cross_connection_last_child_slot_has_one_winner(app):
    root = ready(app, title="root", rules={"max_children": 1})
    other = Database(app.db.path)
    stores = [app.goals, GoalStore(other)]
    barrier = threading.Barrier(2)
    def claim(index):
        barrier.wait()
        try:
            stores[index].create("child "+str(index), actor="system", goal_type="derived",
                parent_goal_id=root["goal_id"], success_criteria=["proof"])
            return "created"
        except GoalConflict:
            return "full"
    try:
        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(claim, [0, 1])) == ["created", "full"]
    finally:
        other.close()


def test_live_revocation_denies_actual_advancement_without_new_task(app):
    goal = ready(app)
    app.goals.revoke(goal["goal_id"], actor="user", reason="stop", expected_revision=goal["revision"])
    assert app.goal_governor.advance(goal["goal_id"], command_id="revoked")["tasks"] == []
    assert app.db.list_tasks_by_goal(goal["goal_id"]) == []


def test_held_approval_task_cannot_be_replaced_with_another_advance(app):
    goal = ready(app)
    task = app.goals.bind_task(Task(user_goal="pending", goal_id=goal["goal_id"]), "advance")
    task.task_status = TaskStatus.WAITING_USER
    app.db.save_task(task)
    with pytest.raises(GoalConflict, match="unfinished"):
        app.goal_governor.advance(goal["goal_id"], command_id="replacement")
    assert len(app.db.list_tasks_by_goal(goal["goal_id"])) == 1


def test_governance_observations_are_frozen_and_replay_does_not_improve_them(app):
    goal = ready(app)
    criterion = objective()
    configure(app, goal, [criterion])
    task = finish(app, goal)
    app.goal_governor.reconcile(goal["goal_id"])
    task.result_evidence = [{"checks": [{"spec": criterion, "passed": True}]}]
    app.db.save_task(task)
    assert app.goal_governor.report(goal["goal_id"])["fulfilled"] == []
    app.goal_governor.reconcile(goal["goal_id"])
    assert app.db._conn.execute("SELECT COUNT(*) FROM goal_observations").fetchone()[0] == 1


def test_unknown_charges_need_actual_reconciliation_before_human_review(app):
    goal = ready(app)
    task = app.goals.bind_task(Task(user_goal="request", goal_id=goal["goal_id"]), "advance")
    task.current_run_id = "test-run"
    task.task_status = TaskStatus.RUNNING
    app.db.save_task(task)
    budget = app.controller._make_budget(task)
    permit = budget.runtime_binding.begin(budget, "model", "provider", 10000, phase="react")
    permit.dispatch()
    permit.unknown("provider:timeout")
    budget.mark_unknown_model_cost("provider")
    task.task_status = TaskStatus.FAILED
    app.db.save_task(task)
    app.goal_governor.reconcile(goal["goal_id"])
    paused = app.goals.get(goal["goal_id"])
    with pytest.raises(GoalConflict):
        app.goal_governor.review(goal["goal_id"], actor="admin", reason="not yet reconciled",
            expected_revision=paused["revision"], evidence_refs=["receipt:fake"])
    permit.store.reconcile(permit.id, {"cost_micros": 2000, "model_calls": 1, "tool_calls": 0},
        evidence_ref="actual:invoice", actor="billing", reason="verified invoice")
    resumed = app.goal_governor.review(goal["goal_id"], actor="admin", reason="reconciled and reviewed",
        expected_revision=paused["revision"], evidence_refs=["actual:invoice"])
    assert resumed["status"] == "ready"
    from evoagent.core.runtime_budget import identity
    assert app.ledger.reservation_store().balance(identity("goal", goal["goal_id"]))["spent"]["cost_micros"] == 2000
    assert app.goal_governor.report(goal["goal_id"])["fulfilled"] == []


def test_governance_and_opportunities_api_require_transport_admin(app):
    from evoagent.api.server import ApiServer
    app.config.api_admin_token, app.config.api_token = "admin-token", "operator-token"
    server = ApiServer(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.httpd.server_address[1]
    def request(method, path, body=None, token="admin-token"):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer "+token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)
    try:
        assert request("POST", "/goal-policies", {}, token="operator-token")[0] == 403
        assert request("POST", "/goal-policies", {"ref": "api/v1", "rules": {}, "reason": "rules"})[0] == 201
        goal = ready(app, ref="manual-review/v1")
        path = "/goals/"+goal["goal_id"]
        assert request("POST", path+"/advance", {"command_id": "api", "actor": "spoofed"})[0] == 400
        assert request("POST", path+"/advance", {"command_id": "api"}, token="operator-token")[0] == 403
        code, result = request("POST", path+"/advance", {"command_id": "api"})
        assert code == 200 and result["goal"]["status"] == "paused"
        assert request("GET", path+"/progress", token="operator-token")[1]["attempts"] == 1
        code, candidate = request("POST", "/opportunities/discover", {"source_kind":"goal", "source_id":goal["goal_id"],
            "reason":"source", "expires_at":time.time()+600})
        assert code == 201
        assert request("GET", "/opportunities", token="operator-token")[1]["opportunities"]
        assert request("POST", "/opportunities/"+candidate["candidate_id"]+"/register", {}, token="operator-token")[0] == 403
    finally:
        server.shutdown()


def test_opportunity_expected_utility_is_linked_to_actual_objectives_and_receipts(app):
    source = ready(app)
    candidate = app.opportunities.discover("goal", source["goal_id"], actor="system", reason="source", expires_at=time.time()+500)
    assessed(app.opportunities, candidate)
    goal = app.opportunities.register(candidate["candidate_id"], actor="admin")
    goal = app.goals.authorize(goal["goal_id"], {"tools": [], "permissions": []}, level="continuous", actor="admin",
        reason="explicit new Goal grant", expires_at=time.time()+300, expected_revision=goal["revision"])
    goal = app.goals.transition(goal["goal_id"], "ready", actor="admin", reason="ready", expected_revision=goal["revision"])
    configure(app, goal, [objective("已按通用流程处理")])
    app.goal_governor.advance(goal["goal_id"], command_id="outcome")
    outcome = app.opportunities.outcome(candidate["candidate_id"])
    assert outcome["expected"]["estimated_cost"] == 1
    assert len(outcome["observed"]["task_refs"]) == 1
    assert len(outcome["observed"]["fulfilled_objectives"]) == 1
    assert outcome["observed"]["settled_cost_usd"] == 0 and outcome["observed"]["cost_complete"]
    assert outcome["production_benefit_verified"] is False


def test_unregistered_policy_fails_closed_and_finalizes_without_masking_task_failure(app):
    goal = ready(app)
    goal = app.goals.authorize(goal["goal_id"], {"tools": [], "permissions": []}, level="continuous", actor="admin",
        reason="missing policy reference", expires_at=time.time()+300, expected_revision=goal["revision"],
        no_progress_policy_ref="missing/v1")
    with pytest.raises(PermissionError, match="unregistered"):
        app.goals.bind_task(Task(user_goal="blocked", goal_id=goal["goal_id"]), "advance")
    report = app.goal_governor.reconcile(goal["goal_id"])[0]
    assert report["reason"] == "unregistered_policy"
    assert app.goals.get(goal["goal_id"])["status"] == "paused"


def test_objective_registration_cannot_retroactively_count_child_evidence(app):
    root = ready(app, title="root")
    child = ready(app, title="child", parent=root["goal_id"])
    finish(app, child, checks=[{"spec": objective(), "passed": True}])
    with pytest.raises(GoalConflict, match="before the first task"):
        configure(app, root, [objective()])


def test_registered_expectation_is_not_rewritten_after_actual_outcome(app):
    source = ready(app)
    candidate = app.opportunities.discover("goal", source["goal_id"], actor="system", reason="source", expires_at=time.time()+300)
    original = assessed(app.opportunities, candidate, expected_benefit=10)
    app.opportunities.register(candidate["candidate_id"], actor="admin")
    assessed(app.opportunities, original, expected_benefit=100)
    assert app.opportunities.outcome(candidate["candidate_id"])["expected"]["expected_benefit"] == 10


def test_missing_consumption_receipts_are_not_reported_as_zero_complete_cost(app):
    source = ready(app)
    candidate = app.opportunities.discover("goal", source["goal_id"], actor="system", reason="source", expires_at=time.time()+500)
    goal = app.opportunities.register(candidate["candidate_id"], actor="admin")
    goal = app.goals.authorize(goal["goal_id"], {"tools": [], "permissions": []}, level="continuous", actor="admin",
        reason="grant", expires_at=time.time()+300, expected_revision=goal["revision"])
    goal = app.goals.transition(goal["goal_id"], "ready", actor="admin", reason="ready", expected_revision=goal["revision"])
    finish(app, goal)
    assert not app.opportunities.outcome(candidate["candidate_id"])["observed"]["cost_complete"]
