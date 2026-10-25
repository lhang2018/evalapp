"""统一任务契约三入口同源测试（ALN-23-A）。

验收口径（任务卡）：三入口字段同源（REST/CLI/客户端）；兼容默认值与非法
输入；general 提示不能无条件屏蔽真实分类；明确约束不丢失。
"""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.request

from evoagent.cli import cmd_task
from evoagent.app import App
from evoagent.core.task import TaskStatus


class TestTaskContractEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_contract_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        # REST returns 202 while its worker is still finishing the run. Closing
        # SQLite first turns successful tests into background thread failures.
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.app.controller._lock:
                active = bool(self.app.controller._active)
            if not active:
                break
            time.sleep(0.01)
        self.assertFalse(active, "任务工作线程未在清理前退出")
        self.app.db.close()
        self.app.memory.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- REST 入口 ----

    def _post_task(self, body):
        from evoagent.api.server import ApiServer
        srv = ApiServer(self.app, host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.port}"
        req = urllib.request.Request(
            base + "/tasks", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
        finally:
            srv.shutdown()

    def test_rest_entry_persists_full_contract(self):
        st, out = self._post_task({
            "goal": "帮我把采购笔记整理成对比报告",
            "intent": "general",
            "constraints": ["用中文", "不超过三页"],
            "acceptance_criteria": [{"criterion": "包含数据来源"}],
            "required_tools": ["file_write"],
            "required_permissions": ["file"],
            "open_questions": ["数据来源优先级？"],
            "expected_output": "markdown 报告",
            "target_object": "实验数据",
            "complexity": "moderate",
        })
        self.assertEqual(st, 202, out)
        task = self.app.db.get_task(out["task_id"])
        self.assertIsNotNone(task)
        # general 提示不屏蔽真实分类（heuristic 命中 note_taking 关键词"笔记"）
        self.assertEqual(task.intent, "note_taking")
        # 明确约束不丢失
        self.assertEqual(task.constraints, ["用中文", "不超过三页"])
        self.assertEqual(task.acceptance_criteria,
                         [{"criterion": "包含数据来源"}])
        self.assertEqual(task.target_object, "实验数据")
        self.assertEqual(task.expected_output, "markdown 报告")
        self.assertEqual(task.required_tools, ["file_write"])
        self.assertEqual(task.required_permissions, ["file"])
        self.assertEqual(task.open_questions, ["数据来源优先级？"])
        self.assertEqual(task.complexity, "moderate")

    def test_general_hint_does_not_block_specific_classification(self):
        t = self.app.controller.submit_task(
            "帮我记个笔记：采购清单", intent="general", wait=True)
        self.assertEqual(t.intent, "note_taking",
                         "general 提示不能屏蔽关键词分类")

    def test_explicit_specific_hint_still_short_circuits(self):
        t = self.app.controller.submit_task(
            "随便算个东西", intent="computation", wait=True)
        self.assertEqual(t.intent, "computation")

    def test_truly_general_goal_stays_general(self):
        t = self.app.controller.submit_task(
            "你好呀", intent="general", wait=True)
        self.assertEqual(t.intent, "general")

    def test_illegal_model_fields_rejected(self):
        st, out = self._post_task({
            "goal": "x", "complexity": "ultra"})
        self.assertEqual(st, 400)
        self.assertIn("complexity", out["error"])

    # ---- CLI 入口 ----

    def test_cli_entry_same_fields(self):
        """CLI 入口与 REST 入口同源：同一契约参数产生一致任务字段。"""
        t = cmd_task(
            "帮我把采购笔记整理成对比报告", intent="general", risk="low",
            wait=False, app_ctx=self.app,
            constraints=["用中文", "不超过三页"],
            acceptance=["包含数据来源"],
            required_tools=["file_write"],
            required_permissions=["file"],
            open_questions=["数据来源优先级？"],
            expected_output="markdown 报告",
            target_object="实验数据",
            complexity="moderate")
        cli_task = self.app.db.get_task(t)

        st, rest_out = self._post_task({
            "goal": "帮我把采购笔记整理成对比报告",
            "constraints": ["用中文", "不超过三页"],
            "acceptance_criteria": [{"criterion": "包含数据来源"}],
            "required_tools": ["file_write"],
            "required_permissions": ["file"],
            "open_questions": ["数据来源优先级？"],
            "expected_output": "markdown 报告",
            "target_object": "实验数据",
            "complexity": "moderate",
        })
        self.assertEqual(st, 202, rest_out)
        rest_task = self.app.db.get_task(rest_out["task_id"])
        for field in ("constraints", "acceptance_criteria", "required_tools",
                      "required_permissions", "open_questions",
                      "expected_output", "target_object", "complexity"):
            self.assertEqual(getattr(cli_task, field),
                             getattr(rest_task, field),
                             f"CLI 与 REST 的 {field} 必须同源")

    def test_cmd_task_with_contract_kwargs(self):
        """cmd_task 直接以契约 kwargs 提交（app 注入避免默认目录）。"""
        cmd_task(
            "帮我记个笔记：采购清单", intent="general", risk="low",
            wait=True, app_ctx=self.app,
            constraints=["先列采购项"], required_tools=["file_write"])
        t = self.app.db.list_tasks(limit=1)[0]
        self.assertEqual(t.intent, "note_taking")
        self.assertEqual(t.constraints, ["先列采购项"])


if __name__ == "__main__":
    unittest.main()
