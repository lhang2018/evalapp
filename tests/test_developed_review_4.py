"""Boundary probes for proactive planning, team contracts and compositions."""

import math
import sqlite3
import pytest
from evoagent.app import App
from evoagent.core.task import Task, TaskStatus
from evoagent.goals.proactivity import ProactivityGovernor, ProactivityError
from evoagent.goals.gap_planner import CapabilityGapPlanner
from evoagent.experts.team import (
    TeamContract,
    TeamRole,
    TeamPlanner,
    Handoff,
    TeamContractError,
    TeamFormationError,
)
from evoagent.experts.builtin import builtin_experts
from evoagent.experts.registry import ExpertRegistry, Expert, CapabilityEntry
from evoagent.assets import AssetDraft
from evoagent.assets.repository import AssetRepository
from evoagent.assets.composition import (
    compose,
    dependency_closure,
    ComposeError,
    AssetIO,
)
from test_asset_composition import _promote


def test_off_accepts_late_attempt_and_status_without_crashing():
    g = ProactivityGovernor(level="off")
    g.record_attempt(had_benefit=False)
    assert g.status()["level"] == "off" and not g.may_start().allowed


def test_user_level_change_releases_no_progress_stop():
    g = ProactivityGovernor(level="low")
    g.record_attempt(had_benefit=False)
    g.record_attempt(had_benefit=False)
    g.set_level("high")
    assert g.may_start().allowed and g.status()["no_progress_streak"] == 0


@pytest.mark.parametrize("count", [-1, float("nan"), True])
def test_invalid_concurrency_cannot_be_allowed(count):
    with pytest.raises(ProactivityError):
        ProactivityGovernor().may_start(concurrent_proactive=count)


def test_nonboolean_benefit_cannot_clear_streak():
    with pytest.raises(ProactivityError):
        ProactivityGovernor().record_attempt(had_benefit="false")


def test_unknown_resource_decision_is_not_admission():
    class Unknown:
        def admit(self, *a):
            return type(
                "Decision", (), {"decision": "unknown", "reason": "unavailable"}
            )()

    assert not ProactivityGovernor(resource_governor=Unknown()).may_start().allowed


def role(r="a", artifact="state", actions=()):
    return TeamRole(
        r,
        r,
        "d",
        ["cap"],
        outputs=[r + ":out"],
        state_artifacts=[artifact] if artifact else [],
        external_actions=list(actions),
    )


def contract(roles):
    return TeamContract(
        contract_id="c", task_intent="i", domain="d", task_ref="task:one", roles=roles
    )


def test_invalid_contract_cannot_advance():
    c = contract([role("a"), role("b")])
    assert not c.advance(role_id="a")["ok"] and c.status == "proposed"


def test_writer_map_drift_cannot_authorize_observer():
    c = contract([role("a"), role("b", artifact="")])
    c.state_writers["state"] = "b"
    assert not c.advance(role_id="b")["ok"]


def test_other_role_failure_prevents_global_completion():
    c = contract([role("a", "one"), role("b", "two")])
    assert c.advance(role_id="a")["ok"]
    c.fail("b", "failed")
    assert not c.advance(role_id="a")["ok"] and c.status == "active"


def test_forged_handoff_cannot_be_authorized():
    c = contract([role("a", "one", ("send",)), role("b", "two")])
    c.status = "active"
    c.handoffs.append(Handoff("h", "a", "b", ["not-an-output"]))
    assert not c.authorize_external_action(role_id="a", action="send")["ok"]


@pytest.mark.parametrize("budget", [math.nan, math.inf, True])
def test_handoff_rejects_nonfinite_or_boolean_budget(budget):
    c = contract([role("a", "one"), role("b", "two")])
    with pytest.raises(TeamContractError):
        TeamPlanner(builtin_experts()).add_handoff(
            c, from_role="a", to_role="b", input_refs=["a:out"], budget_units=budget
        )
    assert not c.handoffs


def test_handoff_cannot_request_foreign_task_context():
    c = contract([role("a", "one"), role("b", "two")])
    with pytest.raises(TeamContractError):
        TeamPlanner(builtin_experts()).add_handoff(
            c,
            from_role="a",
            to_role="b",
            input_refs=["a:out"],
            context_scope=["task:private"],
        )


def test_truthy_confirmation_is_not_explicit_confirmation():
    c = contract([role("a", actions=("send",))])
    c.status = "active"
    c.fail("a", "timeout")
    assert not c.authorize_external_action(
        role_id="a", action="send", confirmed="false"
    )["ok"]


def test_external_role_list_cannot_expand_contract_permission():
    actions = ["read"]
    r = TeamRole(
        "a", "a", "d", ["cap"], state_artifacts=["state"], external_actions=actions
    )
    c = contract([r])
    c.status = "active"
    actions.append("send")
    assert not c.authorize_external_action(role_id="a", action="send")["ok"]


def test_disabled_capability_cannot_form_team():
    reg = ExpertRegistry()
    reg.register_expert(Expert(expert_id="x", capabilities=["cap"]))
    reg.register_capability(
        CapabilityEntry("cap", "1", "d", "disabled", status="disabled")
    )
    with pytest.raises(TeamFormationError):
        TeamPlanner(reg).form(intent="i", domain="d", required_capabilities=["cap"])


@pytest.fixture
def app(tmp_path):
    a = App(data_dir=str(tmp_path))
    yield a
    a.memory.close()
    a.db.close()


def add_failure(app, goal_id=None):
    t = Task(
        user_goal="probe",
        intent="absent_intent",
        domain="absent_domain",
        task_status=TaskStatus.FAILED,
        failure_reason="missing capability",
        goal_id=goal_id,
    )
    app.db.save_task(t)
    return t


def planner(app, **kwargs):
    return CapabilityGapPlanner(
        app.db, experts=app.experts, evolution=app.evolution, goals=app.goals, **kwargs
    )


def test_one_failure_plus_its_goal_is_not_two_independent_events(app):
    goal = app.goals.create("goal", actor="user", success_criteria=["success"])
    add_failure(app, goal["goal_id"])
    assert not planner(app).analyze()["gaps"]


def test_analysis_cannot_inflate_counts_or_erase_evidence(app):
    add_failure(app)
    add_failure(app)
    p = planner(app)
    analysis = p.analyze()
    analysis["gaps"][0]["evidence_refs"] = []
    analysis["gaps"][0]["evidence_count"] = 100
    assert not p.plan_candidates(analysis=analysis)["planned"]
    assert not app.evolution.all_candidates()


def test_stale_analysis_cannot_plan_recovered_tasks(app):
    tasks = [add_failure(app), add_failure(app)]
    p = planner(app)
    analysis = p.analyze()
    for t in tasks:
        t.task_status = TaskStatus.SUCCEEDED
        t.failure_reason = ""
        app.db.save_task(t)
    assert not p.plan_candidates(analysis=analysis)["planned"]


def test_empty_refs_are_not_traceable(app):
    assert not planner(app).traceability({"evidence_refs": []})["ok"]


def test_repeated_planning_reuses_evolution_candidate(app):
    add_failure(app)
    add_failure(app)
    p = planner(app)
    first = p.plan_candidates()
    second = p.plan_candidates()
    assert first["planned"][0]["candidate_id"] == second["planned"][0]["candidate_id"]
    assert len(app.evolution.all_candidates()) == 1


def test_no_backend_is_not_registered_candidate(app):
    add_failure(app)
    add_failure(app)
    out = CapabilityGapPlanner(app.db, experts=app.experts).plan_candidates()
    assert not out["planned"] and out["rejected"]


def test_fallback_requires_two_events_even_with_threshold_one(app):
    t = add_failure(app)
    t.task_status = TaskStatus.SUCCEEDED
    t.failure_reason = ""
    app.db.save_task(t)
    assert not planner(app, min_evidence=1).analyze()["gaps"]


@pytest.fixture
def repo():
    connection = sqlite3.connect(":memory:")
    yield AssetRepository(connection)
    connection.close()


def asset(repo, name, content, *, deps=(), active=True):
    a = repo.register_draft(
        AssetDraft(
            asset_id="skill:" + name,
            asset_type="skill",
            namespace="shared_validated",
            version="1",
            content=content,
            dependency_refs=deps,
            source_refs=("test/review4",),
        )
    )
    return _promote(repo, a) if active else a


def test_compose_reads_real_payload_and_registers_draft(repo):
    a = asset(repo, "a", {"required_inputs": ["source"], "produced_outputs": ["text"]})
    b = asset(repo, "b", {"required_inputs": ["text"], "produced_outputs": ["summary"]})
    result = compose(
        repo, [str(a.ref), str(b.ref)], new_asset_id="skill:combo", new_version="1"
    )
    assert result.io.required_inputs == ("source",) and result.io.produced_outputs == (
        "summary",
    )
    saved = repo.get_exact(result.asset_ref)
    assert saved.lifecycle_state == "draft"
    assert set(saved.dependency_refs) == {str(a.ref), str(b.ref)}
    assert saved.content["dependency_digests"] == {
        str(a.ref): a.content_digest,
        str(b.ref): b.content_digest,
    }


def test_compose_rejects_incompatible_real_chain_before_registration(repo):
    a = asset(repo, "a", {"produced_outputs": ["image"]})
    b = asset(repo, "b", {"required_inputs": ["text"]})
    with pytest.raises(ComposeError):
        compose(
            repo, [str(a.ref), str(b.ref)], new_asset_id="skill:bad", new_version="1"
        )
    assert repo.get("skill:bad", "1") is None


def test_compose_cannot_override_a_broken_chain(repo):
    a = asset(repo, "a", {"produced_outputs": ["image"]})
    b = asset(repo, "b", {"required_inputs": ["text"]})
    with pytest.raises(ComposeError):
        compose(
            repo,
            [str(a.ref), str(b.ref)],
            new_asset_id="skill:bad",
            new_version="1",
            io_overrides=AssetIO(),
        )


def test_compose_rejects_unverified_dependencies(repo):
    a = asset(repo, "a", {}, active=False)
    with pytest.raises(ComposeError):
        compose(repo, [str(a.ref)], new_asset_id="skill:bad", new_version="1")


def test_dependency_cycle_is_not_a_complete_closure(repo):
    a = asset(repo, "a", {}, deps=("skill:b@1",), active=False)
    asset(repo, "b", {}, deps=("skill:a@1",), active=False)
    with pytest.raises(ComposeError):
        dependency_closure(repo, str(a.ref))


def test_typed_downstream_needs_known_producer_type(repo):
    a = asset(repo, "a", {"produced_outputs": ["text"]})
    b = asset(
        repo, "b", {"required_inputs": ["text"], "input_types": {"text": "string"}}
    )
    with pytest.raises(ComposeError):
        compose(
            repo, [str(a.ref), str(b.ref)], new_asset_id="skill:bad", new_version="1"
        )


def test_goal_context_is_linked_to_each_distinct_gap(app):
    goal = app.goals.create("goal", actor="user", success_criteria=["success"])
    first = add_failure(app, goal["goal_id"])
    second = add_failure(app, goal["goal_id"])
    second.intent = "different_intent"
    app.db.save_task(second)
    result = planner(app).analyze()
    assert not result["gaps"] and len(result["insufficient"]) == 2
    assert all(
        "goal:" + goal["goal_id"] in r["evidence_refs"] for r in result["insufficient"]
    )


def test_real_composition_can_only_bind_after_explicit_evaluation(repo):
    from evoagent.assets.consumption import RuntimeAssetBinder, AssetNotConsumable

    a = asset(repo, "source", {"produced_outputs": ["text"]})
    result = compose(repo, [str(a.ref)], new_asset_id="skill:combo", new_version="1")
    binder = RuntimeAssetBinder(repo)
    with pytest.raises(AssetNotConsumable):
        binder.bind("skill", "combo", prefer_ref=result.asset_ref)
    # Test evaluation evidence is explicit; this does not claim real production evaluation.
    repo.transition("skill:combo", "1", "candidate")
    _promote(repo, repo.get_exact(result.asset_ref))
    pinned = binder.bind("skill", "combo", prefer_ref=result.asset_ref)
    assert (
        str(a.ref) in pinned
        and pinned[str(a.ref)]["content_digest"] == a.content_digest
    )
    upgraded = repo.register_draft(
        AssetDraft(
            asset_id=a.asset_id,
            asset_type="skill",
            namespace="shared_validated",
            version="2",
            content={"produced_outputs": ["new_text"]},
            source_refs=("test/new",),
        )
    )
    _promote(repo, upgraded)
    binder.verify_pinned(pinned)
    assert str(upgraded.ref) not in pinned


def test_io_override_cannot_invent_outputs(repo):
    a = asset(repo, "source", {"produced_outputs": ["text"]})
    with pytest.raises(ComposeError):
        compose(
            repo,
            [str(a.ref)],
            new_asset_id="skill:bad",
            new_version="1",
            io_overrides=AssetIO(produced_outputs=("secret",)),
        )
    assert repo.get("skill:bad", "1") is None


def test_retired_transitive_dependency_cannot_enter_new_composition(repo):
    base = asset(repo, "base", {})
    parent = asset(repo, "parent", {}, deps=(str(base.ref),))
    repo.transition(base.asset_id, base.version, "retired")
    with pytest.raises(ComposeError):
        compose(repo, [str(parent.ref)], new_asset_id="skill:bad", new_version="1")


def test_terminal_contract_cannot_be_mutated_by_handoff():
    c = contract([role("a", "one"), role("b", "two")])
    c.status = "completed"
    with pytest.raises(TeamContractError):
        TeamPlanner(builtin_experts()).add_handoff(
            c, from_role="a", to_role="b", input_refs=["a:out"]
        )
    assert not c.handoffs


def test_all_state_writers_must_acknowledge_team_completion():
    c = contract([role("a", "one", ("send",)), role("b", "two")])
    assert c.advance(role_id="a")["status"] == "active"
    waiting = c.advance(role_id="a")
    assert waiting["status"] == "active" and waiting["awaiting_roles"] == ["b"]
    revision = c.revision
    assert c.advance(role_id="a") == waiting and c.revision == revision
    assert not c.authorize_external_action(role_id="a", action="send")["ok"]
    assert c.advance(role_id="b")["status"] == "completed"
