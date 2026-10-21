# -*- coding: utf-8 -*-
"""F-01 统一错误对象（契约 09 卷 §10）测试。

契约：错误返回至少含 error_code/message/retryable/correlation_id/
related_event_id/required_action/safe_to_repeat 七字段；
safe_to_repeat=unknown ⇒ 必须先对账或人工确认。

本文件分两层：
- 纯函数层：error_payload/normalize_error_payload 的语义表与不变量
- 线上层：真实 HTTP 请求验证裸 {"error": ...} 在 wire 上已是七字段形态，
  且注册表路由与既有链共享同一套归一化（二者同走 _json）。
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
from evoagent.core.contracts import ERROR_FIELDS, REQUIRED_ACTIONS, \
    SAFE_TO_REPEAT
from evoagent.core.errors import ApiError, error_payload, \
    normalize_error_payload


@routes.post("/collabbase/fail400")
def _collabbase_fail400(ctx):
    """注册表路由同样经 _json 归一化（同一管道）。"""
    ctx.json(400, {"error": "输入不合法"})


@routes.post("/collabbase/fail409")
def _collabbase_fail409(ctx):
    ctx.json(409, {"error": "冲突", "safe_to_repeat": "yes",
                   "required_action": "none"})


class TestPayloadBuilder(unittest.TestCase):
    """纯函数层：缺省语义表 + 不变量 + 调用方覆写。"""

    def test_seven_fields_complete(self):
        p = error_payload(404, "任务不存在")
        for f in ERROR_FIELDS:
            self.assertIn(f, p)
        self.assertEqual(p["message"], "任务不存在")
        self.assertEqual(p["error"], "任务不存在")  # 向后兼容键
        self.assertTrue(p["correlation_id"])        # 响应级关联 ID
        self.assertIsNone(p["related_event_id"])

    def test_status_default_semantics(self):
        # 400/404/413/415：请求未执行 → 修正请求后重发，重复无害
        self.assertEqual(error_payload(400, "x")["error_code"],
                         "invalid_request")
        for status in (400, 404, 413, 415):
            p = error_payload(status, "x")
            self.assertEqual(p["required_action"], "fix_request")
            self.assertEqual(p["safe_to_repeat"], "yes")
            self.assertFalse(p["retryable"])
        # 403/401：权限拒绝，无需动作
        p = error_payload(403, "x")
        self.assertEqual(p["error_code"], "forbidden")
        self.assertEqual(p["required_action"], "none")
        # 409：冲突，契约保守口径 → 先对账
        p = error_payload(409, "x")
        self.assertEqual(p["safe_to_repeat"], "unknown")
        self.assertEqual(p["required_action"], "reconcile")
        # 未列出的状态（含 5xx）兜底：一律未知 → 对账
        p = error_payload(503, "x")
        self.assertEqual(p["safe_to_repeat"], "unknown")
        self.assertEqual(p["required_action"], "reconcile")

    def test_caller_overrides_survive(self):
        p = normalize_error_payload(409, {
            "error": "存在未结束任务，拒绝在线恢复",
            "safe_to_repeat": "yes", "required_action": "none"})
        self.assertEqual(p["safe_to_repeat"], "yes")
        self.assertEqual(p["required_action"], "none")

    def test_illegal_values_not_accepted(self):
        p = normalize_error_payload(400, {
            "error": "x", "safe_to_repeat": "maybe",
            "required_action": "free_text", "retryable": "yes"})
        self.assertIn(p["safe_to_repeat"], SAFE_TO_REPEAT)
        self.assertIn(p["required_action"], REQUIRED_ACTIONS)
        self.assertFalse(p["retryable"])

    def test_unknown_invariant(self):
        # 任何来源的 unknown ⇒ required_action 必须是对账/人工确认
        p = normalize_error_payload(500, {"error": "boom",
                                          "safe_to_repeat": "unknown",
                                          "required_action": "retry"})
        self.assertIn(p["required_action"], ("reconcile", "human_confirm"))

    def test_extra_keys_preserved(self):
        p = normalize_error_payload(415, {
            "error": "bad type", "detail": {"got": "text/zip"}})
        self.assertEqual(p["detail"], {"got": "text/zip"})

    def test_api_error_payload(self):
        e = ApiError(409, "存在未结束任务，拒绝在线恢复",
                     safe_to_repeat="unknown", required_action="reconcile")
        p = e.payload()
        self.assertEqual(e.status, 409)
        for f in ERROR_FIELDS:
            self.assertIn(f, p)


class TestWireThrough(unittest.TestCase):
    """线上层：真实 HTTP 验证归一化覆盖两条路由管道。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_f01_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=0)
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, path, body=None):
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     method="POST",
                                     headers={"Content-Type":
                                              "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:  # noqa: F821
            return e.code, json.loads(e.read())

    def test_legacy_chain_error_has_seven_fields(self):
        # 既有链端点：缺 goal → 400 裸 error → wire 上应为七字段
        code, p = self._post("/tasks", {"goal": ""})
        self.assertEqual(code, 400)
        for f in ERROR_FIELDS:
            self.assertIn(f, p)
        self.assertEqual(p["error_code"], "invalid_request")
        self.assertEqual(p["required_action"], "fix_request")
        self.assertEqual(p["safe_to_repeat"], "yes")

    def test_registry_route_normalized_too(self):
        code, p = self._post("/collabbase/fail400", {})
        self.assertEqual(code, 400)
        for f in ERROR_FIELDS:
            self.assertIn(f, p)

    def test_registry_route_caller_overrides(self):
        code, p = self._post("/collabbase/fail409", {})
        self.assertEqual(code, 409)
        self.assertEqual(p["safe_to_repeat"], "yes")
        self.assertEqual(p["required_action"], "none")
        self.assertTrue(p["correlation_id"])

    def test_success_payload_untouched(self):
        # 成功负载不得注入错误字段
        data = json.dumps({"goal": "现在几点了", "intent": "time_query"}).encode()
        req = urllib.request.Request(self.base + "/tasks", data=data,
                                     method="POST",
                                     headers={"Content-Type":
                                              "application/json"})
        with urllib.request.urlopen(req) as resp:
            p = json.loads(resp.read())
        self.assertNotIn("error_code", p)
