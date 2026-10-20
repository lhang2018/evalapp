"""环境漂移检测（PRD 44 / Arch 9.3, 9.5）。

验证窗口对比的信号、处置建议、事件发射与指标/告警/API 接线。
"""

import json
import shutil
import tempfile
import threading
import time
import types
import unittest
import urllib.request

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.events import EventBus
from evoagent.evolution.drift import (ACTION_COMPAT, ACTION_EVOLUTION,
                                      ACTION_HEALTH, ACTION_STOP,
                                      DriftDetector, DriftPolicy)


class FakeDB:
    def __init__(self, tasks):
        self.tasks = list(tasks)

    def list_tasks(self, limit=1000):
        return self.tasks[:limit]


def fake_task(intent, created_at):
    return types.SimpleNamespace(intent=intent, created_at=created_at)


class DriftTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_drift_")
        self.bus = EventBus(f"{self.tmp}/events.db")
        self.now = time.time()
        self.detector = DriftDetector(self.bus, policy=DriftPolicy(
            window_seconds=3600, baseline_window_seconds=3600))

    def tearDown(self):
        self.bus._conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def emit_in_window(self, baseline, event_type, payload, **kwargs):
        offset = -5400 if baseline else -600
        return self.bus.emit(event_type=event_type, payload=payload,
                             timestamp=self.now + offset, **kwargs)

    def detect(self):
        return self.detector.detect(now=self.now)


class TestDriftSignals(DriftTestBase):
    def test_tool_success_drop_recommends_health_check(self):
        for _ in range(6):
            self.emit_in_window(True, "tool_call",
                                {"tool": "calculate", "ok": True})
        for i in range(6):
            self.emit_in_window(False, "tool_call", {
                "tool": "calculate", "ok": i < 3,
                "error_class": "timeout"})
        out = self.detect()
        self.assertEqual(out["status"], "drifting")
        kinds = [s["kind"] for s in out["signals"]]
        self.assertIn("tool_success_rate", kinds)
        sig = next(s for s in out["signals"]
                   if s["kind"] == "tool_success_rate")
        self.assertEqual(sig["severity"], "critical")
        self.assertEqual(sig["recommended_action"], ACTION_HEALTH)
        self.assertAlmostEqual(sig["delta"], -0.5, places=4)

    def test_contract_error_increase_recommends_stop(self):
        for _ in range(10):
            self.emit_in_window(True, "tool_call",
                                {"tool": "calculate", "ok": True})
        for i in range(10):
            self.emit_in_window(False, "tool_call", {
                "tool": "calculate", "ok": i != 0,
                "error_class": "invalid_arguments" if i == 0 else None})
        out = self.detect()
        sig = next(s for s in out["signals"]
                   if s["kind"] == "interface_contract")
        self.assertEqual(sig["severity"], "critical")
        self.assertEqual(sig["recommended_action"], ACTION_STOP)
        self.assertAlmostEqual(sig["current"], 0.1, places=4)

    def test_model_drift_success_and_latency(self):
        for _ in range(6):
            self.emit_in_window(True, "model_call", {
                "model_id": "m1", "ok": True, "latency_s": 1.0})
        for i in range(6):
            self.emit_in_window(False, "model_call", {
                "model_id": "m1", "ok": i < 4, "latency_s": 3.0})
        out = self.detect()
        sig = next(s for s in out["signals"]
                   if s["kind"] == "model_behavior")
        self.assertEqual(sig["recommended_action"], ACTION_EVOLUTION)
        self.assertGreaterEqual(sig["latency_ratio"], 2.9)

    def test_task_distribution_shift(self):
        tasks = ([fake_task("time_query", self.now - 5000) for _ in range(5)]
                 + [fake_task("computation", self.now - 1000)
                    for _ in range(5)])
        detector = DriftDetector(self.bus, db=FakeDB(tasks),
                                 policy=DriftPolicy())
        out = detector.detect(now=self.now)
        sig = next(s for s in out["signals"]
                   if s["kind"] == "task_distribution")
        self.assertEqual(sig["recommended_action"], ACTION_COMPAT)
        self.assertAlmostEqual(sig["delta"], 1.0, places=4)

    def test_component_and_dependency_version_changes(self):
        self.emit_in_window(True, "info", {}, component_id="react",
                            component_version="1.0.0")
        self.emit_in_window(False, "info", {}, component_id="react",
                            component_version="2.0.0")
        self.emit_in_window(
            True, "task_created", {"dependencies": {"libx": "1.0"}})
        self.emit_in_window(
            False, "task_created", {"dependencies": {"libx": "2.0"}})
        out = self.detect()
        kinds = {s["kind"]: s for s in out["signals"]}
        self.assertIn("component_version", kinds)
        self.assertEqual(kinds["component_version"]["recommended_action"],
                         ACTION_COMPAT)
        self.assertIn("dependency_version", kinds)
        self.assertEqual(kinds["dependency_version"]["recommended_action"],
                         ACTION_STOP)

    def test_insufficient_data(self):
        out = self.detect()
        self.assertEqual(out["status"], "insufficient_data")
        self.assertEqual(out["signal_count"], 0)

    def test_emit_only_when_requested(self):
        for _ in range(5):
            self.emit_in_window(True, "tool_call",
                                {"tool": "t", "ok": True})
        for _ in range(5):
            self.emit_in_window(False, "tool_call",
                                {"tool": "t", "ok": False})
        self.detect()
        before = [e for e in self.bus.query(limit=100)
                  if e.event_type == "drift_detected"]
        self.assertEqual(before, [])
        self.detect()
        out = self.detector.detect(now=self.now, emit=True)
        self.assertTrue(out["signals"])
        events = [e for e in self.bus.query(limit=100)
                  if e.event_type == "drift_detected"]
        self.assertEqual(len(events), 1)
        self.assertIn("kinds", events[0].payload)

    def test_recommended_actions_ordered_by_priority(self):
        for _ in range(10):
            self.emit_in_window(True, "tool_call",
                                {"tool": "t", "ok": True})
            self.emit_in_window(True, "model_call", {
                "model_id": "m1", "ok": True, "latency_s": 1.0})
        for i in range(10):
            self.emit_in_window(False, "tool_call", {
                "tool": "t", "ok": i != 0,
                "error_class": "invalid_arguments" if i == 0 else None})
            self.emit_in_window(False, "model_call", {
                "model_id": "m1", "ok": i < 5, "latency_s": 2.0})
        actions = self.detect()["recommended_actions"]
        self.assertEqual(actions[0], ACTION_STOP)
        self.assertIn(ACTION_EVOLUTION, actions)


class TestDriftApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_drift_api_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=18303)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:18303"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.app.db.close()
        cls.app.memory.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def test_drift_endpoints_metrics_and_alerts(self):
        now = time.time()
        for _ in range(6):
            self.app.bus.emit(event_type="tool_call", timestamp=now - 5400,
                              payload={"tool": "calculate", "ok": True})
        for _ in range(6):
            self.app.bus.emit(event_type="tool_call", timestamp=now - 600,
                              payload={"tool": "calculate", "ok": False,
                                       "error_class": "timeout"})

        report = self._get("/diagnostics/drift")
        self.assertEqual(report["status"], "drifting")
        self.assertIn("tool_success_rate",
                      [s["kind"] for s in report["signals"]])

        snap = self.app.metrics.snapshot()
        self.assertEqual(snap["drift"]["status"], "drifting")
        rules = {a["rule"] for a in snap["alerts"]}
        self.assertIn("environment_drift", rules)
        prom = self.app.metrics.prometheus()
        self.assertIn("evoagent_drift_detected 1", prom)
        self.assertIn('evoagent_drift_signals_by_kind{kind="tool_success_rate"}',
                      prom)

        st, scanned = self._post("/diagnostics/drift/scan", {})
        self.assertEqual(st, 200)
        self.assertEqual(scanned["status"], "drifting")
        emitted = [e for e in self.app.bus.query(limit=1000)
                   if e.event_type == "drift_detected"]
        self.assertTrue(emitted)


if __name__ == "__main__":
    unittest.main()
