"""任务模型全字段（PRD 7.3）。

覆盖：字段往返、旧任务兼容、显式输入优先、计划/工作流推断、
REST 输入校验与持久化。
"""

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


class TestTaskModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_taskmodel_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_field_roundtrip(self):
        task = Task(user_goal="整理笔记", target_object="notes/demo.txt",
                    expected_output="按日期归档的笔记",
                    complexity="moderate",
                    required_tools=["file_read", "file_write"],
                    required_permissions=["local.write"],
                    requires_network=False, workflow_suitable=True,
                    open_questions=["是否覆盖同名文件？"])
        restored = Task.from_dict(task.to_dict())
        for name in ("target_object", "expected_output", "complexity",
                     "required_tools", "required_permissions",
                     "requires_network", "workflow_suitable", "open_questions"):
            self.assertEqual(getattr(restored, name), getattr(task, name),
                             name)

    def test_legacy_task_defaults(self):
        legacy = {"task_id": "legacy1", "user_goal": "旧任务",
                  "task_status": "CREATED", "risk_level": "low"}
        task = Task.from_dict(legacy)
        self.assertEqual(task.required_tools, [])
        self.assertEqual(task.required_permissions, [])
        self.assertIsNone(task.requires_network)
        self.assertIsNone(task.workflow_suitable)
        self.assertEqual(task.complexity, "")
        self.assertEqual(task.open_questions, [])

    def test_explicit_fields_survive_execution(self):
        task = self.app.submit(
            "计算 2+3", intent="computation",
            target_object="expression:2+3", expected_output="5",
            complexity="complex", required_tools=["calculate"],
            required_permissions=["calc.execute"],
            requires_network=False, workflow_suitable=False,
            open_questions=["是否允许小数？"],
            acceptance=[{"type": "tool_succeeded"}])
        self.assertEqual(task.task_status, TaskStatus.SUCCEEDED)
        stored = self.app.get_task(task.task_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.target_object, "expression:2+3")
        self.assertEqual(stored.expected_output, "5")
        self.assertEqual(stored.complexity, "complex")
        self.assertEqual(stored.required_tools, ["calculate"])
        self.assertEqual(stored.required_permissions, ["calc.execute"])
        self.assertFalse(stored.requires_network)
        self.assertFalse(stored.workflow_suitable)
        self.assertEqual(stored.open_questions, ["是否允许小数？"])

    def test_plan_enrichment(self):
        task = self.app.submit("现在几点了", intent="time_query",
                               acceptance=[{"type": "tool_succeeded"}])
        self.assertEqual(task.task_status, TaskStatus.SUCCEEDED)
        stored = self.app.get_task(task.task_id)
        self.assertIn("current_time", stored.required_tools)
        self.assertTrue(stored.required_permissions == []
                        or isinstance(stored.required_permissions, list))
        self.assertFalse(stored.requires_network)
        self.assertIn(stored.complexity, ("simple", "moderate", "complex"))
        self.assertIsInstance(stored.workflow_suitable, bool)

    def test_network_inference_and_complexity(self):
        task = Task(user_goal="抓取网页", intent="general")
        self.app.controller._enrich_task_model(
            task, ["http_get"], 6)
        self.assertTrue(task.requires_network)
        self.assertEqual(task.complexity, "complex")
        self.assertEqual(task.required_tools, ["http_get"])

    def test_workflow_path_enrichment(self):
        task = Task(user_goal="生成漫剧", intent="comic_drama",
                    domain="comic_drama")
        hints = ["mock_script_gen", "mock_video_clip", "mock_compose"]
        self.app.controller._enrich_task_model(
            task, hints, 7, has_workflow=True)
        self.assertTrue(task.workflow_suitable)
        self.assertEqual(task.complexity, "complex")
        self.assertEqual(set(task.required_tools), set(hints))


class TestTaskModelApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_taskmodel_api_")
        self.app = App(data_dir=self.tmp)
        self.server = ApiServer(self.app, port=0)
        self.port = self.server.httpd.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/tasks",
            data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _get_task(self, task_id):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/tasks/{task_id}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def test_api_accepts_and_persists_fields(self):
        status, out = self._post({
            "goal": "计算 7+8", "intent": "computation",
            "target_object": "expression:7+8", "expected_output": "15",
            "complexity": "moderate", "required_tools": ["calculate"],
            "required_permissions": ["calc.execute"],
            "requires_network": False, "workflow_suitable": True,
            "open_questions": ["是否需要单位？"],
            "acceptance_criteria": [{"type": "tool_succeeded"}]
        })
        self.assertEqual(status, 202)
        task_id = out["task_id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            data = self._get_task(task_id)
            if data["task_status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(0.05)
        self.assertEqual(data["target_object"], "expression:7+8")
        self.assertEqual(data["expected_output"], "15")
        self.assertEqual(data["complexity"], "moderate")
        self.assertEqual(data["required_tools"], ["calculate"])
        self.assertEqual(data["required_permissions"], ["calc.execute"])
        self.assertFalse(data["requires_network"])
        self.assertTrue(data["workflow_suitable"])
        self.assertEqual(data["open_questions"], ["是否需要单位？"])

    def test_api_rejects_invalid_fields(self):
        cases = [
            {"goal": "x", "complexity": "huge"},
            {"goal": "x", "required_tools": "calculate"},
            {"goal": "x", "required_tools": [1]},
            {"goal": "x", "requires_network": "yes"},
            {"goal": "x", "open_questions": ["q"] * 21},
        ]
        for payload in cases:
            status, out = self._post(payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
