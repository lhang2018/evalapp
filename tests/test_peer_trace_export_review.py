"""Independent review probes for dfcfcee: real storage and unsafe boundaries."""
from dataclasses import replace
import json
import os

import pytest

from evoagent.core.events import EventBus
from evoagent.core.audit import AuditLog
from evoagent.core.log_layers import ExecutionTracer, LayerManager
from evoagent.core.persistence import Database
from evoagent.evolution.diagnostics import DiagnosticCase, DiagnosticExperiment, Hypothesis
from evoagent.observability.contract import canonical_digest
from evoagent.observability.export import (
    DeletionScopeError, EvidenceExporter, ExportDenied, execute_deletion,
    export_to_file, plan_deletion,
)
from evoagent.planning.planner import ExecutionPlan, PlanStep, RecoveryPolicy
from evoagent.planning.trace import PlanTrace, compile_trace


@pytest.fixture
def storage(tmp_path):
    db = Database(str(tmp_path / "state.db"))
    bus = EventBus(str(tmp_path / "events.db"))
    ExecutionTracer(bus, db)
    manager = LayerManager(db, bus)
    yield db, bus, manager
    bus._conn.close()
    db.close()


def seed(db, bus, task):
    event = bus.emit(event_type="tool_call", task_id=task)
    db.save_evidence("ev-" + task, task, "file", "ref", "body")
    return event


def test_real_layer_manager_counts_and_durable_receipt(storage):
    db, bus, manager = storage
    event = seed(db, bus, "a")
    seed(db, bus, "b")
    plan = plan_deletion([event], task_id="a", actor="admin", reason="erase")
    out = execute_deletion(manager, plan, actor="admin", reason="execute")
    assert out["ok"] and out["total"] == 3
    assert out["counts"]["diagnostics"] == {"cases": 0, "hypotheses": 0, "experiments": 0}
    assert bus.count("a") == 0 and bus.count("b") == 1
    assert not db.list_traces(task_id="a") and not db.list_evidence(task_id="a")
    reopened = Database(db.path)
    try:
        receipts = [r for r in reopened.list_audit() if r["action"] == "privacy_deletion"]
        assert any(r["outcome"] == "ok" and r["detail"]["digest"] == plan.digest
                   and r["detail"]["executed_total"] == 3 for r in receipts)
    finally:
        reopened.close()
    again = execute_deletion(manager, plan, actor="admin", reason="replay")
    assert again["ok"] and again["total"] == 0


def test_changed_scope_rejected_before_any_deletion(storage):
    db, bus, manager = storage
    a = seed(db, bus, "a")
    seed(db, bus, "b")
    plan = plan_deletion([a], task_id="a", actor="admin", reason="r")
    with pytest.raises(DeletionScopeError):
        execute_deletion(manager, replace(plan, scope="b"), actor="admin", reason="r")
    assert bus.count("a") == bus.count("b") == 1


def test_digest_cannot_be_bypassed(storage):
    db, bus, manager = storage
    a = seed(db, bus, "a")
    plan = plan_deletion([a], task_id="a", actor="admin", reason="r")
    with pytest.raises(DeletionScopeError):
        execute_deletion(manager, replace(plan, digest="tampered"), actor="admin",
                         reason="r", verify_digest=False)
    assert bus.count("a") == 1


def test_empty_event_snapshot_still_erases_task_evidence(storage):
    db, bus, manager = storage
    db.save_evidence("only-evidence", "a", "file", "ref", "private")
    plan = plan_deletion([], task_id="a", actor="admin", reason="r")
    out = execute_deletion(manager, plan, actor="admin", reason="r")
    assert out["ok"] and out["total"] == 1
    assert not db.list_evidence(task_id="a")


def test_real_diagnostic_children_counted_and_deleted(storage):
    db, bus, manager = storage
    case = DiagnosticCase(task_id="a")
    db.save_diagnostic_case(case)
    hypothesis = Hypothesis(case_id=case.case_id)
    db.save_hypothesis(hypothesis)
    db.save_experiment(DiagnosticExperiment(case_id=case.case_id,
                                           hypothesis_id=hypothesis.hypothesis_id))
    out = execute_deletion(manager, plan_deletion([], task_id="a", actor="admin", reason="r"),
                           actor="admin", reason="r")
    assert out["counts"]["diagnostics"] == {"cases": 1, "hypotheses": 1, "experiments": 1}
    assert out["total"] == 3 and db.get_case_by_task("a") is None


def test_live_cross_task_event_reference_rejected(storage):
    db, bus, manager = storage
    a = seed(db, bus, "a")
    b = seed(db, bus, "b")
    plan = plan_deletion([a], task_id="a", actor="admin", reason="r")
    digest = canonical_digest([b.event_id])
    plan = replace(plan, event_ids=(b.event_id,), digest=digest,
                   tombstone={**plan.tombstone, "digest": digest})
    with pytest.raises(DeletionScopeError):
        execute_deletion(manager, plan, actor="admin", reason="r")
    assert bus.count("a") == bus.count("b") == 1


def test_successful_export_hash_matches_real_file(tmp_path):
    import hashlib
    result = export_to_file(EvidenceExporter(), [], "report.json", actor="admin",
                            reason="r", allowed_root=str(tmp_path))
    assert result["sha256"] == hashlib.sha256((tmp_path / "report.json").read_bytes()).hexdigest()


def test_export_audit_is_durable_when_store_is_provided(storage, tmp_path):
    db, bus, manager = storage
    result = export_to_file(EvidenceExporter(), [], "report.json", actor="admin",
                            reason="audit", allowed_root=str(tmp_path), audit_log=AuditLog(db))
    assert result["audit_persisted"]
    reopened = Database(db.path)
    try:
        row = next(r for r in reopened.list_audit() if r["audit_id"] == result["audit_id"])
        assert row["detail"]["sha256"] == result["sha256"]
        assert row["detail"]["reason"] == "audit"
    finally:
        reopened.close()


def test_export_missing_audit_store_rejected_before_write(tmp_path):
    with pytest.raises(ExportDenied):
        export_to_file(EvidenceExporter(), [], "report.json", actor="admin",
                       reason="r", allowed_root=str(tmp_path), audit_log=AuditLog())
    assert not (tmp_path / "report.json").exists()


def test_same_graph_compiles_independent_of_storage_order():
    steps = [step("z", "done"), step("a", deps=["z"]), step("middle", "pending")]
    a = compile_trace(make_plan(steps)).to_dict()
    b = compile_trace(make_plan(list(reversed(steps)))).to_dict()
    assert a == b
    ids = [s["step_id"] for s in a["steps"]]
    assert ids.index("z") < ids.index("a")


def test_legacy_custom_deleter_reports_lack_of_durable_audit():
    class Custom:
        def delete_task(self, task_id):
            return {"events": 0}
    result = execute_deletion(Custom(), plan_deletion([], task_id="a", actor="admin", reason="r"),
                              actor="admin", reason="r")
    assert result["ok"] and not result["audit_persisted"] and not result["audit_ids"]


def test_deserialized_unknown_recovery_action_rejected():
    data = make_plan([step("a")]).to_dict()
    data["recovery_policies"] = [{"category": "risk", "action": "arbitrary_shell"}]
    with pytest.raises(ValueError):
        ExecutionPlan.from_dict(data)


def test_audit_failure_prevents_deletion(storage, monkeypatch):
    db, bus, manager = storage
    a = seed(db, bus, "a")
    plan = plan_deletion([a], task_id="a", actor="admin", reason="r")
    def fail(*args, **kwargs):
        raise OSError("audit unavailable")
    monkeypatch.setattr(db, "save_audit", fail)
    with pytest.raises(OSError, match="audit unavailable"):
        execute_deletion(manager, plan, actor="admin", reason="r")
    assert bus.count("a") == 1


def test_partial_delete_failure_is_audited(storage, monkeypatch):
    db, bus, manager = storage
    a = seed(db, bus, "a")
    plan = plan_deletion([a], task_id="a", actor="admin", reason="r")
    def fail(*args, **kwargs):
        raise OSError("layer unavailable")
    monkeypatch.setattr(db, "delete_traces", fail)
    with pytest.raises(OSError, match="layer unavailable"):
        execute_deletion(manager, plan, actor="admin", reason="r")
    rows = [r for r in db.list_audit() if r["action"] == "privacy_deletion"]
    assert any(r["outcome"] == "failed" for r in rows)
    assert bus.count("a") == 0 and db.list_traces(task_id="a")


@pytest.mark.parametrize("hardlink", [False, True])
def test_export_does_not_clobber_existing_file(tmp_path, hardlink):
    root = tmp_path / "exports"
    root.mkdir()
    victim = tmp_path / "important.txt"
    victim.write_text("keep me", encoding="utf-8")
    target = root / "report.json"
    if hardlink:
        os.link(victim, target)
    else:
        target.write_text("existing report", encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(ExportDenied):
        export_to_file(EvidenceExporter(), [], str(target), actor="admin", reason="r",
                       allowed_root=str(root))
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert target.read_bytes() == before


def make_plan(steps, **kw):
    return ExecutionPlan(plan_id="p", goal="g", steps=steps, **kw)


def step(sid, state="failed", deps=()):
    return PlanStep(sid, sid, state=state, deps=list(deps))


def test_failed_dependency_with_pending_ancestor_is_blocked():
    plan = make_plan([step("p", "pending"), step("f", deps=["p"]), step("x", deps=["f"])])
    by_id = {c.step_id: c for c in compile_trace(plan).recovery_candidates}
    assert by_id["x"].kind == "blocked"
    assert set(by_id["x"].requires) == {"p", "f"}


def test_mixed_pending_and_failed_dependencies_are_not_hidden():
    plan = make_plan([step("p", "running"), step("f"), step("x", deps=["p", "f"])])
    by_id = {c.step_id: c for c in compile_trace(plan).recovery_candidates}
    assert by_id["x"].kind == "blocked" and set(by_id["x"].requires) == {"p", "f"}


def test_infeasible_dependency_propagates_to_child():
    plan = make_plan([step("f"), step("x", deps=["f"])], completed_work=["f"])
    by_id = {c.step_id: c for c in compile_trace(plan).recovery_candidates}
    assert by_id["f"].kind == by_id["x"].kind == "infeasible"


@pytest.mark.parametrize("steps", [
    [step("a", deps=["b"]), step("b", deps=["a"])],
    [step("a"), step("a")],
    [step("a", deps=["missing"])],
    [step("a", state="unknown")],
])
def test_invalid_graph_is_explicitly_rejected(steps):
    with pytest.raises(ValueError):
        compile_trace(make_plan(steps))


def test_trace_snapshot_has_no_alias_to_live_plan_or_serialized_result():
    plan = make_plan([step("a")], revisions=[{"added": ["a"]}],
                     acceptance_criteria=[{"params": {"values": [1]}}])
    trace = compile_trace(plan)
    snapshot = json.loads(json.dumps(trace.to_dict()))
    plan.revisions[0]["added"].append("changed")
    plan.acceptance_criteria[0]["params"]["values"].append(2)
    assert trace.to_dict() == snapshot
    exported = trace.to_dict()
    exported["revisions"][0]["added"].append("mutate export")
    assert trace.to_dict() == snapshot


def test_trace_deserialization_owns_its_nested_values():
    data = compile_trace(make_plan([step("a")], revisions=[{"added": ["a"]}])).to_dict()
    trace = PlanTrace.from_dict(data)
    data["revisions"][0]["added"].append("changed")
    assert trace.to_dict()["revisions"][0]["added"] == ["a"]
    with pytest.raises(TypeError):
        trace.states["a"] = "done"


def test_recovery_policy_does_not_allow_unsupported_action():
    with pytest.raises(ValueError):
        RecoveryPolicy(category="risk", action="arbitrary_shell")


def test_recovery_policy_params_are_immutable_snapshots():
    params = {"tools": ["safe"], "options": {"count": 1}}
    policy = RecoveryPolicy(category="risk", action="partial_retry", params=params)
    plan = make_plan([step("a")], recovery_policies=[policy])
    before = json.loads(json.dumps(plan.to_dict()))
    params["tools"].append("external")
    returned = plan.recovery_for_failure("a")[0].to_dict()
    returned["params"]["options"]["count"] = 999
    assert plan.to_dict() == before
    with pytest.raises(TypeError):
        plan.recovery_for_failure("a")[0].params["options"]["count"] = 999
