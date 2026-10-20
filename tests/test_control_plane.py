"""可观测性与安全控制面测试（PRD 83-85 / Architecture 18）。

覆盖：令牌角色、发布回滚的高风险闸门、指标聚合与 Prometheus 输出、
审计只追加、管理端只读页面、健康信息暴露面。
"""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from evoagent.api.auth import ApiAuth
from evoagent.api.server import ApiServer
from evoagent.app import App


class TestApiAuth(unittest.TestCase):
    def test_open_mode_allows_anonymous_admin(self):
        res = ApiAuth().check(None, "POST", "/releases/rollback")
        self.assertTrue(res.ok)
        self.assertEqual(res.role, "admin")

    def test_missing_and_invalid_token(self):
        auth = ApiAuth(token="op", admin_token="root")
        self.assertEqual(auth.check(None, "GET", "/tasks").status, 401)
        self.assertEqual(
            auth.check("Bearer nope", "GET", "/tasks").status, 403)

    def test_operator_cannot_do_admin_actions(self):
        auth = ApiAuth(token="op", admin_token="root")
        self.assertEqual(auth.check("Bearer op", "GET", "/tasks").role,
                         "operator")
        self.assertEqual(
            auth.check("Bearer op", "POST", "/releases/rollback").status, 403)
        self.assertEqual(
            auth.check("Bearer op", "POST",
                       "/evolution/candidates/cand_1/deploy").status, 403)
        self.assertTrue(auth.check("Bearer root", "POST",
                                   "/releases/rollback").ok)
        # 普通提交任务对 operator 开放
        self.assertTrue(auth.check("Bearer op", "POST", "/tasks").ok)


class TestAudit(unittest.TestCase):
    def test_record_is_persisted_and_listed(self):
        tmp = tempfile.mkdtemp(prefix="evoagent_audit_")
        try:
            app = App(data_dir=tmp)
            app.audit.record("operator", "evolution.propose",
                             target="cand_x", detail={"component": "planner"})
            app.audit.record("admin", "release.rollback", target="rel_1",
                             outcome="failed")
            rows = app.audit.list(limit=10)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["action"], "release.rollback")
            self.assertEqual(rows[0]["outcome"], "failed")
            self.assertEqual(rows[1]["detail"]["component"], "planner")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_metrics_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_snapshot_has_expected_sections(self):
        self.app.submit("现在几点了", intent="time_query",
                        acceptance=[{"type": "tool_succeeded"}])
        snap = self.app.metrics.snapshot()
        for key in ("tasks", "events", "diagnostics", "evolution",
                    "budget", "releases", "tools_health"):
            self.assertIn(key, snap)
        self.assertGreaterEqual(snap["tasks"]["total"], 1)
        self.assertGreaterEqual(snap["tasks"]["success_rate"], 0.0)
        self.assertIn("runtime_day", snap["budget"])
        self.assertIn("evolution_day", snap["budget"])

    def test_prometheus_text(self):
        self.app.submit("现在几点了", intent="time_query",
                        acceptance=[{"type": "tool_succeeded"}])
        text = self.app.metrics.prometheus()
        self.assertIn("evoagent_tasks_total", text)
        self.assertIn('evoagent_budget_model_calls{scope="runtime_day"}', text)
        self.assertIn("# TYPE", text)


class TestControlPlaneServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_plane_")
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
            return resp.status, resp.headers.get("Content-Type", ""), \
                resp.read().decode()

    def test_admin_page_and_metrics_formats(self):
        status, ctype, body = self._get("/admin")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("EvoAgent", body)

        status, ctype, body = self._get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertIn("tasks", json.loads(body))

        status, ctype, body = self._get("/metrics?format=prometheus")
        self.assertEqual(status, 200)
        self.assertIn("text/plain", ctype)
        self.assertIn("evoagent_tasks_total", body)

    def test_malformed_body_returns_400_and_server_stays_up(self):
        for payload in (b'{"goal":"\xcf\xb2\xb7\xa8"}', b"[1, 2]"):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/tasks",
                data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 400)
        status, _, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_token_enforcement_and_audit_on_mutation(self):
        self.app.config.api_token = "op"
        self.app.config.api_admin_token = "root"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/audit")
        self.assertEqual(ctx.exception.code, 401)

        body = json.dumps({"reason": "test"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/releases/rollback",
            data=body, method="POST")
        req.add_header("Authorization", "Bearer op")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

        status, _, text = self._get("/audit", token="root")
        self.assertEqual(status, 200)
        self.assertIn("audit", json.loads(text))


if __name__ == "__main__":
    unittest.main()
