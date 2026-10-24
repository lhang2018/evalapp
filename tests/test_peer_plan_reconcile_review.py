"""Independent negative and real-ledger review probes for ALN-07-E."""
from dataclasses import FrozenInstanceError
import copy

import pytest

from evoagent.core.persistence import Database
from evoagent.core.side_effects import SideEffectLedger
from evoagent.planning.planner import ExecutionPlan, PlanStep
from evoagent.planning.reconcile import ReconciliationError, ReconciliationReport, SideEffectReconciler, reconcile_plan


def plan(*steps):
    return ExecutionPlan("p", "g", list(steps))


def step(sid="s", state="done"):
    return PlanStep(sid, sid, tool_hint="file_write", state=state)


def row(eid="e", task="t", status="confirmed", **kw):
    return dict(effect_id=eid, task_id=task, tool_id="file_write", status=status, **kw)


def test_other_task_cannot_satisfy_own_completed_step():
    with pytest.raises(ReconciliationError):
        reconcile_plan(plan(step()), [row(task="other")], task_id="t")


def test_mixed_task_projection_without_scope_is_rejected():
    with pytest.raises(ReconciliationError):
        reconcile_plan(plan(step("a"), step("b")), [row("e1"), row("e2", task="other")])


def test_single_task_is_inferred_and_preserved_in_record():
    report = reconcile_plan(plan(step()), [row(status="unknown")])
    assert report.task_id == "t"
    assert report.uncertain[0].to_dict()["task_id"] == "t"


def test_missing_task_reference_is_rejected():
    missing = row()
    del missing["task_id"]
    with pytest.raises(ReconciliationError):
        reconcile_plan(plan(step()), [missing], task_id="t")


def test_duplicate_effect_cannot_cover_two_declarations():
    with pytest.raises(ReconciliationError):
        reconcile_plan(plan(step("a"), step("b")), [row(), row()])


@pytest.mark.parametrize("states", [("done", "done"), ("pending", "done")])
def test_same_tool_without_step_binding_is_ambiguous(states):
    report = reconcile_plan(plan(step("a", states[0]), step("z", states[1])), [row()])
    assert not report.ok and not report.matched
    assert report.summary["ambiguous"] == 1
    assert {d.step_id for d in report.unexecuted} == ({"a", "z"} if states[0] == "done" else {"z"})


def test_explicit_step_bindings_resolve_same_tool_without_guessing():
    report = reconcile_plan(plan(step("a"), step("z")), [row("e1"), row("e2")],
                            step_bindings={"e1": "z", "e2": "a"})
    assert report.ok
    assert {(m.step_id, m.effect_id) for m in report.matched} == {("z", "e1"), ("a", "e2")}


def test_unknown_binding_is_rejected():
    with pytest.raises(ReconciliationError):
        reconcile_plan(plan(step()), [row()], step_bindings={"missing-effect": "s"})


@pytest.mark.parametrize("status", [None, "", "garbage"])
def test_invalid_ledger_status_is_rejected(status):
    with pytest.raises(ReconciliationError):
        reconcile_plan(plan(step()), [row(status=status)])


@pytest.mark.parametrize("bad_plan", [None, object(), plan(), plan(step("same"), step("same")), plan(step(state="garbage"))])
def test_invalid_plan_does_not_report_clean(bad_plan):
    with pytest.raises(ReconciliationError):
        reconcile_plan(bad_plan, [])


@pytest.mark.parametrize("status", ["failed", "compensated"])
def test_known_terminal_result_is_not_reported_as_unknown(status):
    report = reconcile_plan(plan(step()), [row(status=status)])
    assert not report.ok and report.unexecuted
    assert not report.matched and not report.uncertain
    assert [r.status for r in report.resolved] == [status]


def test_deserialized_ok_is_derived_from_details():
    data = reconcile_plan(plan(step()), [row(status="unknown")]).to_dict()
    data["ok"] = True
    assert not ReconciliationReport.from_dict(data).ok


def test_deserialization_does_not_drop_invalid_rows():
    data = reconcile_plan(plan(step()), [row()]).to_dict()
    data["uncertain"] = ["unreadable evidence"]
    with pytest.raises(ReconciliationError):
        ReconciliationReport.from_dict(data)


def test_report_is_a_frozen_snapshot_with_independent_serialization():
    report = reconcile_plan(plan(step()), [row(status="unknown")])
    before = copy.deepcopy(report.to_dict())
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        report.uncertain.clear()
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        report.ok = True
    data = report.to_dict()
    data["uncertain"].clear()
    assert report.to_dict() == before


def test_real_sqlite_ledger_remains_unchanged_and_retains_task_scope(tmp_path):
    db = Database(str(tmp_path / "state.db"))
    try:
        ledger = SideEffectLedger(db)
        ledger.record("t", "file_write", status="confirmed", run_id="run-t")
        ledger.record("other", "file_write", status="confirmed", run_id="run-other")
        before = ledger.history("t")
        changes = db._conn.total_changes
        import sqlite3
        writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
        db._conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in writes else sqlite3.SQLITE_OK)
        report = SideEffectReconciler().reconcile_ledger(plan(step()), ledger, task_id="t")
        assert report.ok and len(report.matched) == 1
        assert ledger.history("t") == before and db._conn.total_changes == changes
        assert len(ledger.history("other")) == 1
    finally:
        db._conn.set_authorizer(None)
        db.close()


def test_ledger_entry_requires_nonempty_task_scope(tmp_path):
    db = Database(str(tmp_path / "state.db"))
    try:
        with pytest.raises(ReconciliationError):
            SideEffectReconciler().reconcile_ledger(plan(step()), SideEffectLedger(db))
    finally:
        db.close()


def test_resolved_category_cannot_hide_a_confirmed_extra_effect():
    data = reconcile_plan(plan(step()), [row()]).to_dict()
    data["resolved"] = [row("unexpected")]
    with pytest.raises(ReconciliationError):
        ReconciliationReport.from_dict(data)


def test_serialized_report_cannot_drop_missing_declarations():
    data = reconcile_plan(plan(step()), []).to_dict()
    data["unexecuted"] = []
    data["ok"] = True
    with pytest.raises(ReconciliationError):
        ReconciliationReport.from_dict(data)


def test_corrupt_completed_work_is_not_a_clean_pending_plan():
    p = plan(step(state="pending"))
    p.completed_work = ["s"]
    with pytest.raises(ReconciliationError):
        reconcile_plan(p, [])


def test_false_tool_hint_is_invalid_rather_than_silently_absent():
    p = plan(step())
    p.steps[0].tool_hint = False
    with pytest.raises(ReconciliationError):
        reconcile_plan(p, [])


def test_duplicate_tool_bindings_and_new_categories_roundtrip():
    p = plan(step("a"), step("z"))
    records = [row("e1"), row("e2", status="failed")]
    report = reconcile_plan(p, records)
    assert report.ambiguous and report.resolved and not report.ok
    assert ReconciliationReport.from_dict(report.to_dict()).to_dict() == report.to_dict()
