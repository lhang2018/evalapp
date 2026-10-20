"""客户端事件同步与任务幂等提交（PRD 73-76）。"""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.events import EventBus, RunEvent


def make_event(event_id, **overrides):
    data = {
        "event_id": event_id,
        "event_type": "client_task_queued",
        "timestamp": time.time(),
        "payload": {"note": "ok"},
        "privacy_classification": "standard",
        "severity": "info",
    }
    data.update(overrides)
    return data


def eid(n):
    return f"evt_{n:016d}"


class TestEventBus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_events_")
        self.path = os.path.join(self.tmp, "events.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ingest_is_idempotent(self):
        bus = EventBus(self.path)
        evt = RunEvent(event_id=eid(1), event_type="client_note", task_id="t1")
        self.assertTrue(bus.ingest(evt))
        self.assertFalse(bus.ingest(evt))
        self.assertTrue(bus.exists(eid(1)))
        rows = bus.query(task_id="t1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].sync_status, "synced")
        bus._conn.close()

    def test_old_schema_migrated_and_defaults(self):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE runtime_events (event_id TEXT PRIMARY KEY, task_id TEXT,"
            " run_id TEXT, severity TEXT, event_type TEXT, component_id TEXT,"
            " timestamp REAL, payload TEXT)")
        conn.execute(
            "INSERT INTO runtime_events VALUES (?,?,?,?,?,?,?,?)",
            (eid(2), "t2", None, "info", "legacy", "system", time.time(),
             json.dumps({"old": True})))
        conn.commit()
        conn.close()

        bus = EventBus(self.path)
        rows = bus.query(task_id="t2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].component_version, "0.1.0")
        self.assertEqual(rows[0].privacy_classification, "standard")
        self.assertEqual(rows[0].sync_status, "local")
        self.assertEqual(rows[0].payload, {"old": True})

        evt = bus.emit(event_type="after_migrate", task_id="t2",
                       correlation_id="corr-1")
        self.assertEqual(evt.correlation_id, "corr-1")
        cols = {r[1] for r in
                bus._conn.execute("PRAGMA table_info(runtime_events)")}
        for name in ("component_version", "correlation_id", "causation_id",
                     "privacy_classification", "sync_status"):
            self.assertIn(name, cols)
        bus._conn.close()


class TestEventSyncApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_sync_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=18302)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:18302"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.app.db.close()
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

    def test_sync_then_duplicate(self):
        batch = {"client_id": "c1",
                 "events": [make_event(eid(10)), make_event(eid(11))]}
        st, out = self._post("/events/sync", batch)
        self.assertEqual(st, 200)
        self.assertEqual(sorted(out["accepted"]), [eid(10), eid(11)])
        self.assertEqual(out["duplicates"], [])

        st, out = self._post("/events/sync", batch)
        self.assertEqual(st, 200)
        self.assertEqual(out["accepted"], [])
        self.assertEqual(sorted(out["duplicates"]), [eid(10), eid(11)])
        stored = self.app.bus.query(task_id=None, limit=1000)
        matching = [e for e in stored if e.event_id in (eid(10), eid(11))]
        self.assertEqual(len(matching), 2)
        self.assertTrue(all(e.sync_status == "synced" for e in matching))

    def test_sensitive_event_rejected(self):
        st, out = self._post("/events/sync", {"events": [
            make_event(eid(12), privacy_classification="sensitive")]})
        self.assertEqual(st, 400)
        self.assertEqual(out["accepted"], [])
        self.assertEqual(len(out["rejected"]), 1)
        self.assertIn("不接收", out["rejected"][0]["error"])

    def test_partial_success_and_rejections(self):
        too_old = time.time() - 8 * 24 * 3600
        batch = {"events": [
            make_event(eid(13)),
            {"event_type": "no_id", "timestamp": time.time()},
            make_event(eid(14), timestamp=too_old),
            make_event(eid(15), payload={"blob": "x" * 20000}),
        ]}
        st, out = self._post("/events/sync", batch)
        self.assertEqual(st, 200)
        self.assertEqual(out["accepted"], [eid(13)])
        self.assertEqual(len(out["rejected"]), 3)

    def test_batch_level_rejections(self):
        st, out = self._post("/events/sync", {"events": []})
        self.assertEqual(st, 400)
        big = {"events": [make_event(eid(100 + i)) for i in range(201)]}
        st, out = self._post("/events/sync", big)
        self.assertEqual(st, 400)

    def test_task_idempotency(self):
        payload = {"goal": "现在几点了", "intent": "time_query",
                   "client_request_id": "req-abcdef-0001"}
        st1, out1 = self._post("/tasks", payload)
        st2, out2 = self._post("/tasks", payload)
        self.assertEqual(st1, 202)
        self.assertEqual(st2, 202)
        self.assertEqual(out1["task_id"], out2["task_id"])
        self.assertTrue(out2.get("deduplicated"))
        self.app.db.save_request("req-abcdef-0001", out1["task_id"])
        stored = self.app.db.get_request_task_id("req-abcdef-0001")
        self.assertEqual(stored, out1["task_id"])

    def test_concurrent_idempotency(self):
        payload = {"goal": "并发幂等任务", "intent": "time_query",
                   "client_request_id": "req-concurrent-0002"}
        results = []
        barrier = threading.Barrier(2)

        def submit():
            barrier.wait()
            results.append(self._post("/tasks", payload))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        ids = {out["task_id"] for _, out in results}
        self.assertEqual(len(ids), 1)
        count = sum(1 for t in self.app.db.list_tasks(limit=500)
                    if t.user_goal == "并发幂等任务")
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
