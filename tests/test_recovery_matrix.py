# -*- coding: utf-8 -*-
"""ALN-04-V：组合状态与生产身份恢复矩阵。

验收对照：
- 旧授权不跨身份复用：授权绑定提交 principal，其他身份消费一律拒绝；
- 未知在途保留：崩溃接管把 planned/submitted 转 unknown 且不吞、
  预绑定意图（07-C）与等待状态（问答/审批）在重启后保留；
- 覆盖检查点、重启与组合资产：同库新实例（模拟重启）后身份/授权/
  等待状态完整可恢复。
"""

import shutil
import tempfile
import unittest

import evoagent.core.task as T
from evoagent.app import App
from evoagent.core.authorization import AuthorizationStore, check_policy
from evoagent.tools.contract import ToolContract
from evoagent.core.leases import CURRENT_LEASE
from evoagent.core.persistence import Database


def contract(tool="calc_v", side_effect="none"):
    return ToolContract(tool, input_schema={"type": "object"},
                        side_effect_level=side_effect)


class PrincipalBindingTest(unittest.TestCase):
    """授权 × 身份矩阵：同身份放行 / 跨身份拒绝 / legacy 兼容。"""

    def setUp(self):
        self.db = Database(":memory:")
        self.store = AuthorizationStore(self.db)

    def tearDown(self):
        self.db.close()

    def _issue(self, principal=None):
        return self.store.issue(
            "t_p", {"tools": ["calc_v"], "permissions": []},
            actor="test", principal=principal)

    def test_same_principal_consumes(self):
        self._issue(principal="user-a")
        ref = self.store.check("t_p", contract(), {}, principal="user-a")
        self.assertTrue(ref.startswith("auth_"))

    def test_cross_principal_rejected(self):
        self._issue(principal="user-a")
        with self.assertRaises(PermissionError) as ctx:
            self.store.check("t_p", contract(), {}, principal="user-b")
        self.assertIn("拒绝以 user-b 复用", str(ctx.exception))

    def test_bound_grant_rejects_anonymous_consumer(self):
        """绑定了身份的授权，匿名（不带 principal）请求同样拒绝。"""
        self._issue(principal="user-a")
        with self.assertRaises(PermissionError):
            self.store.check("t_p", contract(), {}, principal=None)

    def test_legacy_grant_without_principal_still_works(self):
        """旧记录（未绑定身份）保持兼容：任何调用方可消费。"""
        self._issue(principal=None)
        self.store.check("t_p", contract(), {}, principal="user-b")

    def test_rebind_on_new_identity_issues_new_grant(self):
        """身份切换后重新授权产生新授权；旧授权不复用。"""
        first = self._issue(principal="user-a")
        second = self.store.issue(
            "t_p", {"tools": ["calc_v"], "permissions": []},
            actor="test", principal="user-b")
        self.assertNotEqual(first, second)
        # current() 取最新——user-b 生效
        ref = self.store.check("t_p", contract(), {}, principal="user-b")
        self.assertEqual(ref, second)
        with self.assertRaises(PermissionError):
            self.store.check("t_p", contract(), {}, principal="user-a")

    def test_normalize_rejects_invalid_principal(self):
        with self.assertRaises(ValueError):
            self.store.issue("t_p", {"tools": ["x"], "permissions": [],
                                     "principal": 123}, actor="t")


class CombinationRecoveryMatrix(unittest.TestCase):
    """组合等待 + 未知在途 + 重启保留矩阵。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_04v_")
        self.db_path = self.tmp + "/state.db"
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, task_id="t_comb", principal=None, status=None):
        task = T.Task(task_id=task_id, user_goal="组合恢复")
        if principal:
            task.runtime_manifest["principal_id"] = principal
        task.task_status = status or T.TaskStatus.RUNNING
        self.app.db.save_task(task)
        return task

    def test_unknown_inflight_effects_survive_takeover(self):
        """崩溃接管：planned 副作用转 unknown 保留，不静默清除。"""
        task = self._task(status=T.TaskStatus.CREATED)
        rid, _ = self.app.controller.runs.begin(
            self.app.db.get_task(task.task_id), {})
        self.app.db.save_side_effect(
            "fx_p", task.task_id, rid, "file_write", "k1", "", "planned")
        token, reset, stop, thread = \
            self.app.controller.runs._handles.pop(rid)
        stop.set(); thread.join(2); CURRENT_LEASE.reset(reset)
        self.app.db._conn.execute(
            "UPDATE run_leases SET expires_at=0 WHERE task_id=?",
            (task.task_id,))
        self.app.db._conn.commit()
        task.task_status = T.TaskStatus.RUNNING
        self.app.db.save_task(self.app.db.get_task(task.task_id))
        # 07-C 意图同窗口登记，崩溃后仍在
        self.app.controller.react.bindings.record_intent(
            task.task_id, "s1", "file_write")
        out = self.app.controller.recover_task(task.task_id)
        # leases 层拦截未决副作用 → SUSPENDED（不是吞掉）
        self.assertEqual(out.task_status.value, "SUSPENDED")
        rows = self.app.controller.side_fx.history(task.task_id)
        self.assertEqual([r["status"] for r in rows], ["unknown"],
                         "planned 必须转为 unknown 保留在账本")
        pending = self.app.controller.react.bindings.pending_intents(
            task.task_id)
        self.assertEqual(len(pending), 1, "预绑定意图必须保留")

    def test_waiting_request_and_approvals_preserved_across_restart(self):
        """组合等待（问答+审批同时在途）跨重启保留。"""
        task = self._task(status=T.TaskStatus.WAITING_USER)
        task.waiting_request = {"request_id": "req_1", "phase": "execution",
                                "question": "选哪个？", "answered": False}
        task.waiting_approvals = ["appr_1"]
        self.app.db.save_task(task)
        approval = {"approval_id": "appr_1", "tool": "file_write",
                    "args": {}, "risk": "high", "status": "pending",
                    "task_id": task.task_id}
        self.app.db._conn.execute(
            "INSERT INTO approvals (approval_id, task_id, op, target,"
            " scope, action_summary, status, granted_by, created_at)"
            " VALUES ('appr_1', ?, 'file_write', 'f.txt', 'write',"
            " '写文件', 'pending', 'user', 0.0)",
            (task.task_id,))
        self.app.db._conn.commit()
        # 重启：同库新实例
        self.app.db.close()
        self.app = App(data_dir=self.tmp)
        restored = self.app.db.get_task(task.task_id)
        self.assertEqual(restored.task_status.value, "WAITING_USER")
        self.assertEqual(restored.waiting_request["request_id"], "req_1")
        self.assertEqual(restored.waiting_approvals, ["appr_1"])
        rows = self.app.db._conn.execute(
            "SELECT approval_id, status FROM approvals"
            " WHERE task_id=?", (task.task_id,)).fetchall()
        self.assertEqual([r["approval_id"] for r in rows], ["appr_1"])

    def test_restart_preserves_bound_principal_and_grant(self):
        """重启后授权身份绑定仍在，跨身份继续被拒。"""
        task = self._task(principal="user-a")
        self.app.tools.authorizations.issue(
            task.task_id, {"tools": ["current_time"], "permissions": []},
            actor="test", principal="user-a")
        self.app.db.close()
        self.app = App(data_dir=self.tmp)
        with self.assertRaises(PermissionError):
            self.app.tools.authorizations.check(
                task.task_id, contract("current_time"), {},
                principal="user-b")
        ref = self.app.tools.authorizations.check(
            task.task_id, contract("current_time"), {}, principal="user-a")
        self.assertTrue(ref.startswith("auth_"))

    def test_submit_records_principal_into_manifest(self):
        task = self.app.controller.submit_task(
            "查询时间", intent="time_query", principal_id="api:operator")
        self.assertEqual(
            (task.runtime_manifest or {}).get("principal_id"), "api:operator")


if __name__ == "__main__":
    unittest.main()
