"""REST API 冒烟测试（Android 客户端依赖的接口契约）。"""

import shutil
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import json

from evoagent.app import App
from evoagent.api.server import ApiServer


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_api_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=18300)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:18300"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    def _post(self, path, payload):
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def test_health(self):
        out = self._get("/health")
        self.assertEqual(out["status"], "ok")
        self.assertIn("general", out["experts"])

    def test_task_lifecycle_via_api(self):
        st, out = self._post("/tasks", {"goal": "现在几点了",
                                        "intent": "time_query",
                                        "risk_level": "low"})
        self.assertEqual(st, 202)
        tid = out["task_id"]
        import time
        for _ in range(100):
            t = self._get(f"/tasks/{tid}")
            if t["task_status"] in ("SUCCEEDED", "FAILED"):
                break
            time.sleep(0.1)
        self.assertEqual(t["task_status"], "SUCCEEDED")
        events = self._get(f"/tasks/{tid}/events")
        types = [e["event_type"] for e in events["events"]]
        self.assertIn("state_transition", types)
        # 任务列表
        tasks = self._get("/tasks")["tasks"]
        self.assertTrue(any(t["task_id"] == tid for t in tasks))

    def test_costs_endpoint(self):
        out = self._get("/costs?limit=10")
        self.assertIn("totals", out)
        self.assertIn("by_task", out)
        self.assertIn("by_domain", out)
        self.assertIn("by_model", out)
        self.assertIn("evolution", out)
        self.assertIn("cost_per_success_including_waste", out["totals"])

    def test_invalid_risk_rejected(self):
        try:
            self._post("/tasks", {"goal": "x", "risk_level": "boom"})
            self.fail("应返回 400 被拒")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            body = json.loads(e.read().decode())
            self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
