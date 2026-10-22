"""Independent boundary acceptance for ALN-16."""
import pytest

from evoagent.api.auth import ApiAuth
from evoagent.core.events import RunEvent
from evoagent.observability import (
    CredentialError, CredentialRegistry, EventRefs, EvidenceContractError,
    EvidenceEvent, EvidenceExporter, TokenRing, build_decision_chain,
)


@pytest.mark.parametrize("entry", ["export", "chain"])
def test_run_event_uses_the_same_validation_as_dict_input(entry):
    event = RunEvent(trust="untrusted", untrusted_origin=None)
    with pytest.raises(EvidenceContractError):
        if entry == "export":
            EvidenceExporter().export([event], actor="reviewer", reason="test")
        else:
            build_decision_chain([event])


def test_legacy_error_events_infer_fault_severity():
    assert EvidenceEvent.from_run_event(RunEvent(severity="error")).fault_level == "degraded"


@pytest.mark.parametrize("kind", ["registry", "token"])
def test_rotation_does_not_extend_existing_expiry(kind):
    if kind == "registry":
        ring = CredentialRegistry()
        ring.register("old", "api_token", secret="old-value", not_before=0, not_after=20)
        ring.rotate("old", "new", secret="new-value", at=10, grace_seconds=100)
        assert not ring.verify_secret("old", "old-value", at=21)
    else:
        ring = TokenRing()
        ring.add("old-value", "operator", key_id="old", not_after=20)
        ring.rotate("old", "new-value", at=10, grace_seconds=100)
        assert ring.role_for("old-value", at=21) is None


@pytest.mark.parametrize("kind", ["registry", "token"])
def test_failed_rotation_leaves_old_credential_unchanged(kind):
    if kind == "registry":
        ring = CredentialRegistry()
        ring.register("old", "api_token", secret="old-value", not_before=0)
        call = lambda: ring.rotate("old", "new", secret="", at=10)
    else:
        ring = TokenRing()
        ring.add("old-value", "operator", key_id="old")
        call = lambda: ring.rotate("old", "", at=10)
    before = ring.status("old")
    with pytest.raises(CredentialError):
        call()
    assert ring.status("old") == before


def test_revoked_ring_token_cannot_fall_back_to_static_config():
    ring = TokenRing()
    key = ring.add("same-value", "admin")
    auth = ApiAuth(admin_token="same-value", token_ring=ring)
    ring.revoke(key, reason="compromised")
    assert not auth.check("Bearer same-value", "GET", "/tasks").ok
    assert ApiAuth(admin_token="other", token_ring=ring).check(
        "Bearer other", "GET", "/tasks").ok


def test_scoped_chain_excludes_other_tasks_records():
    event = EvidenceEvent(event_id="event", refs=EventRefs(task_id="t1", run_id="r1", approval_id="ap"))
    chain = build_decision_chain([event], task_id="t1", run_id="r1", approvals=[
        {"approval_id": "ap", "task_id": "t2", "run_id": "r2", "status": "approved"}])
    assert all(node.kind != "approval" for node in chain.nodes)
    assert {"kind": "approval", "id": "ap", "referenced_by": "event"} in chain.missing_refs


def test_chain_digest_is_independent_of_event_input_order():
    parent = EvidenceEvent(event_id="parent", timestamp=1)
    left = EvidenceEvent(event_id="left", timestamp=2, causation_id="parent")
    right = EvidenceEvent(event_id="right", timestamp=3, causation_id="parent")
    assert build_decision_chain([parent, left, right]).digest() == build_decision_chain([right, parent, left]).digest()


def test_chain_reports_missing_cause_even_without_run_filter():
    event = EvidenceEvent(event_id="event", causation_id="missing")
    assert {"kind": "event", "id": "missing", "referenced_by": "event"} in build_decision_chain([event]).missing_refs


def test_public_trust_keys_can_rotate_without_a_private_secret():
    registry = CredentialRegistry()
    registry.register("old", "client_trust", public_key="PUBLIC-OLD", not_before=0)
    registry.rotate("old", "new", public_key="PUBLIC-NEW", at=10, grace_seconds=20)
    assert registry.status("old")["not_after"] == 30
    assert {key["key_id"] for key in registry.export_public_keys()} == {"old", "new"}


def test_same_token_cannot_be_registered_twice_to_bypass_revocation():
    ring = TokenRing()
    key = ring.add("same-value", "admin")
    with pytest.raises(CredentialError):
        ring.add("same-value", "operator")
    ring.revoke(key, reason="compromised")
    with pytest.raises(CredentialError):
        ring.add("same-value", "admin")


def test_scoped_chain_can_link_global_release_evidence_without_unrelated_nodes():
    event = EvidenceEvent(event_id="event", refs=EventRefs(task_id="t1", release_id="rel1"))
    chain = build_decision_chain([event], task_id="t1", releases=[
        {"release_id": "rel1", "candidate_id": "c1"},
        {"release_id": "unrelated", "candidate_id": "c2"}],
        evaluations=[{"candidate_id": "c1"}, {"candidate_id": "c2"}])
    assert {node.node_id for node in chain.nodes} == {"event", "rel1", "c1"}
