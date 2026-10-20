"""日志分层、保留策略、指标补全与告警测试（PRD 37-40 / Arch 9.2, 20.1-20.2）。"""

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
from evoagent.core.alerts import AlertPolicy, evaluate_alerts
from evoagent.core.log_layers import (LAYER_POLICIES, redact_payload,
                                      role_can_read)
from evoagent.core.task import Task, TaskStatus
from evoagent.evolution.candidates import CandidateStatus


class TestLayerPolicies(unittest.TestCase):
    def test_four_layers_with_roles(self):
        self.assertEqual(set(LAYER_POLICIES), {
            "runtime_event", "execution_trace",
            "diagnostic_evidence", "audit"})
        self.assertTrue(role_can_read("runtime_event", "operator"))
        self.assertTrue(role_can_read("execution_trace", "operator"))
        self.assertFalse(role_can_read("diagnostic_evidence", "operator"))
        self.assertFalse(role_can_read("audit", "operator"))
        self.assertTrue(role_can_read("audit", "admin"))

    def test_redact_non_standard_payload(self):
        out = redact_payload("execution_trace", "sensitive",
                             {"tool": "file_read", "content": "secret"})
        self.assertTrue(out["redacted"])
        self.assertNotIn("content", out)
        self.assertEqual(
            redact_payload("execution_trace", "standard",
                           {"tool": "file_read"}), {"tool": "file_read"})


class TestLogLayers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_logs_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tracer_records_execution_steps(self):
        task = self.app.submit("现在几点了", intent="time_query",
                               acceptance=[{"type": "tool_succeeded"}])
        traces = self.app.db.list_traces(task_id=task.task_id)
        kinds = [t["kind"] for t in traces]
        self.assertIn("task_created", kinds)
        self.assertIn("tool_call", kinds)
        self.assertIn("task_finalized", kinds)
        self.assertEqual([t["step"] for t in traces],
                         sorted(t["step"] for t in traces))
        tool = next(t for t in traces if t["kind"] == "tool_call")
        self.assertIn("ok=", tool["summary"])

    def test_sensitive_trace_is_redacted(self):
        self.app.bus.emit(event_type="tool_call", task_id="task_x",
                          privacy_classification="sensitive",
                          payload={"tool": "screen", "content": "private"})
        traces = self.app.db.list_traces(task_id="task_x")
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["summary"], "")
        self.assertTrue(traces[0]["payload"]["redacted"])
        self.assertNotIn("content", traces[0]["payload"])

    def test_evidence_export_requires_authorization(self):
        ev = self.app.evidence
        plain = ev.add("t1", "tool_output", "ref://1", "普通证据")
        sensitive = ev.add("t1", "screen", "ref://2", "界面证据",
                           privacy="sensitive", authorized=True)
        allowed = ev.add("t1", "file", "ref://3", "授权文件",
                         authorized=True)
        rows = {r["evidence_id"]: r for r in ev.list("t1")}
        self.assertFalse(ev.exportable(rows[plain]))
        self.assertFalse(ev.exportable(rows[sensitive]))
        self.assertTrue(ev.exportable(rows[allowed]))
        with self.assertRaises(ValueError):
            ev.add("t1", "screen", "ref://4", "x", privacy="secret")

    def test_purge_respects_retention(self):
        now = time.time()
        old = now - 40 * 86400
        # 用非执行轨迹事件，避免 tracer 额外写入干扰保留计数
        self.app.bus.emit(event_type="log_probe", task_id="old",
                          timestamp=old)
        self.app.bus.emit(event_type="log_probe", task_id="new")
        self.app.db.save_trace("trc_old", "old", None, 1, old, "task_created",
                               "old", "{}", "standard")
        self.app.db.save_trace("trc_new", "new", None, 1, now, "task_created",
                               "new", "{}", "standard")
        self.app.db.save_evidence("ev_old", "old", "file", "r", "old",
                                  created_at=old)
        self.app.db.save_evidence("ev_new", "new", "file", "r", "new",
                                  created_at=now)
        out = self.app.logs.purge(now)
        self.assertGreaterEqual(out["runtime_event"], 1)
        self.assertEqual(out["execution_trace"], 1)
        self.assertEqual(out["diagnostic_evidence"], 1)
        self.assertEqual(self.app.db.list_traces(task_id="new")[0]["step"], 1)
        self.assertEqual(len(self.app.evidence.list("new")), 1)

    def test_delete_task_removes_layers_but_keeps_task(self):
        task = self.app.submit("现在几点了", intent="time_query",
                               acceptance=[{"type": "tool_succeeded"}])
        self.app.evidence.add(task.task_id, "file", "ref://x", "证据")
        out = self.app.logs.delete_task(task.task_id)
        self.assertGreaterEqual(out["runtime_event"], 1)
        self.assertGreaterEqual(out["execution_trace"], 1)
        self.assertEqual(out["diagnostic_evidence"], 1)
        self.assertEqual(self.app.bus.count(task.task_id), 0)
        self.assertEqual(self.app.db.list_traces(task_id=task.task_id), [])
        self.assertEqual(self.app.evidence.list(task.task_id), [])
        self.assertIsNotNone(self.app.get_task(task.task_id))


class TestExtendedMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_obs_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runtime_snapshot_covers_new_metrics(self):
        self.app.submit("现在几点了", intent="time_query",
                        acceptance=[{"type": "tool_succeeded"}])
        snap = self.app.metrics.snapshot()
        for key in ("environment", "model_calls", "tools", "react",
                    "recovery", "context", "memory", "logs", "alerts"):
            self.assertIn(key, snap)
        self.assertGreaterEqual(snap["model_calls"]["total"], 1)
        self.assertEqual(snap["model_calls"]["success_rate"], 1.0)
        self.assertGreaterEqual(snap["tools"]["calls"], 1)
        self.assertEqual(snap["tools"]["contract_error_rate"], 0.0)
        self.assertIn("time_query", snap["tasks"]["by_intent"])
        self.assertEqual(
            snap["tasks"]["by_intent"]["time_query"]["success_rate"], 1.0)
        self.assertGreaterEqual(snap["logs"]["runtime_event"], 1)
        self.assertGreaterEqual(snap["logs"]["execution_trace"], 1)

    def test_context_and_memory_metrics(self):
        task = Task(user_goal="x", intent="general")
        task.task_status = TaskStatus.SUCCEEDED
        task.updated_at = task.created_at + 1.0
        task.runtime_manifest = {"context": {
            "memory_candidates": 4, "memory_used": 2, "dropped_memories": 1,
            "quality": {"overflow": True, "constraint_retention": 0.5,
                        "untrusted_instructions": 2,
                        "cross_domain_candidates": 1}}}
        task.result_evidence = [{"budget": {"usage": {
            "cost": 0.2, "model_calls": 2, "tool_calls": 1}}}]
        self.app.db.save_task(task)
        snap = self.app.metrics.snapshot()
        self.assertEqual(snap["context"]["tasks"], 1)
        self.assertEqual(snap["context"]["overflow"], 1)
        self.assertEqual(snap["context"]["constraint_retention_avg"], 0.5)
        self.assertEqual(snap["context"]["key_constraint_omission_rate"], 0.5)
        self.assertEqual(snap["context"]["untrusted_instructions"], 2)
        self.assertEqual(snap["memory"]["hit_rate"], 0.5)
        self.assertEqual(snap["tasks"]["avg_cost"], 0.2)

    def test_recovery_metrics(self):
        ok = Task(user_goal="x", intent="general")
        ok.task_status = TaskStatus.SUCCEEDED
        ok.runtime_manifest = {"resumed_from": "t0"}
        fail = Task(user_goal="x", intent="general")
        fail.task_status = TaskStatus.FAILED
        fail.result_evidence = [{"recovery_attempted": True,
                                 "recovered": False}]
        self.app.db.save_task(ok)
        self.app.db.save_task(fail)
        snap = self.app.metrics.snapshot()
        self.assertEqual(snap["recovery"]["attempts"], 2)
        self.assertEqual(snap["recovery"]["successes"], 1)
        self.assertEqual(snap["recovery"]["success_rate"], 0.5)

    def test_evolution_pass_rate_and_rollback_rate(self):
        promoted = self.app.evolution.propose_candidate(
            "v1", "expert/general", "p1", "h1", change_scope="expert")
        rejected = self.app.evolution.propose_candidate(
            "v1", "expert/general", "p2", "h2", change_scope="expert")
        promoted.status = CandidateStatus.PROMOTED
        rejected.status = CandidateStatus.REJECTED
        self.app.db.save_candidate(promoted)
        self.app.db.save_candidate(rejected)
        self.app.releases.deployments.append({"release_id": "r1"})
        self.app.releases.deployments.append({"release_id": "r2"})
        self.app.releases.rollback_records.append({"reason": "bad"})
        snap = self.app.metrics.snapshot()
        self.assertEqual(snap["evolution"]["pass_rate"], 0.5)
        self.assertEqual(snap["releases"]["rollback_rate"], 0.5)

    def test_prometheus_contains_new_series(self):
        self.app.submit("现在几点了", intent="time_query",
                        acceptance=[{"type": "tool_succeeded"}])
        text = self.app.metrics.prometheus()
        for name in ("evoagent_model_call_success_rate",
                     "evoagent_tool_contract_error_rate",
                     "evoagent_context_overflow_rate",
                     "evoagent_memory_hit_rate",
                     "evoagent_evolution_pass_rate",
                     "evoagent_release_rollback_rate",
                     "evoagent_log_layer_items"):
            self.assertIn(name, text)


class TestAlerts(unittest.TestCase):
    def test_rules_trigger(self):
        snap = {
            "tasks": {"terminal": 10, "success_rate": 0.4,
                      "consecutive_failures": 6},
            "model_calls": {"total": 10, "success_rate": 0.7},
            "tools": {"calls": 20, "contract_error_rate": 0.2},
            "budget": {"runtime_day": {"cost": 9.6}},
            "releases": {"deployments": 4, "rollbacks": 3},
            "react": {"runs": 10, "anomaly_rate": 0.4},
            "events": {"security_incidents": 1},
        }
        alerts = evaluate_alerts(snap, AlertPolicy(), daily_cost_limit=10.0)
        rules = {a["rule"] for a in alerts}
        self.assertTrue({
            "task_success_rate", "consecutive_failures",
            "model_call_success_rate", "tool_contract_error_rate",
            "daily_cost_ratio", "release_rollback_rate",
            "react_anomaly_rate", "security_incident"} <= rules)
        by_rule = {a["rule"]: a for a in alerts}
        self.assertEqual(by_rule["task_success_rate"]["severity"], "critical")
        self.assertEqual(by_rule["security_incident"]["severity"], "critical")

    def test_quiet_when_healthy_and_enough_samples(self):
        snap = {
            "tasks": {"terminal": 10, "success_rate": 0.95,
                      "consecutive_failures": 0},
            "model_calls": {"total": 10, "success_rate": 0.99},
            "tools": {"calls": 20, "contract_error_rate": 0.0},
            "budget": {"runtime_day": {"cost": 1.0}},
            "releases": {"deployments": 4, "rollbacks": 0},
            "react": {"runs": 10, "anomaly_rate": 0.0},
            "events": {"security_incidents": 0},
        }
        self.assertEqual(evaluate_alerts(snap, daily_cost_limit=10.0), [])


class TestObservabilityApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_obs_api_")
        self.app = App(data_dir=self.tmp)
        self.server = ApiServer(self.app, port=0)
        self.port = self.server.httpd.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self, path, token=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()

    def _post(self, path, payload, token=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()

    def test_layer_access_and_alerts(self):
        self.app.config.api_token = "op"
        self.app.config.api_admin_token = "root"
        task = self.app.submit("现在几点了", intent="time_query",
                               acceptance=[{"type": "tool_succeeded"}])
        self.app.evidence.add(task.task_id, "file", "ref://1", "证据",
                              authorized=True)

        status, body = self._get("/logs/layers", token="op")
        self.assertEqual(status, 200)
        layers = {l["layer"]: l for l in json.loads(body)["layers"]}
        self.assertTrue(layers["runtime_event"]["readable"])
        self.assertFalse(layers["diagnostic_evidence"]["readable"])

        status, _ = self._get("/logs/traces?task_id=" + task.task_id,
                              token="op")
        self.assertEqual(status, 200)

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/logs/evidence", token="op")
        self.assertEqual(ctx.exception.code, 403)
        status, body = self._get("/logs/evidence", token="root")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["evidence"][0]["exportable"])

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/logs/purge", {}, token="op")
        self.assertEqual(ctx.exception.code, 403)
        status, _ = self._post("/logs/purge", {}, token="root")
        self.assertEqual(status, 200)

        status, body = self._get("/alerts", token="op")
        self.assertEqual(status, 200)
        self.assertIn("alerts", json.loads(body))

    def test_delete_task_endpoint_cascades(self):
        self.app.config.api_admin_token = "root"
        task = self.app.submit("现在几点了", intent="time_query",
                               acceptance=[{"type": "tool_succeeded"}])
        status, body = self._post(
            "/logs/delete-task", {"task_id": task.task_id}, token="root")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(
            json.loads(body)["deleted"]["runtime_event"], 1)
        self.assertEqual(self.app.bus.count(task.task_id), 0)

    def test_metrics_include_observability_sections(self):
        self.app.submit("现在几点了", intent="time_query",
                        acceptance=[{"type": "tool_succeeded"}])
        status, body = self._get("/metrics")
        self.assertEqual(status, 200)
        snap = json.loads(body)
        self.assertIn("alerts", snap)
        self.assertIn("recovery", snap)
        self.assertIn("context", snap)
        self.assertIn("logs", snap)


if __name__ == "__main__":
    unittest.main()
