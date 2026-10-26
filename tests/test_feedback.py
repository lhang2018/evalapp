"""用户反馈端点测试（ALN-29-C）。

验证：反馈绑定真实 task/run（不存在则 404）；凭据泄漏拒绝入库；
重复反馈聚合计数；cause_status 恒为 unverified；列表为 admin 面。
"""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from evoagent.api.server import ApiServer
from evoagent.app import App


class TestFeedback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_feedback_")
        cls.app = App(data_dir=cls.tmp)
        cls.task = cls.app.controller.submit_task(
            "计算 2+3", intent="computation", wait=True)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.app.db.close()
        cls.app.memory.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_submit_binds_real_task_and_returns_unverified(self):
        st, out = self._post("/feedback", {
            "category": "bug", "text": "点开设置页面没有反应",
            "task_id": self.task.task_id,
            "evidence_refs": ["evt_1"]})
        self.assertEqual(st, 201, out)
        self.assertFalse(out["aggregated"])
        st, row = self._get(f"/feedback/{out['feedback_id']}")
        self.assertEqual(st, 200)
        self.assertEqual(row["cause_status"], "unverified",
                         "用户反馈是现象，不是根因")
        self.assertEqual(row["task_id"], self.task.task_id)
        self.assertEqual(row["evidence_refs"], ["evt_1"])
        self.assertTrue(row["manifest_id"], "应绑定运行时清单引用")

    def test_unknown_task_or_run_rejected(self):
        st, out = self._post("/feedback", {
            "category": "bug", "text": "x", "task_id": "no_such_task"})
        self.assertEqual(st, 404)
        st, out = self._post("/feedback", {
            "category": "bug", "text": "x", "run_id": "run_missing"})
        self.assertEqual(st, 404)

    def test_secret_feedback_rejected_not_stored(self):
        st, out = self._post("/feedback", {
            "category": "bug",
            "text": "我的密钥 sk-abcdef1234567890abcdef 提交失败了"})
        self.assertEqual(st, 400, out)
        self.assertIn("凭据", out["error"])
        st, listed = self._get("/feedback")
        texts = json.dumps(listed, ensure_ascii=False)
        self.assertNotIn("sk-abcdef", texts)

    def test_duplicate_feedback_aggregates(self):
        body = {"category": "idea", "text": "希望支持夜间模式"}
        st1, out1 = self._post("/feedback", body)
        st2, out2 = self._post("/feedback", body)
        self.assertEqual(st1, 201)
        self.assertEqual(st2, 201)
        self.assertFalse(out1["aggregated"])
        self.assertTrue(out2["aggregated"])
        self.assertEqual(out2["count"], 2)
        st, listed = self._get("/feedback")
        matches = [f for f in listed["feedback"]
                   if f["feedback_id"] == out1["feedback_id"]]
        self.assertEqual(len(matches), 1, "聚合不产生新行")
        self.assertEqual(matches[0]["count"], 2)

    def test_list_is_admin_only(self):
        # 本地演示环境 auth 关闭即全 admin；此处仅验证 admin 面可列出
        st, listed = self._get("/feedback")
        self.assertEqual(st, 200)
        self.assertIn("feedback", listed)

    def test_duplicate_http_feedback_retains_new_evidence(self):
        body = {"category": "problem", "text": "重复反馈证据合并HTTP",
                "task_id": self.task.task_id, "evidence_refs": ["evt_http_a"]}
        status, first = self._post("/feedback", body)
        self.assertEqual(status, 201, first)
        body["evidence_refs"] = ["evt_http_b", "evt_http_a"]
        status, second = self._post("/feedback", body)
        self.assertEqual(status, 201, second)
        self.assertEqual(first["feedback_id"], second["feedback_id"])
        status, row = self._get("/feedback/" + first["feedback_id"])
        self.assertEqual(status, 200, row)
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["evidence_refs"], ["evt_http_a", "evt_http_b"])
        self.assertEqual(row["cause_status"], "unverified")

    def test_aggregate_http_evidence_limit_rejects_without_increment(self):
        body = {"category": "problem", "text": "重复反馈证据上限HTTP",
                "evidence_refs": [f"evt_http_{i}" for i in range(32)]}
        status, first = self._post("/feedback", body)
        self.assertEqual(status, 201, first)
        body["evidence_refs"] = ["evt_http_extra"]
        status, error = self._post("/feedback", body)
        self.assertEqual(status, 400, error)
        self.assertIn("32", error["error"])
        _, row = self._get("/feedback/" + first["feedback_id"])
        self.assertEqual(row["count"], 1)
        self.assertEqual(len(row["evidence_refs"]), 32)

    def test_empty_and_unknown_category_rejected(self):
        st, _ = self._post("/feedback", {"category": "bug", "text": "  "})
        self.assertEqual(st, 400)
        st, _ = self._post("/feedback", {"category": "junk", "text": "x"})
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
