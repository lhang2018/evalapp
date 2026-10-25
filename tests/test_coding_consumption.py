# -*- coding: utf-8 -*-
"""ALN-18-G：App 与编程专家真实消费能力包。

验收对照：
- 真实项目跨文件修改/验收/回滚闭环：ReactEngine（真实执行内核）+
  真实项目文件 + coding_edit 跨文件修改 + coding_syntax_check 验收，
  失败时快照恢复；
- 包生命周期与精确版本记录：install_for_task 产出绑定记录
  （pack_id/version/tool_ids/root），与 06-R 精确消费语义对齐；
- 产物归档消费 24-A ArtifactStore：修改文件归档为任务产物证据；
- 未实现 LSP 不宣称支持：本模块与文档均不新增语言服务能力。
"""

import shutil
import tempfile
import unittest

from evoagent.app import App
from evoagent.core.budget import BudgetController, BudgetLimits
from evoagent.core.permissions import PermissionController
from evoagent.models.base import Action
from evoagent.models.mock import MockModel
from evoagent.tools.contract import ToolResult
from evoagent.tools.coding_consumption import (
    CodingPackRuntime, ConsumptionError,
    snapshot_project, restore_snapshot,
)

PROJECT_FILES = {
    "calc/__init__.py": "",
    "calc/ops.py": "def add(a, b):\n    return a + b\n",
    "tests/test_ops.py": (
        "from calc.ops import add\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n"),
}


class ScriptModel(MockModel):
    def __init__(self, actions):
        super().__init__()
        self.actions = list(actions)

    def suggest_action(self, messages, tools):
        return self.actions.pop(0) if self.actions else \
            Action("finish", answer="done")


class RealConsumptionTest(unittest.TestCase):
    """受控真实项目：磁盘上的真实 Python 文件，非单测夹具数据。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_18g_")
        self.project = self.tmp + "/project"
        import os
        os.makedirs(self.project + "/calc", exist_ok=True)
        os.makedirs(self.project + "/tests", exist_ok=True)
        for rel, content in PROJECT_FILES.items():
            with open(self.project + "/" + rel, "w", encoding="utf-8") as fh:
                fh.write(content)
        self.app = App(data_dir=self.tmp + "/data")
        self.runtime = CodingPackRuntime(self.app.tools)

    def tearDown(self):
        self.app.memory.close()
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_binds_exact_pack_identity(self):
        binding = self.runtime.install_for_task("t1", self.project)
        self.assertEqual(binding["pack_id"], "coding_pack")
        self.assertEqual(len(binding["tool_ids"]), 8)
        self.assertEqual(binding["root"], self.project.replace("/", "\\")
                         if "\\" in self.project else self.project)
        # 注册表确实可执行包内工具（真实消费的第一环）
        result = self.app.tools.execute(
            "coding_symbols", {"subdir": "calc"},
            task_id="t1", run_id="run_t1")
        self.assertTrue(result.ok, result.error)

    def test_cross_file_edit_and_verify_through_real_engine(self):
        # 独立注册表 + 预授权（测试夹具模式）；ReactEngine/副作用账本/
        # 幂等与授权边界仍是真实执行内核，仅风险确认按测试约定放行
        from evoagent.react.engine import ReactEngine
        from evoagent.tools.registry import ToolRegistry
        registry = ToolRegistry(PermissionController(high_risk_mode="allow"),
                                self.app.db)
        # 包装进本测试的注册表（与 ReactEngine 同一 registry——
        # 这正是 06-R/18-G 的消费语义：安装到哪个注册表，哪个注册表可执行）
        CodingPackRuntime(registry).install_for_task("t2", self.project)
        model = ScriptModel([
            Action("tool_call", tool="coding_edit", args={
                "path": "calc/ops.py",
                "old_text": "def add(a, b):\n    return a + b\n",
                "new_text": "def add(a, b):\n    return a + b + 0\n"}),
            Action("tool_call", tool="coding_references", args={
                "symbol": "add"}),
            Action("finish", answer="跨文件检查完成"),
        ])
        outcome = ReactEngine(model, registry).run(
            [], BudgetController(BudgetLimits()), task_id="t2",
            run_id="run_t2", model=model)
        self.assertTrue(outcome.finished, outcome.stop_reason)
        # 文件真实被修改（磁盘事实，不是工具返回值）
        with open(self.project + "/calc/ops.py", encoding="utf-8") as fh:
            self.assertIn("a + b + 0", fh.read())

    def test_snapshot_restore_rollback_loop(self):
        self.runtime.install_for_task("t3", self.project)
        snapshot = snapshot_project(self.project)
        # 破坏性修改（语法错误）+ 新增文件
        with open(self.project + "/calc/ops.py", "w") as fh:
            fh.write("def add(a, b:\n    return a + b\n")  # 语法错误
        with open(self.project + "/calc/extra.py", "w") as fh:
            fh.write("x = 1\n")
        # 验收工具真实执行：语法检查失败
        result = self.app.tools.execute(
            "coding_syntax_check", {"targets": ["calc"]},
            task_id="t3", run_id="run_t3")
        self.assertFalse(result.ok)
        self.assertTrue(result.executed, result.error)
        self.assertEqual(result.error_class, "contract_violation")
        self.assertNotEqual(result.evidence["returncode"], 0)
        # 回滚闭环：快照恢复 → 语法恢复 → 新增文件由调用方按
        # changed_paths 处置（明确列出，不越权删除）
        touched = restore_snapshot(snapshot)
        self.assertIn("calc/ops.py", touched)
        with open(self.project + "/calc/ops.py", encoding="utf-8") as fh:
            self.assertIn("return a + b\n", fh.read())
        self.assertIn("calc/extra.py",
                      snapshot.changed_paths(self.project))
        verified = self.app.tools.execute("coding_syntax_check", {"targets": ["calc"]},
            task_id="t3", run_id="run_t3_restored")
        self.assertTrue(verified.ok, verified.error)
        self.assertTrue(verified.executed)
        self.assertEqual(verified.evidence["returncode"], 0)

    def test_archive_modified_consumes_artifact_store(self):
        artifact_store = self.app.releases and None  # 24-A Store 独立实例
        from evoagent.delivery.artifacts import ArtifactStore
        store = ArtifactStore(self.tmp + "/artifacts")
        runtime = CodingPackRuntime(self.app.tools, artifact_store=store)
        snapshot = snapshot_project(self.project)
        with open(self.project + "/calc/ops.py", "a") as fh:
            fh.write("\ndef sub(a, b):\n    return a - b\n")
        out = runtime.archive_modified("t4", self.project, snapshot)
        self.assertEqual(len(out["archived"]), 1, out["skipped"])
        evidence = out["archived"][0]
        self.assertEqual(evidence["source_path"], "calc/ops.py")
        # 归档可读回（24-A 消费链）
        data = store.read(evidence)
        self.assertIn(b"def sub", data)

    def test_no_lsp_claim(self):
        """不宣称 LSP：包工具集与绑定记录均无 lsp/language-server 字段。"""
        binding = self.runtime.install_for_task("t5", self.project)
        blob = str(binding) + str(self.runtime.pack_identity())
        self.assertNotIn("lsp", blob.lower())
        self.assertNotIn("language_server", blob.lower())

    def test_invalid_root_rejected(self):
        with self.assertRaises(ConsumptionError):
            self.runtime.install_for_task("t6", self.tmp + "/no_such_dir")


if __name__ == "__main__":
    unittest.main()
