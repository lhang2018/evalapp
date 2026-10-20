"""工作流候选 REST 接口（Arch 16.2）（PRD 13.4 / 89）。"""

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.task import Task, TaskStatus


class TestWorkflowApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_wfapi_")
        cls.app = App(data_dir=cls.tmp)
        cls._seed(cls.app, "calculate", "calc_a")
        cls._seed(cls.app, "calculate", "calc_b")
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _seed(cls, app, tool, task_id):
        task = Task(task_id=task_id, user_goal="计算", intent="computation",
                    task_status=TaskStatus.SUCCEEDED)
        task.result_evidence = [{"budget": {"usage": {"tool_calls": 1}}}]
        app.db.save_task(task)
        app.bus.emit(event_type="tool_call", task_id=task_id,
                     timestamp=time.time() + len(task_id),
                     payload={"tool": tool, "ok": True})

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    def _post(self, path, payload=None):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def _post_status(self, path, payload=None):
        try:
            return self._post(path, payload)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_full_candidate_lifecycle(self):
        st, out = self._post("/workflows/extract")
        self.assertEqual(st, 200)
        self.assertEqual(len(out["created"]), 1)
        cand = out["created"][0]
        cid = cand["candidate_id"]
        self.assertEqual(cand["signature"], ["calculate"])

        rows = self._get("/workflows/candidates?status=candidate")["candidates"]
        self.assertIn(cid, [r["candidate_id"] for r in rows])
        detail = self._get(f"/workflows/candidates/{cid}")
        self.assertEqual(detail["support"], 2)

        st, err = self._post_status(f"/workflows/candidates/{cid}/publish",
                                    {"args_by_step": {"step_1": {
                                        "expression": "3+4"}}})
        self.assertEqual(st, 409)

        st, approved = self._post(
            f"/workflows/candidates/{cid}/approve", {"reason": "ok"})
        self.assertEqual(st, 200)
        self.assertEqual(approved["status"], "approved")

        st, err = self._post_status(f"/workflows/candidates/{cid}/publish")
        self.assertEqual(st, 409)
        self.assertIn("未绑定", err["error"])

        st, published = self._post(
            f"/workflows/candidates/{cid}/publish",
            {"reason": "验证通过",
             "args_by_step": {"step_1": {"expression": "3+4"}}})
        self.assertEqual(st, 200)
        self.assertEqual(published["status"], "published")
        wf = self._get(f"/workflows/{published['workflow_id']}")["workflow"]
        self.assertEqual(wf["nodes"][0]["args"], {"expression": "3+4"})

    def test_reject_and_unknown_candidate(self):
        self._seed(self.app, "current_time", "time_a")
        self._seed(self.app, "current_time", "time_b")
        st, out = self._post("/workflows/extract")
        self.assertEqual(st, 200)
        cid = out["created"][0]["candidate_id"]
        st, rejected = self._post(f"/workflows/candidates/{cid}/reject",
                                  {"reason": "证据不足"})
        self.assertEqual(st, 200)
        self.assertEqual(rejected["status"], "rejected")
        st, again = self._post_status(f"/workflows/candidates/{cid}/reject")
        self.assertEqual(st, 409)

        st, err = self._post_status("/workflows/candidates/nope/approve")
        self.assertEqual(st, 404)
        st, err = self._post_status(
            f"/workflows/candidates/{cid}/explode")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
