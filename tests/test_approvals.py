# -*- coding: utf-8 -*-
"""F-04 审批记录（A-01 五项绑定 + 09 卷 §1 approval_id）。

验证面：
1) 实体：五项绑定齐备、approval_id 可产生
2) 一次性语义：授即用、二拒、revoke 后不可再授（消费）
3) 决策收口：_decide_high_risk 三路径（confirm 允/拒、deny/policy）全落账
4) resume force 明示授权落账（与 F-03 对账链相接）
5) API：注册表路由 /approvals（admin 200、非 admin 403）
"""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.request

from evoagent.api import routes
from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.permissions import PermissionController, \
    PermissionDecision
from evoagent.core.task import Task, TaskStatus


class TestApprovalEntity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_f04_")
        self.app = App(data_dir=self.tmp)
        self.store = self.app.approvals

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_five_bindings_recorded(self):
        aid = self.store.grant(
            "pay", "pay 89.0 ¥ 至 target=shop_42",
            target="shop_42", scope="op:pay", task_id="task_A",
            granted_by="user:console", ttl_seconds=60)
        row = self.store.get(aid)
        self.assertEqual(row["op"], "pay")
        self.assertEqual(row["action_summary"], "pay 89.0 ¥ 至 target=shop_42")
        self.assertEqual(row["target"], "shop_42")
        self.assertEqual(row["scope"], "op:pay")
        self.assertEqual(row["task_id"], "task_A")
        self.assertIsNotNone(row["expires_at"])   # ④ 有效期
        self.assertEqual(row["status"], "approved")
        self.assertTrue(row["approval_id"].startswith("apr_"))

    def test_one_shot_semantics(self):
        aid = self.store.grant("pay", "pay once", target="t",
                               scope="op:pay", task_id="task_A",
                               granted_by="user")
        self.assertTrue(self.store.consume(aid))      # 首次消费成功
        self.assertEqual(self.store.get(aid)["status"], "used")
        self.assertFalse(self.store.consume(aid))     # ②次拒绝：一次性
        revoked_from_used = self.store.revoke(aid)
        self.assertFalse(revoked_from_used)           # used 不可再吊销

    def test_revoke_blocks_consume(self):
        aid = self.store.grant("deploy_release", "dep", target="v1",
                               scope="op:deploy_release",
                               granted_by="user")
        self.assertTrue(self.store.revoke(aid))
        self.assertEqual(self.store.get(aid)["status"], "revoked")
        self.assertFalse(self.store.consume(aid))

    def test_expiry(self):
        aid = self.store.grant("pay", "short ttl", target="t",
                               scope="op:pay", granted_by="user",
                               ttl_seconds=-1)  # 立即过期
        self.assertEqual(self.store.get(aid)["status"], "expired")


class TestDecisionChokePoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_f04_dec_")
        self.app = App(data_dir=self.tmp)
        self.perm = self.app.permissions
        self.store = self.app.approvals

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_confirm_allow_records_used(self):
        self.app.config.high_risk_mode = "confirm"
        perm = PermissionController(
            high_risk_mode="confirm",
            confirm_callback=lambda op, args: True,
            approvals=self.store)
        decision = perm.check_op("pay", {"target": "shop_1", "amount": 9})
        self.assertEqual(decision, PermissionDecision.ALLOW)
        rows = self.store.list_recent()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # A-01 五项：动作摘要含参数、目标对象、范围、一次性（已消费）
        self.assertIn("amount", row["action_summary"])
        self.assertEqual(row["target"], "shop_1")
        self.assertEqual(row["scope"], "pay")
        self.assertEqual(row["status"], "used")
        self.assertEqual(row["granted_by"], "policy:confirm")

    def test_confirm_deny_records_denied(self):
        perm = PermissionController(
            high_risk_mode="confirm",
            confirm_callback=lambda op, args: False,
            approvals=self.store)
        dec = perm.check_op("delete", {"target": "note_3"})
        self.assertEqual(dec, PermissionDecision.DENY)
        row = self.store.list_recent()[0]
        self.assertEqual(row["status"], "denied")
        self.assertEqual(row["target"], "note_3")

    def test_tool_high_risk_records(self):
        perm = PermissionController(
            high_risk_mode="confirm",
            confirm_callback=lambda op, args: True,
            approvals=self.store)
        perm.check_tool("pay", {}, risk=__import__(
            "evoagent.core.task", fromlist=["RiskLevel"]).RiskLevel.HIGH,
            side_effect="external")
        rows = self.store.list_recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["op"], "pay")

    def test_decision_without_store_backward_compat(self):
        perm = PermissionController(
            high_risk_mode="confirm",
            confirm_callback=lambda op, args: True,
            approvals=None)
        self.assertEqual(perm.check_op("pay", {"target": "x"}),
                         PermissionDecision.ALLOW)


class TestResumeForceApproval(unittest.TestCase):
    """resume force 产生审批记录（GAP：resume 校验 safe_to_repeat 链）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_f04_res_")
        self.app = App(data_dir=self.tmp)
        # 悬挂任务 + 未决副作用（账本投影，F-03）
        t = Task(task_id="task_f04_r", user_goal="g")
        t.task_status = TaskStatus.SUSPENDED
        self.app.db.save_task(t)
        self.ledger = self.app.controller.side_fx
        self.ledger.record("task_f04_r", "http_request", status="unknown")

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_force_records_approval(self):
        resumed = self.app.controller.resume_task("task_f04_r", force=True)
        self.assertIsNotNone(resumed)
        rows = self.app.approvals.list_for_task("task_f04_r")
        self.assertTrue(rows)
        row = rows[0]
        self.assertEqual(row["op"], "resume")
        self.assertEqual(row["target"], "task_f04_r")
        self.assertEqual(row["granted_by"], "api:force")
        self.assertEqual(row["status"], "used")

    def test_without_force_no_record(self):
        self.assertRaises(PermissionError,
                          self.app.controller.resume_task, "task_f04_r")
        self.assertEqual(self.app.approvals.list_for_task("task_f04_r"), [])


class TestApprovalsEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_f04_api_")
        cls.app = App(data_dir=cls.tmp)
        cls.store = cls.app.approvals
        cls.store.grant("pay", "api demo", target="x",
                        scope="op:pay", task_id="task_api_1",
                        granted_by="user")
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=0)
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.app.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path, token=None):
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_admin_can_list(self):
        app = self._get("/approvals", token=None)
        # 无 admin token 配置时所有人即 admin（默认口径由 ApiAuth 决定）
        self.assertEqual(app[0], 200)
        self.assertTrue(app[1]["approvals"])

    def test_error_shape_contract(self):
        # 注入 admin token 后无凭据请求应 401/403 且回七字段错误对象
        self.app.config.api_token = "op-token"
        self.app.config.api_admin_token = "adm-token"
        try:
            st, out = self._get("/approvals", token="op-token")
            self.assertEqual(st, 403)
            for f in ("error_code", "required_action", "safe_to_repeat"):
                self.assertIn(f, out)
        finally:
            self.app.config.api_token = None
            self.app.config.api_admin_token = None


if __name__ == "__main__":
    unittest.main()
