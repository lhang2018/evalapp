"""ALN-26-B：动态团队与跨专家协作契约测试。

复用真实 builtin 能力目录；冲突/歧义场景用显式构造的 registry（fixture）。
覆盖：成组能力覆盖与缺口拒绝、冲突可解释、唯一状态写入者、交接校验、
失败隔离与不越权重放外部动作。
"""

import pytest

from evoagent.experts.builtin import builtin_experts
from evoagent.experts.registry import CapabilityEntry, Expert, ExpertRegistry
from evoagent.experts.team import (Handoff, TeamContract, TeamContractError,
                                   TeamFormationError, TeamPlanner, TeamRole)


@pytest.fixture
def planner():
    return TeamPlanner(builtin_experts())


def role(**overrides):
    values = dict(role_id="role_a", expert_id="a", domain="d",
                  capabilities=["cap_a"], responsibilities=["r"],
                  outputs=["role_a:output"], state_artifacts=["role_a:artifact"],
                  external_actions=[])
    values.update(overrides)
    return TeamRole(**values)


def manual_contract(roles, *, status="proposed"):
    contract = TeamContract(contract_id="team_manual", task_intent="intent",
                            domain="d", roles=roles, task_ref="task:t1")
    contract.status = status
    return contract


def test_form_team_covers_required_capabilities_with_real_registry(planner):
    contract = planner.form(intent="coding", domain="coding",
                            required_capabilities=["code_gen", "test"],
                            task_ref="task:t1")
    assert len(contract.roles) == 1
    assigned = contract.roles[0]
    assert assigned.expert_id == "coding"
    assert set(assigned.capabilities) == {"code_gen", "test"}
    assert contract.validate()["ok"] is True
    assert contract.digest()
    assert contract.status == "proposed"


def test_two_domains_form_two_roles_and_single_writer_each(planner):
    contract = planner.form(intent="coding", domain="coding",
                            required_capabilities=["code_gen", "itinerary"])
    assert {role.expert_id for role in contract.roles} == {"coding", "travel"}
    writers = contract.state_writers
    assert len(writers) == len(set(writers))
    assert len(writers) == 2
    for artifact, writer in writers.items():
        assert writer in {role.role_id for role in contract.roles}
    assert contract.validate()["ok"] is True


def test_missing_capability_is_refused_with_visible_gap(planner):
    with pytest.raises(TeamFormationError) as exc:
        planner.form(intent="coding", domain="coding",
                     required_capabilities=["code_gen", "no_such_cap_xyz"])
    assert "no_such_cap_xyz" in str(exc.value)
    with pytest.raises(TeamFormationError):
        planner.form(intent="coding", domain="coding", required_capabilities=[])


def test_ambiguous_capability_ownership_is_explainable():
    registry = ExpertRegistry()
    registry.register_expert(Expert(expert_id="alpha", capabilities=["cap_x"],
                                    applicable_intents=["intent_a"]))
    registry.register_expert(Expert(expert_id="beta", capabilities=["cap_x"],
                                    applicable_intents=["intent_a"]))
    with pytest.raises(TeamFormationError) as exc:
        TeamPlanner(registry).form(intent="intent_a", domain="",
                                   required_capabilities=["cap_x"])
    message = str(exc.value)
    assert "归属冲突" in message and "alpha" in message and "beta" in message


def test_domain_expert_resolves_ambiguity():
    registry = ExpertRegistry()
    registry.register_expert(Expert(expert_id="alpha", capabilities=["cap_x"],
                                    applicable_intents=[]))
    registry.register_expert(Expert(expert_id="beta", capabilities=["cap_x"],
                                    applicable_intents=["intent_b"]))
    contract = TeamPlanner(registry).form(intent="intent_b", domain="",
                                          required_capabilities=["cap_x"])
    assert contract.roles[0].expert_id == "beta"


def test_handoff_requires_authorized_inputs_and_no_self_handoff(planner):
    contract = planner.form(intent="coding", domain="coding",
                            required_capabilities=["code_gen", "itinerary"],
                            task_ref="task:t1")
    roles = {role.expert_id: role for role in contract.roles}
    producer = roles["coding"]
    result = planner.add_handoff(contract, from_role=producer.role_id,
                                 to_role=roles["travel"].role_id,
                                 input_refs=[producer.outputs[0]],
                                 budget_units=0.5)
    assert result["ok"] is True
    assert contract.handoffs and contract.handoffs[0].from_role == producer.role_id
    with pytest.raises(TeamContractError):
        planner.add_handoff(contract, from_role=producer.role_id,
                            to_role=roles["travel"].role_id,
                            input_refs=["role_coding:output:not_exists"])
    with pytest.raises(TeamContractError):
        planner.add_handoff(contract, from_role=producer.role_id,
                            to_role=producer.role_id,
                            input_refs=[producer.outputs[0]])
    with pytest.raises(TeamContractError):
        planner.add_handoff(contract, from_role="role_nope",
                            to_role=roles["travel"].role_id,
                            input_refs=["x"])


def test_contract_validation_rejects_duplicate_state_writers():
    first = role(role_id="role_a", state_artifacts=["shared_artifact"],
                 outputs=["role_a:output"])
    second = role(role_id="role_b", state_artifacts=["shared_artifact"],
                  outputs=["role_b:output"])
    contract = manual_contract([first, second])
    check = contract.validate()
    assert check["ok"] is False
    assert any("多个写入者" in error for error in check["errors"])


def test_only_role_owns_external_action_and_failed_role_needs_confirmation():
    writer = role(role_id="role_a", state_artifacts=["a_artifact"],
                  outputs=["a_output"], external_actions=["post_message"])
    other = role(role_id="role_b", state_artifacts=["b_artifact"],
                 outputs=["b_output"], external_actions=["post_message"])
    contract = manual_contract([writer, other], status="active")
    assert contract.authorize_external_action(
        role_id="role_b", action="post_message")["ok"] is True
    contract.fail("role_b", "外部服务超时")
    denied = contract.authorize_external_action(role_id="role_b",
                                                action="post_message")
    assert denied["ok"] is False and "人工确认" in denied["reason"]
    confirmed = contract.authorize_external_action(
        role_id="role_b", action="post_message", confirmed=True)
    assert confirmed["ok"] is True
    # 失败角色属于其它角色的动作不能代执行
    foreign = contract.authorize_external_action(role_id="role_a",
                                                 action="unknown_action")
    assert foreign["ok"] is False


def test_failure_is_local_and_never_auto_replays_external_actions():
    writer = role(role_id="role_a", state_artifacts=["a_artifact"],
                  outputs=["a_output"], external_actions=["send_email"])
    contract = manual_contract([writer], status="active")
    contract.fail("role_a", "发送失败")
    recovery = contract.recovery()
    assert recovery["failed_roles"] == ["role_a"]
    assert recovery["auto_replay"] is False
    assert recovery["requires_confirmation"] is True
    assert recovery["external_actions"] == [
        {"role_id": "role_a", "action": "send_email"}]
    # 失败角色不能推进契约
    advanced = contract.advance(role_id="role_a")
    assert advanced["ok"] is False


def test_only_state_writer_can_advance_contract():
    writer = role(role_id="role_a", state_artifacts=["a_artifact"],
                  outputs=["a_output"])
    observer = role(role_id="role_observer", expert_id="obs",
                    state_artifacts=[], outputs=[])
    contract = manual_contract([writer, observer])
    refused = contract.advance(role_id="role_observer")
    assert refused["ok"] is False and "非唯一状态写入者" in refused["reason"]
    assert contract.advance(role_id="role_a") == {
        "ok": True, "status": "active", "previous": "proposed"}
    assert contract.advance(role_id="role_a")["status"] == "completed"
    terminal = contract.advance(role_id="role_a")
    assert terminal["ok"] is False and "终态" in terminal["reason"]


def test_contract_digest_is_stable_and_changes_with_handoff(planner):
    first = planner.form(intent="coding", domain="coding",
                         required_capabilities=["code_gen", "itinerary"],
                         task_ref="task:t1")
    second = planner.form(intent="coding", domain="coding",
                          required_capabilities=["code_gen", "itinerary"],
                          task_ref="task:t1")
    assert first.digest() == second.digest()
    roles = {role.expert_id: role for role in first.roles}
    planner.add_handoff(first, from_role=roles["coding"].role_id,
                        to_role=roles["travel"].role_id,
                        input_refs=[roles["coding"].outputs[0]])
    assert first.digest() != second.digest()
