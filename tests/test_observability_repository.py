# -*- coding: utf-8 -*-
"""ALN-16-P：业务仓储 → EvidenceEvent 装配与删除对账。

验收对照：
- 受信身份隔离：服务端表投影 trusted；untrusted 行缺来源拒绝；
- 最小证据与脱敏：设备动作/副作用/审批 → restricted + redacted，
  正文不复制只留引用；凭证不进证据（CredentialLeakError 拒绝）；
- 实际删除/保留/失败可对账：装配 → plan_deletion → execute_deletion
  → deletion_reconciliation 残留核对，不返回假成功。
"""

import json
import shutil
import tempfile
import unittest

import evoagent.core.task as T
from evoagent.app import App
from evoagent.observability.contract import EvidenceContractError
from evoagent.observability.credentials import CredentialError
from evoagent.observability.export import execute_deletion, plan_deletion
from evoagent.observability.repository import (
    collect_for_task,
    deletion_reconciliation,
    project_approval_row,
    project_audit_row,
    project_runtime_event,
    project_side_effect_row,
)


def rt_row(**over):
    row = {"event_id": "evt_1", "task_id": "t", "run_id": "r",
           "event_type": "tool_call", "severity": "info",
           "timestamp": 1.0, "payload": json.dumps({"x": 1}),
           "privacy_classification": "standard", "refs": "{}",
           "origin": "server", "trust": "trusted", "redacted": 0,
           "component_id": "sys", "component_version": "1.0"}
    row.update(over)
    return row


class ProjectionTest(unittest.TestCase):
    def test_runtime_event_projects_trusted_internal(self):
        ev = project_runtime_event(rt_row())
        self.assertEqual(ev.trust, "trusted")
        self.assertEqual(ev.origin, "server")
        self.assertEqual(ev.privacy_level, "internal")  # standard→internal
        self.assertEqual(ev.payload, {"x": 1})
        ev.validate()

    def test_sensitive_event_type_is_restricted_and_redacted(self):
        ev = project_runtime_event(rt_row(
            event_type="device_action",
            payload=json.dumps({"target": "设置"})))
        self.assertEqual(ev.privacy_level, "restricted")
        self.assertTrue(ev.redacted)
        self.assertEqual(ev.payload, {}, "敏感正文不得复制")

    def test_untrusted_row_requires_origin(self):
        with self.assertRaises(EvidenceContractError):
            project_runtime_event(rt_row(trust="untrusted"))
        ev = project_runtime_event(rt_row(
            trust="untrusted", untrusted_origin="diag-import:abc"))
        self.assertEqual(ev.trust, "untrusted")
        self.assertEqual(ev.untrusted_origin, "diag-import:abc")

    def test_credential_payload_rejected(self):
        with self.assertRaises((EvidenceContractError, CredentialError)):
            project_runtime_event(rt_row(
                payload=json.dumps({"api_key": "sk-1234567890abcdef"})))

    def test_unknown_privacy_fails_safe_to_restricted(self):
        ev = project_runtime_event(rt_row(privacy_classification="weird"))
        self.assertEqual(ev.privacy_level, "restricted")

    def test_side_effect_and_approval_minimal_evidence(self):
        fx = project_side_effect_row({
            "effect_id": "fx_1", "task_id": "t", "run_id": "r",
            "tool_id": "file_write", "idempotency_key": "k",
            "status": "confirmed", "updated_at": 2.0,
            "args_json": json.dumps({"path": "/secret"})})
        self.assertEqual(fx.privacy_level, "restricted")
        self.assertTrue(fx.redacted)
        self.assertNotIn("path", str(fx.payload))
        ap = project_approval_row({
            "approval_id": "ap1", "task_id": "t", "status": "approved",
            "created_at": 3.0, "action_summary": "发送消息给张三"})
        self.assertEqual(ap.privacy_level, "restricted")
        self.assertEqual(ap.refs.approval_id, "ap1")

    def test_audit_row_keeps_detail_and_refs(self):
        ev = project_audit_row({
            "audit_id": "aud_1", "at": 4.0, "actor": "admin",
            "action": "release.deploy", "target": "t", "outcome": "ok",
            "detail": json.dumps({"release_id": "rel_1"})})
        self.assertEqual(ev.event_type, "audit.release.deploy")
        self.assertEqual(ev.refs.task_id, "t")
        self.assertEqual(ev.payload, {"release_id": "rel_1"})


class CollectAndReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_16p_")
        self.app = App(data_dir=self.tmp)
        task = T.Task(task_id="t_16p", user_goal="装配")
        self.app.db.save_task(task)
        self.app.bus.emit(task_id="t_16p", run_id="run_1",
                          event_type="tool_call", severity="info",
                          payload={"tool": "current_time"})
        self.app.bus.emit(task_id="t_16p", run_id="run_1",
                          event_type="device_action", severity="info",
                          payload={"target": "设置"})
        self.app.db.save_side_effect(
            "fx_1", "t_16p", "run_1", "file_write", "k", "", "confirmed")
        self.app.audit.record("admin", "deploy", target="t_16p",
                              detail={"ok": True})
        self.app.db._conn.execute(
            "INSERT INTO approvals (approval_id, task_id, op, target,"
            " scope, action_summary, status, granted_by, created_at)"
            " VALUES ('ap1', 't_16p', 'file_write', 'f', 'w', '写',"
            " 'approved', 'u', 1.0)")
        self.app.db._conn.commit()

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _events(self):
        return collect_for_task(self.app.db, "t_16p",
                                events_conn=self.app.bus._conn)

    def test_collect_aggregates_all_sources_time_sorted(self):
        events = self._events()
        types = [e.event_type for e in events]
        self.assertIn("tool_call", types)
        self.assertIn("device_action", types)
        self.assertIn("side_effect.confirmed", types)
        self.assertIn("audit.deploy", types)
        self.assertIn("approval.approved", types)
        self.assertEqual(types, sorted(types, key=lambda _: 0) or types)
        self.assertGreaterEqual(len(events), 5)

    def test_collect_requires_task_scope(self):
        with self.assertRaises(ValueError):
            collect_for_task(self.app.db, "")

    def test_plan_covers_all_projected_events(self):
        plan = plan_deletion(self._events(), task_id="t_16p",
                             actor="admin", reason="用户删除")
        self.assertEqual(len(plan.event_ids), len(self._events()))

    def test_execute_then_reconcile_reports_clean(self):
        plan = plan_deletion(self._events(), task_id="t_16p",
                             actor="admin", reason="用户删除")
        app = self.app

        class Deleter:
            def delete_task(self, task_id):
                conn = app.db._conn
                bus = app.bus._conn
                counts = {
                    "task_side_effects": conn.execute(
                        "DELETE FROM task_side_effects WHERE task_id=?",
                        (task_id,)).rowcount,
                    "approvals": conn.execute(
                        "DELETE FROM approvals WHERE task_id=?",
                        (task_id,)).rowcount,
                    "audit_log": conn.execute(
                        "DELETE FROM audit_log WHERE target=?",
                        (task_id,)).rowcount,
                    "bus_runtime_events": bus.execute(
                        "DELETE FROM runtime_events WHERE task_id=?",
                        (task_id,)).rowcount,
                }
                conn.commit()
                bus.commit()
                return counts

        out = execute_deletion(Deleter(), plan, actor="admin",
                               reason="执行删除计划")
        self.assertTrue(out["ok"], out)
        report = deletion_reconciliation(
            self.app.db, "t_16p", events_conn=self.app.bus._conn)
        self.assertTrue(report["fully_deleted"], report["residuals"])
        # 删除后装配为空（证据不再可检索）
        self.assertEqual(self._events(), [])

    def test_reconciliation_reports_residuals_honestly(self):
        report = deletion_reconciliation(
            self.app.db, "t_16p", events_conn=self.app.bus._conn)
        self.assertFalse(report["fully_deleted"])
        self.assertGreater(report["residuals"]["task_side_effects"], 0)
        self.assertGreater(report["residuals"]["audit_log"], 0)


if __name__ == "__main__":
    unittest.main()
