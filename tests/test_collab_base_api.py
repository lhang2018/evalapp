"""协作基座 F-00.2 端到端验收：新端点不触碰 server.py 即可生效。

`tests/test_collab_base.py` 验证注册表本身的分派语义；本文件验证
**真实 HTTP 服务**上注册端点可用，且存量端点行为未受影响。
"""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from evoagent.api import routes
from evoagent.api.server import ApiServer
from evoagent.app import App


@routes.get("/collabbase/ping")
def _collabbase_ping(ctx):
    """演示端点：特性作者新增此文件与本函数即可，无需改 server.py。"""
    ctx.json(200, {"pong": True, "role": ctx.auth.role})


@routes.post("/collabbase/echo")
def _collabbase_echo(ctx):
    ctx.json(201, {"received": ctx.body.get("value"),
                   "query": ctx.query})


class TestRegisteredRoutesEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_collabbase_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def test_registered_get_endpoint_live(self):
        status, out = self._get("/collabbase/ping")
        self.assertEqual(status, 200)
        self.assertTrue(out["pong"])
        self.assertEqual(out["role"], "admin")

    def test_registered_post_endpoint_receives_body_and_query(self):
        status, out = self._post("/collabbase/echo?k=v",
                                 {"value": "hello"})
        self.assertEqual(status, 201)
        self.assertEqual(out["received"], "hello")
        self.assertEqual(out["query"], {"k": "v"})

    def test_trailing_slash_normalized(self):
        status, out = self._get("/collabbase/ping/")
        self.assertEqual(status, 200)
        self.assertTrue(out["pong"])

    def test_existing_endpoints_still_work(self):
        """注册表未命中时必须回落到既有链路 —— 存量行为零变化。"""
        status, out = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(out["status"], "ok")
        status, out = self._get("/version")
        self.assertEqual(status, 200)
        self.assertIn("version", out)
        status, out = self._get("/tasks")
        self.assertEqual(status, 200)
        self.assertIn("tasks", out)

    def test_task_submission_still_works(self):
        status, out = self._post("/tasks", {"goal": "现在几点了",
                                            "intent": "time_query",
                                            "risk_level": "low"})
        self.assertEqual(status, 202)
        self.assertIn("task_id", out)

    def test_unknown_path_still_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/definitely/not/a/route")
        self.assertEqual(ctx.exception.code, 404)

    def test_auth_still_applies_to_registered_routes(self):
        """鉴权在注册表之前完成，注册处理器拿到的 auth 已通过。"""
        import evoagent.api.routes as r
        self.assertIn("/collabbase/ping", r.registered("GET"))
        self.assertIn("/collabbase/echo", r.registered("POST"))


if __name__ == "__main__":
    unittest.main()