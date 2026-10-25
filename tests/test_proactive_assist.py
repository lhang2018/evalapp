"""ALN-25-A: 有来源的主动帮助——授权范围、近期整理与建议交付。

验收锚点：EA-UX-008（chat/extracted/157_qa.md）§6 F04 与 §6.4 验收条件；
EA-PRODUCT-007（chat/extracted/156_qa.md）UX-18/UX-19/UX-20；
出口证据引用 UX-F04-01/02 验收用例。

全部用例使用本地 App 夹具与显式时钟，不连真机、不连外部供应商；
记忆冲突经 fact_key 真实冲突路径产生，不伪造 has_conflict 字段。
"""
import json
import time

import pytest

from evoagent.app import App
from evoagent.core.task import Task, TaskStatus
from evoagent.goals.assist import AssistScopeError, ProactiveAssistService
from evoagent.memory.store import Credibility, MemoryItem, MemoryKind, Scope
from evoagent.memory.user_control import MemoryControlService


class Clock:
    """可变假时钟：测试过时/过期判定不依赖睡眠。"""

    def __init__(self, now=None):
        self.now = now if now is not None else time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def app(tmp_path):
    application = App(data_dir=str(tmp_path))
    yield application
    application.db.close()


@pytest.fixture
def control(app):
    return MemoryControlService(app.memory)


@pytest.fixture
def assist(app, control):
    return ProactiveAssistService(app.db, memory_control=control)


ASSIST_POLICY = "assist/v1"      # 高阈值：夹具可连续绑定多任务
TIGHT_POLICY = "assist-tight/v1"  # max_no_progress=1：一个无新检查的终态任务即触发治理裁决


def make_goal(app, *, title="organize recent work", policy=ASSIST_POLICY,
              criteria=("verified improvement",)):
    governor = app.goal_governor
    rules = {"max_attempts": 32, "max_no_progress": 8, "max_failures": 8,
             "max_repeated_states": 8, "idle_seconds": 100000}
    if policy == TIGHT_POLICY:
        rules["max_no_progress"] = 1
    if policy != "manual-review/v1":
        try:
            governor.policy(policy)
        except PermissionError:
            governor.install_policy(policy, rules, actor="admin", reason="assist test thresholds")
    deadline = time.time() + 3600
    goal = app.goals.create(title, actor="user", success_criteria=list(criteria), deadline=deadline)
    goal = app.goals.authorize(goal["goal_id"], {"tools": ["search"], "permissions": ["memory.read"],
                                                 "max_risk": "low"},
                               actor="admin", reason="explicit continuous grant", level="continuous",
                               expected_revision=goal["revision"], expires_at=deadline - 100,
                               no_progress_policy_ref=policy)
    return app.goals.transition(goal["goal_id"], "ready", actor="user", reason="start",
                                expected_revision=goal["revision"])


def make_task(app, goal, *, status=TaskStatus.SUCCEEDED, answer="done", reason="", updated_at=None):
    task = Task(user_goal="bounded attempt", goal_id=goal["goal_id"])
    task = app.goals.bind_task(task, "advance")
    task.task_status = status
    task.result, task.failure_reason = answer, reason
    task.updated_at = updated_at if updated_at is not None else time.time()
    task.result_evidence = [{"checks": [], "passed": status == TaskStatus.SUCCEEDED}]
    app.db.save_task(task)
    app.db.save_execution_budget(task.task_id, "test-run",
                                 {"usage": {"cost": 0, "model_calls": 1, "tool_calls": 0,
                                            "unknown_model_costs": 0}})
    return task


def make_memory(app, *, content, source="user", scope=Scope.USER, status=Credibility.VERIFIED,
                kind=MemoryKind.USER_PREFERENCE, fact_key="", evidence=None, **extra):
    item = MemoryItem(content=content, kind=kind, scope=scope, scope_key="proj-1",
                      status=status, source=source, fact_key=fact_key,
                      evidence=evidence or ({"note": "user statement"} if status == Credibility.VERIFIED else {}),
                      **extra)
    return app.memory.write(item)


def make_candidate(app, source_kind, source_id, *, benefit=100.0, confidence=0.9, cost=1.0,
                   opportunity_cost=0.0, risk="low", expires_in=3600, assessed=True):
    candidate = app.opportunities.discover(source_kind, source_id, actor="user",
                                           reason="spot improvement",
                                           expires_at=time.time() + expires_in)
    if not assessed:
        return candidate
    return app.opportunities.assess(candidate["candidate_id"], expected_benefit=benefit,
                                    evidence_confidence=confidence, estimated_cost=cost,
                                    opportunity_cost=opportunity_cost, risk=risk, actor="user",
                                    reason="assessed", expected_revision=candidate["revision"])


def all_refs(result):
    return {(entry["ref"]["kind"], entry["ref"]["id"]) for entry in result["evidence"]}


# ---- F04-01 / UX-F04-01：只使用授权来源，事实可追溯 ----

def test_organize_reads_only_authorized_sources(assist, app):
    inside = make_goal(app, title="inside scope")
    make_task(app, inside)
    outside = make_goal(app, title="outside scope")
    make_task(app, outside)
    result = assist.organize({"goal_ids": [inside["goal_id"]]}, actor="user", reason="weekly review")
    assert result["ok"] is True
    refs = all_refs(result)
    assert ("goal", inside["goal_id"]) in refs
    assert ("goal", outside["goal_id"]) not in refs
    assert all(kind != "task" or app.db.get_task(ident).goal_id == inside["goal_id"]
               for kind, ident in refs if kind == "task")
    assert result["scope"]["resolved_goal_ids"] == [inside["goal_id"]]
    assert result["scope"]["memory_scopes_allowed"] == []


def test_unknown_scope_ids_recorded_not_silently_dropped(assist):
    result = assist.organize({"goal_ids": ["ghost-goal-1"], "task_ids": ["ghost-task-1"]},
                             actor="user", reason="probe")
    assert result["ok"] is True
    assert result["scope"]["unresolved"] == [
        {"kind": "goal", "id": "ghost-goal-1", "reason": "not_found"},
        {"kind": "task", "id": "ghost-task-1", "reason": "not_found"}]
    assert result["evidence"] == []


def test_denied_memory_scope_contributes_no_facts(assist, app, control):
    control.set_scope_policy(allow=["user"], deny=["session"], actor="user", reason="session off-limits")
    denied = make_memory(app, content="session scratch note", scope=Scope.SESSION)
    allowed = make_memory(app, content="user stated preference", scope=Scope.USER)
    result = assist.organize({"memory_scopes": ["session", "user"]}, actor="user", reason="review")
    assert result["scope"]["memory_scopes_denied"] == [
        {"scope": "session", "reason": "denied_by_scope_policy"}]
    assert result["scope"]["memory_scopes_allowed"] == ["user"]
    refs = all_refs(result)
    assert ("memory", denied.memory_id) not in refs
    assert ("memory", allowed.memory_id) in refs
    assert not any(denied.content in entry["summary"] for entry in result["evidence"])


def test_memory_without_policy_service_fails_closed(app):
    service = ProactiveAssistService(app.db)  # 无 24-D 范围策略服务
    make_memory(app, content="user stated preference")
    result = service.organize({"memory_scopes": ["user"]}, actor="user", reason="review")
    assert result["scope"]["memory_scopes_denied"] == [
        {"scope": "user", "reason": "no_scope_policy_service"}]
    assert result["evidence"] == []


def test_every_evidence_ref_resolves(assist, app):
    goal = make_goal(app)
    make_task(app, goal)
    make_memory(app, content="user stated preference")
    result = assist.organize({"goal_ids": [goal["goal_id"]], "memory_scopes": ["user"]},
                             actor="user", reason="traceability")
    assert result["evidence"]
    for kind, ident in all_refs(result):
        if kind == "goal":
            assert app.goals.get(ident)["goal_id"] == ident
        elif kind == "task":
            assert app.db.get_task(ident) is not None
        elif kind == "memory":
            assert app.memory.get(ident) is not None
        else:
            raise AssertionError("unknown ref kind: " + kind)


# ---- UX-19 / F04 第二三步：事实、推断、未知区分 ----

def test_evidence_four_levels_classified(assist, app):
    # 一个目标族同时只允许一个未完成任务（ALN-14 治理），过时/失败分布在不同目标
    stale_goal = make_goal(app, title="stale work")
    make_task(app, stale_goal, status=TaskStatus.RUNNING, updated_at=time.time() - 7200)  # unconfirmed 过时
    failed_goal = make_goal(app, title="failed work", policy=TIGHT_POLICY)
    make_task(app, failed_goal, status=TaskStatus.FAILED, reason="provider timeout")  # recorded + 触发 inferred 治理裁决
    make_memory(app, content="user stated preference")  # user_stated
    result = assist.organize({"goal_ids": [stale_goal["goal_id"], failed_goal["goal_id"]],
                              "memory_scopes": ["user"]}, actor="user", reason="levels")
    levels = {entry["evidence"] for entry in result["evidence"]}
    assert levels == {"user_stated", "recorded", "inferred", "unconfirmed"}
    stale = [entry for entry in result["evidence"] if entry.get("unconfirmed_reason") == "stale_state"]
    assert stale and stale[0]["evidence"] == "unconfirmed"
    inferred = [entry for entry in result["evidence"] if entry["evidence"] == "inferred"]
    assert inferred and "治理裁决" in inferred[0]["summary"]


def test_stale_task_state_not_treated_as_current_fact(assist, app):
    stale_goal = make_goal(app, title="stale work")
    stale = make_task(app, stale_goal, status=TaskStatus.RUNNING, updated_at=time.time() - 7200)
    fresh_goal = make_goal(app, title="fresh work")
    fresh = make_task(app, fresh_goal, status=TaskStatus.RUNNING, updated_at=time.time() - 10)
    result = assist.organize({"goal_ids": [stale_goal["goal_id"], fresh_goal["goal_id"]]},
                             actor="user", reason="staleness")
    sections = result["sections"]
    assert any(entry["ref"]["id"] == stale.task_id for entry in sections["pending_confirmation"])
    assert any(entry["ref"]["id"] == fresh.task_id for entry in sections["in_progress"])
    assert not any(entry["ref"]["id"] == stale.task_id for entry in sections["in_progress"])


def test_conflicting_memory_marked_pending_confirmation(assist, app):
    first = make_memory(app, content="budget cap is 100", fact_key="budget-cap")
    second = make_memory(app, content="budget cap is 200", fact_key="budget-cap")
    # 冲突标记由 ConflictLedger.attach 持久化到行，回读库状态（返回对象不带标记）
    assert app.memory.get(first.memory_id).has_conflict
    assert app.memory.get(second.memory_id).has_conflict
    result = assist.organize({"memory_scopes": ["user"]}, actor="user", reason="conflicts")
    pending = {entry["ref"]["id"]: entry for entry in result["sections"]["pending_confirmation"]}
    assert set(pending) == {first.memory_id, second.memory_id}
    assert all(entry["unconfirmed_reason"] == "conflict" for entry in pending.values())
    assert all(entry["evidence"] == "unconfirmed" for entry in pending.values())


def test_superseded_memory_does_not_surface_as_current_fact(assist, app):
    """被取代记忆经 supersede 置 revoked，存储层 listing 即排除，不当作当前事实。"""
    old = make_memory(app, content="old constraint")
    newer = make_memory(app, content="new constraint")
    app.memory.supersede(old.memory_id, newer.memory_id, reason="corrected")
    result = assist.organize({"memory_scopes": ["user"]}, actor="user", reason="currency")
    refs = all_refs(result)
    assert ("memory", old.memory_id) not in refs
    assert ("memory", newer.memory_id) in refs
    assert not any("old constraint" in entry["summary"] for entry in result["evidence"])


def test_expired_memory_marked_pending_confirmation(assist, app):
    expired = make_memory(app, content="time boxed note", valid_until=time.time() - 60)
    result = assist.organize({"memory_scopes": ["user"]}, actor="user", reason="currency")
    entry = [e for e in result["sections"]["pending_confirmation"]
             if e["ref"]["id"] == expired.memory_id][0]
    assert entry["evidence"] == "unconfirmed"
    assert entry["unconfirmed_reason"] == "expired"


def test_candidate_memory_is_inferred_not_confirmed(assist, app):
    item = make_memory(app, content="runtime guessed preference", source="runtime",
                       status=Credibility.CANDIDATE)
    result = assist.organize({"memory_scopes": ["user"]}, actor="user", reason="candidate")
    entry = [e for e in result["evidence"] if e["ref"]["id"] == item.memory_id][0]
    assert entry["evidence"] == "inferred"
    assert entry["section"] == "pending_confirmation"


def test_sections_cover_expected_groups(assist, app):
    done_goal = make_goal(app, title="done work", criteria=("ship the report",))
    make_task(app, done_goal, status=TaskStatus.SUCCEEDED, answer="report shipped")
    failed_goal = make_goal(app, title="failed work")
    make_task(app, failed_goal, status=TaskStatus.FAILED, reason="evidence gap")
    stale_goal = make_goal(app, title="stale work")
    make_task(app, stale_goal, status=TaskStatus.RUNNING, updated_at=time.time() - 7200)
    fresh_goal = make_goal(app, title="fresh work")
    make_task(app, fresh_goal, status=TaskStatus.RUNNING, updated_at=time.time() - 5)
    blocked = make_goal(app, title="blocked goal")
    app.goals.transition(blocked["goal_id"], "blocked", actor="user", reason="missing authorization",
                         expected_revision=app.goals.get(blocked["goal_id"])["revision"])
    make_memory(app, content="user stated preference")
    result = assist.organize({"goal_ids": [done_goal["goal_id"], failed_goal["goal_id"],
                                           stale_goal["goal_id"], fresh_goal["goal_id"],
                                           blocked["goal_id"]],
                              "memory_scopes": ["user"]}, actor="user", reason="full sweep")
    counts = result["counts"]
    assert counts["completed"] >= 1
    assert counts["in_progress"] >= 1
    assert counts["blocked"] >= 1
    assert counts["unresolved_issues"] >= 1
    assert counts["confirmed_constraints"] >= 1
    assert counts["time_nodes"] >= 1
    assert counts["pending_confirmation"] >= 1
    assert sum(counts.values()) == len(result["evidence"])


# ---- F04-02 / UX-F04-02：没有高价值机会，不强行生成无关建议 ----

def test_suggest_without_high_value_opportunity_outputs_none(assist, app):
    goal = make_goal(app)
    make_candidate(app, "goal", goal["goal_id"], assessed=False)  # 未评估：needs_evidence
    result = assist.suggest({"goal_ids": [goal["goal_id"]]}, actor="user", reason="weekly")
    assert result["suggestions"] == []
    assert result["withheld"] == "no_high_value_opportunity"
    assert [item["reason"] for item in result["suppressed"]] == ["needs_evidence"]


def test_low_value_candidate_below_threshold_suppressed(assist, app):
    goal = make_goal(app)
    make_candidate(app, "goal", goal["goal_id"], benefit=1.0, confidence=0.1, cost=10.0)
    result = assist.suggest({"goal_ids": [goal["goal_id"]]}, actor="user", reason="weekly")
    assert result["suggestions"] == []
    assert result["withheld"] == "no_high_value_opportunity"
    assert result["suppressed"][0]["reason"] == "below_threshold"
    assert result["suppressed"][0]["score"] <= 0


def test_min_score_gate_is_configurable(assist, app):
    goal = make_goal(app)
    make_candidate(app, "goal", goal["goal_id"], benefit=10.0, confidence=0.5, cost=1.0)
    strict = assist.suggest({"goal_ids": [goal["goal_id"]]}, actor="user", reason="weekly",
                            min_score=1000.0)
    assert strict["suggestions"] == []
    assert strict["suppressed"][0]["reason"] == "below_threshold"
    loose = assist.suggest({"goal_ids": [goal["goal_id"]]}, actor="user", reason="weekly")
    assert len(loose["suggestions"]) == 1


def test_expired_candidate_suppressed(assist, app):
    clock = Clock()
    service = ProactiveAssistService(app.db, memory_control=assist.memory_control, clock=clock)
    goal = make_goal(app)
    make_candidate(app, "goal", goal["goal_id"], expires_in=60)
    clock.now = time.time() + 120
    result = service.suggest({"goal_ids": [goal["goal_id"]]}, actor="user", reason="weekly")
    assert result["suggestions"] == []
    assert result["suppressed"][0]["reason"] == "expired"


def test_out_of_scope_candidate_excluded(assist, app):
    inside = make_goal(app, title="inside")
    outside = make_goal(app, title="outside")
    make_candidate(app, "goal", outside["goal_id"])
    result = assist.suggest({"goal_ids": [inside["goal_id"]]}, actor="user", reason="weekly")
    assert result["suggestions"] == []
    assert result["excluded_out_of_scope"] == 1
    assert result["suppressed"] == []  # 范围外只计数，不展开候选细节


# ---- UX-20 / 卡验收第三条：四段式建议，含收益、权限与成本，不自动执行 ----

def test_high_value_suggestion_four_part_format(assist, app):
    goal = make_goal(app)
    make_candidate(app, "goal", goal["goal_id"], benefit=100.0, confidence=0.9, cost=2.0, risk="medium")
    result = assist.suggest({"goal_ids": [goal["goal_id"]]}, actor="user", reason="weekly")
    assert len(result["suggestions"]) == 1
    suggestion = result["suggestions"][0]
    assert suggestion["what"] and suggestion["why"] and suggestion["how"]
    requires = suggestion["requires"]
    assert requires["permissions"]["resolved"] is True
    assert requires["permissions"]["tools"] == ["search"]
    assert requires["permissions"]["permissions"] == ["memory.read"]
    assert requires["permissions"]["max_risk"] == "low"
    assert requires["estimated_cost_usd"] == 2.0
    assert requires["risk"] == "medium"
    assert requires["budget_check"]
    assert suggestion["score"] > 0
    assert suggestion["authorization"] == {
        "executed": False, "auto_executed": False,
        "note": "建议不等于用户授权执行建议；任何外部操作须另经权限、风险与预算检查"}
    refs = {(item["kind"], item["id"]) for item in suggestion["evidence_refs"]}
    assert ("opportunity", suggestion["candidate_id"]) in refs
    assert ("goal", goal["goal_id"]) in refs


def test_suggest_never_auto_executes(assist, app):
    goal = make_goal(app)
    candidate = make_candidate(app, "goal", goal["goal_id"])
    before_tasks = app.db._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    before_goals = app.db._conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
    result = assist.suggest({"goal_ids": [goal["goal_id"]]}, actor="user", reason="weekly")
    assert result["suggestions"]
    after_tasks = app.db._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    after_goals = app.db._conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
    assert (before_tasks, before_goals) == (after_tasks, after_goals)
    stored = app.opportunities._get(candidate["candidate_id"])
    assert stored["registered_goal_id"] is None  # 未代为登记
    assert app.db._conn.execute("SELECT COUNT(*) FROM goal_advances").fetchone()[0] == 0
    assert app.db._conn.execute("SELECT COUNT(*) FROM task_side_effects").fetchone()[0] == 0


def test_task_sourced_gain_candidate_flows_through_gate(assist, app):
    """UX-18 可复用出口：终态任务衍生的增益候选经同一筛选与四段式交付。"""
    goal = make_goal(app)
    task = make_task(app, goal, status=TaskStatus.FAILED, reason="evidence gap")
    candidate = make_candidate(app, "task", task.task_id)
    assert candidate["title"].startswith("Review failure:")
    result = assist.suggest({"task_ids": [task.task_id]}, actor="user", reason="after action review")
    assert len(result["suggestions"]) == 1
    suggestion = result["suggestions"][0]
    assert suggestion["source_ref"] == "task:" + task.task_id
    assert suggestion["requires"]["permissions"]["resolved"] is True


def test_suggestion_for_unauthorized_source_states_explicit_authorization_needed(assist, app):
    goal = make_goal(app)
    app.goals.transition(goal["goal_id"], "paused", actor="user", reason="pause",
                         expected_revision=app.goals.get(goal["goal_id"])["revision"])
    ref = app.goals.get(goal["goal_id"])["authorization_ref"]
    app.db._conn.execute("UPDATE goal_grants SET revoked_at=? WHERE authorization_ref=?",
                         (time.time(), ref))
    app.db._conn.commit()
    make_candidate(app, "goal", goal["goal_id"])
    result = assist.suggest({"goal_ids": [goal["goal_id"]]}, actor="user", reason="weekly")
    assert result["suggestions"]
    permissions = result["suggestions"][0]["requires"]["permissions"]
    assert permissions["resolved"] is False
    assert "显式授权" in permissions["note"]


# ---- 输入纪律与失败隔离 ----

def test_organize_requires_actor_and_reason(assist, app):
    goal = make_goal(app)
    with pytest.raises(ValueError):
        assist.organize({"goal_ids": [goal["goal_id"]]}, actor="", reason="weekly")
    with pytest.raises(ValueError):
        assist.organize({"goal_ids": [goal["goal_id"]]}, actor="user", reason="  ")


def test_suggest_requires_actor_and_reason(assist, app):
    goal = make_goal(app)
    with pytest.raises(ValueError):
        assist.suggest({"goal_ids": [goal["goal_id"]]}, actor="user", reason="")


@pytest.mark.parametrize("bad", [
    "not-a-dict", {"unknown_field": []}, {"goal_ids": "abc"}, {"goal_ids": [""]},
    {"task_ids": [123]}, {"memory_scopes": ["bogus-scope"]}, {"memory_scopes": "user"}])
def test_scope_validation_errors(assist, bad):
    with pytest.raises(AssistScopeError):
        assist.organize(bad, actor="user", reason="probe")


def test_failed_analysis_leaves_core_tasks_untouched(assist, app):
    """§6.4：主动分析失败不会影响核心任务。"""
    goal = make_goal(app)
    make_task(app, goal)
    before = app.goals.get(goal["goal_id"])
    with pytest.raises(AssistScopeError):
        assist.suggest({"goal_ids": [goal["goal_id"]], "bogus": 1}, actor="user", reason="weekly")
    after = app.goals.get(goal["goal_id"])
    assert (before["status"], before["revision"]) == (after["status"], after["revision"])
    assert app.db._conn.execute("SELECT COUNT(*) FROM tasks WHERE goal_id=?",
                                (goal["goal_id"],)).fetchone()[0] == 1
