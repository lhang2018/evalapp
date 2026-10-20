"""任务运行回放（Arch 20.3）。

回放只读重建事件/轨迹/检查点/版本清单；非 standard 载荷脱敏；
不重新执行任何工具或外部副作用。
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


class TestReplayService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_replay_")
        self.app = App(data_dir=self.tmp)
        self.task = self.app.controller.submit_task(
            "计算 2+2", intent="computation", wait=True)

    def tearDown(self):
        self.app.db.close()
        self.app.memory.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_reconstructs_task(self):
        out = self.app.replay.build(self.task.task_id)
        self.assertTrue(out["read_only"])
        self.assertFalse(out["reexecute_side_effects"])
        self.assertEqual(out["task_status"], "SUCCEEDED")
        self.assertEqual(out["task_id"], self.task.task_id)
        self.assertGreater(out["stats"]["events"], 0)
        self.assertGreater(out["stats"]["traces"], 0)
        self.assertEqual(out["stats"]["timeline"], len(out["timeline"]))
        self.assertTrue(out["runs"])
        self.assertEqual(out["versions"]["model_route"]["model_id"],
                         "mock-1")
        times = [item["at"] for item in out["timeline"]]
        self.assertEqual(times, sorted(times))
        kinds = {item["kind"] for item in out["timeline"]}
        self.assertIn("task_created", kinds)
        self.assertIn("state_transition", kinds)

    def test_sensitive_payload_is_redacted(self):
        self.app.bus.emit(
            task_id=self.task.task_id, event_type="device_action",
            payload={"pin": "000000", "text": "secret target"},
            privacy_classification="sensitive")
        self.app.bus.emit(
            task_id=self.task.task_id, event_type="credential",
            payload={"token": "sk-should-not-appear"},
            privacy_classification="secret")
        out = self.app.replay.build(self.task.task_id)
        self.assertGreaterEqual(out["stats"]["redacted"], 2)
        entry = next(i for i in out["timeline"]
                     if i["kind"] == "device_action")
        self.assertTrue(entry["payload"]["redacted"])
        self.assertNotIn("000000", json.dumps(entry, ensure_ascii=False))
        secret = next(i for i in out["timeline"]
                      if i["kind"] == "credential")
        self.assertTrue(secret["payload"]["redacted"])
        self.assertNotIn("sk-should-not-appear",
                         json.dumps(out, ensure_ascii=False))
        self.assertTrue(any("脱敏" in w for w in out["warnings"]))

    def test_replay_does_not_reexecute_tools(self):
        before = self.app.db._conn.execute(
            "SELECT COUNT(*) FROM tool_executions").fetchone()[0]
        self.app.replay.build(self.task.task_id)
        self.app.replay.build(self.task.task_id)
        after = self.app.db._conn.execute(
            "SELECT COUNT(*) FROM tool_executions").fetchone()[0]
        self.assertEqual(before, after)

    def test_unknown_task_raises(self):
        with self.assertRaises(KeyError):
            self.app.replay.build("no-such-task")


class TestReplayApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_replay_api_")
        cls.app = App(data_dir=cls.tmp)
        cls.task = cls.app.controller.submit_task(
            "现在几点了", intent="time_query", wait=True)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=18306)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:18306"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.app.db.close()
        cls.app.memory.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def test_replay_endpoint(self):
        st, out = self._get(f"/tasks/{self.task.task_id}/replay")
        self.assertEqual(st, 200)
        self.assertTrue(out["read_only"])
        self.assertEqual(out["task_id"], self.task.task_id)
        self.assertTrue(out["timeline"])

    def test_unknown_task_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/tasks/missing-task/replay")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
