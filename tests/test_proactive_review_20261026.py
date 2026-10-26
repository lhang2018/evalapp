"""Real component boundaries for proactive admission and planning identities."""
from types import SimpleNamespace

import pytest

from evoagent.core.persistence import Database
from evoagent.app import App
from evoagent.core.resource_admission import ResourceGovernor
from evoagent.core.task import Task, TaskStatus
from evoagent.experts.registry import Expert, ExpertRegistry
from evoagent.experts.team import TeamPlanner
from evoagent.goals.gap_planner import CapabilityGapPlanner
from evoagent.goals.proactivity import ProactivityGovernor


@pytest.mark.parametrize("sample", [None, {}, {"cpu": float("nan")}, {"cpu": -0.1}, {"cpu": False}, {"cpu": "0.1"}])
def test_real_resource_adapter_does_not_admit_proactivity_on_unknown_samples(sample):
    resources = ResourceGovernor(lambda: sample)
    assert not ProactivityGovernor(resource_governor=resources).may_start().allowed


def test_real_sampler_exception_does_not_become_proactive_zero_pressure():
    def broken():
        raise OSError("sensor unavailable")
    resources = ResourceGovernor(broken)
    assert not ProactivityGovernor(resource_governor=resources).may_start().allowed
    # Preserve the resource layer's established foreground/compatibility policy.
    assert resources.admit("user").decision == "admit"


def test_known_low_pressure_remains_admissible():
    resources = ResourceGovernor(lambda: {"cpu": 0.1, "memory": 0.2, "model_pool": 0.3})
    assert ProactivityGovernor(resource_governor=resources).may_start().allowed


def planner(owner="owner", version="1", capabilities=None):
    registry = ExpertRegistry()
    registry.register_expert(Expert(owner, version=version, capabilities=capabilities or ["cap"]))
    return TeamPlanner(registry)


def form(p):
    return p.form(intent="i", domain="d", required_capabilities=["cap"], task_ref="task:one")


def test_default_team_identity_is_scoped_to_task():
    p = planner()
    a = p.form(intent="i", domain="d", required_capabilities=["cap"], task_ref="task:a")
    b = p.form(intent="i", domain="d", required_capabilities=["cap"], task_ref="task:b")
    assert a.contract_id != b.contract_id


def test_team_identity_does_not_alias_delimited_intent_and_domain():
    p = planner()
    a = p.form(intent="a|b", domain="c", required_capabilities=["cap"])
    b = p.form(intent="a", domain="b|c", required_capabilities=["cap"])
    assert a.contract_id != b.contract_id


def test_team_identity_does_not_alias_one_capability_with_two():
    p = planner(capabilities=["a,b", "a", "b"])
    a = p.form(intent="i", domain="d", required_capabilities=["a,b"])
    b = p.form(intent="i", domain="d", required_capabilities=["a", "b"])
    assert a.contract_id != b.contract_id


def test_team_identity_tracks_the_selected_expert():
    assert form(planner("first")).contract_id != form(planner("second")).contract_id


def test_team_identity_and_role_snapshot_track_expert_version():
    a, b = form(planner(version="1")), form(planner(version="2"))
    assert a.contract_id != b.contract_id
    assert b.to_dict()["roles"][0]["expert_version"] == "2"


def test_same_team_declaration_has_stable_default_identity():
    p = planner()
    assert form(p).contract_id == form(p).contract_id


def analyze_domains(pairs):
    db = Database(":memory:")
    try:
        for domain, intent in pairs:
            for _ in range(2):
                db.save_task(Task(user_goal="fixture", domain=domain, intent=intent,
                    task_status=TaskStatus.FAILED, failure_reason="missing tool"))
        experts = SimpleNamespace(route_with_match=lambda *args: (None, False))
        return CapabilityGapPlanner(db, experts=experts).analyze()["gaps"]
    finally:
        db.close()


def test_delimited_domains_do_not_mix_independent_gap_evidence():
    gaps = analyze_domains([("part:a", "b"), ("part", "a:b")])
    assert len(gaps) == 2
    assert len({g["gap_id"] for g in gaps}) == 2
    assert {g["domain"] for g in gaps} == {"part:a", "part"}
    assert all(g["distinct_tasks"] == 2 for g in gaps)


def test_literal_percent_sequences_remain_distinct_from_escaped_separators():
    gaps = analyze_domains([("part:a", "b"), ("part%3Aa", "b")])
    assert len(gaps) == 2 and len({g["capability"] for g in gaps}) == 2


@pytest.mark.parametrize("value", [float("inf"), 2.0])
def test_invalid_high_pressure_is_not_changed_into_foreground_fail_open(value):
    resources = ResourceGovernor(lambda: {"cpu": value})
    assert resources.admit("user").decision == "defer"
    assert not ProactivityGovernor(resource_governor=resources).may_start().allowed


def test_explicit_contract_id_is_not_rewritten():
    p = planner()
    c = p.form(intent="i", domain="d", required_capabilities=["cap"],
               task_ref="task:one", contract_id="caller-owned")
    assert c.contract_id == "caller-owned"


def test_existing_role_version_snapshot_does_not_follow_registry_mutation():
    p = planner(version="1")
    original = form(p)
    p.registry.get("owner").version = "2"
    assert original.roles[0].expert_version == "1"
    assert form(p).contract_id != original.contract_id


def test_distinct_domain_gaps_register_distinct_real_candidates(tmp_path):
    app = App(data_dir=str(tmp_path / "app"))
    try:
        for domain, intent in [("part:a", "b"), ("part", "a:b")]:
            for _ in range(2):
                app.db.save_task(Task(user_goal="fixture", domain=domain, intent=intent,
                    task_status=TaskStatus.FAILED, failure_reason="missing tool"))
        experts = SimpleNamespace(route_with_match=lambda *args: (None, False))
        p = CapabilityGapPlanner(app.db, experts=experts, evolution=app.evolution)
        outcome = p.plan_candidates()
        assert outcome["counts"]["planned"] == 2
        candidates = app.evolution.all_candidates()
        assert len(candidates) == 2
        assert len({c.target_component for c in candidates}) == 2
        assert {c.target_domain for c in candidates} == {"part:a", "part"}
        assert p.plan_candidates()["counts"]["planned"] == 2
        assert len(app.evolution.all_candidates()) == 2
    finally:
        app.db.close()
