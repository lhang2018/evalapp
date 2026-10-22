# -*- coding: utf-8 -*-
"""ALN-18-A 编程工具包：定位、改写、检查/测试与能力包清单。

负向优先：坏代码、失败测试、错误路径都不得被判成功。
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from evoagent.tools.coding import (
    CODING_PACK, CODING_TOOL_IDS, build_coding_pack, coding_edit_tool,
    coding_symbols_tool, coding_syntax_check_tool, coding_test_tool,
    coding_typecheck_tool, install_coding_pack,
)
from evoagent.tools.registry import ToolRegistry


class TestCodingTools(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="evoagent_coding_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _project(self) -> Path:
        return Path(tempfile.mkdtemp(dir=self.tmp))

    # ---- 符号定位 ----

    def test_symbols_locate_definitions_and_skip_dirs(self):
        root = self._project()
        (root / "pkg").mkdir()
        (root / "pkg" / "mod.py").write_text(
            "class A:\n"
            "    def m(self):\n"
            "        pass\n"
            "\n"
            "def outer():\n"
            "    def inner():\n"
            "        pass\n",
            encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "skip.py").write_text(
            "def should_not_appear():\n    pass\n", encoding="utf-8")

        contract, run = coding_symbols_tool(str(root))
        result = run({})

        self.assertTrue(result.ok)
        self.assertTrue(result.goal_reached)
        self.assertTrue(result.independently_verified)
        data = result.data
        quals = {s["qualname"] for s in data["symbols"]}
        self.assertIn("A", quals)
        self.assertIn("A.m", quals)
        self.assertIn("outer.inner", quals)
        self.assertNotIn("should_not_appear", quals)
        self.assertTrue(all(not s["file"].startswith("node_modules")
                            for s in data["symbols"]))
        method = next(s for s in data["symbols"] if s["qualname"] == "A.m")
        self.assertEqual(method["file"], "pkg/mod.py")
        self.assertEqual(method["line"], 2)
        self.assertEqual(contract.contract_test_ref,
                         "tests/test_coding_tools.py")

    def test_symbols_reports_syntax_errors_not_silent(self):
        root = self._project()
        (root / "broken.py").write_text("def x(:\n", encoding="utf-8")
        (root / "ok.py").write_text("def good():\n    pass\n",
                                    encoding="utf-8")
        _, run = coding_symbols_tool(str(root))
        result = run({})
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["errors"]), 1)
        self.assertEqual(result.data["errors"][0]["file"], "broken.py")
        self.assertIn("good", {s["name"] for s in result.data["symbols"]})

    # ---- 受约束改写 ----

    def test_edit_success_verified(self):
        root = self._project()
        path = root / "app.py"
        path.write_text("value = 1\nprint(value)\n", encoding="utf-8")
        _, run = coding_edit_tool(str(root))

        result = run({"path": "app.py", "old_text": "value = 1",
                      "new_text": "value = 2"})

        self.assertTrue(result.ok)
        self.assertTrue(result.goal_reached)
        self.assertTrue(result.independently_verified)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "value = 2\nprint(value)\n")
        self.assertEqual(result.evidence["line"], 1)
        self.assertEqual(result.evidence["replacements"], 1)
        self.assertTrue(result.evidence["written"])

    def test_edit_ambiguous_text_rejected(self):
        root = self._project()
        path = root / "dup.txt"
        original = "x = 1\nx = 1\n"
        path.write_text(original, encoding="utf-8")
        _, run = coding_edit_tool(str(root))

        result = run({"path": "dup.txt", "old_text": "x = 1",
                      "new_text": "x = 2"})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_class, "invalid_arguments")
        self.assertFalse(result.goal_reached)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_edit_missing_text_rejected(self):
        root = self._project()
        path = root / "app.py"
        path.write_text("value = 1\n", encoding="utf-8")
        _, run = coding_edit_tool(str(root))
        result = run({"path": "app.py", "old_text": "nope",
                      "new_text": "x"})
        self.assertFalse(result.ok)
        self.assertIn("未命中", result.error)
        self.assertEqual(path.read_text(encoding="utf-8"), "value = 1\n")

    def test_edit_path_escape_rejected(self):
        root = self._project()
        outside = self.tmp / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        _, run = coding_edit_tool(str(root))

        result = run({"path": "../outside.txt", "old_text": "secret",
                      "new_text": "pwned"})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_class, "auth_or_permission")
        self.assertEqual(outside.read_text(encoding="utf-8"), "secret\n")

    def test_edit_syntax_error_rejected_without_write(self):
        root = self._project()
        path = root / "app.py"
        original = "value = 1\n"
        path.write_text(original, encoding="utf-8")
        _, run = coding_edit_tool(str(root))

        result = run({"path": "app.py", "old_text": "value = 1",
                      "new_text": "value = ("})

        self.assertFalse(result.ok)
        self.assertFalse(result.goal_reached)
        self.assertTrue(result.independently_verified)
        self.assertFalse(result.evidence["written"])
        self.assertIn("syntax_error", result.evidence)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    # ---- 语法检查 ----

    def test_syntax_check_pass_and_fail(self):
        root = self._project()
        good = root / "good.py"
        good.write_text("def ok():\n    return 1\n", encoding="utf-8")
        _, run = coding_syntax_check_tool(str(root))
        passed = run({"targets": ["good.py"]})
        self.assertTrue(passed.ok)
        self.assertTrue(passed.goal_reached)

        bad = root / "bad.py"
        bad.write_text("def broken(:\n", encoding="utf-8")
        failed = run({"targets": ["bad.py"]})
        self.assertFalse(failed.ok)
        self.assertFalse(failed.goal_reached)
        self.assertFalse(failed.independently_verified)
        self.assertNotEqual(failed.evidence["returncode"], 0)
        self.assertFalse((root / "__pycache__").exists())

    # ---- 类型检查 ----

    def test_typecheck_unavailable_is_not_success(self):
        root = self._project()
        (root / "app.py").write_text("value: int = 1\n", encoding="utf-8")
        checker = [sys.executable, "-c", "raise SystemExit(3)"]
        _, run = coding_typecheck_tool(str(root), checker=checker)

        result = run({"targets": ["app.py"]})

        self.assertFalse(result.ok)
        self.assertFalse(result.executed)
        self.assertIsNone(result.goal_reached)
        self.assertFalse(result.independently_verified)
        self.assertFalse(result.evidence["available"])

    def test_typecheck_fake_checker_pass_and_fail(self):
        root = self._project()
        fake = root / "fake_mypy.py"
        fake.write_text(
            "import sys\n"
            "if '--version' in sys.argv:\n"
            "    print('fake-mypy 1.0')\n"
            "    raise SystemExit(0)\n"
            "targets = [a for a in sys.argv[1:] if not a.startswith('-')]\n"
            "bad = False\n"
            "for t in targets:\n"
            "    try:\n"
            "        text = open(t, encoding='utf-8').read()\n"
            "    except OSError:\n"
            "        continue\n"
            "    if 'TYPE_ERROR' in text:\n"
            "        bad = True\n"
            "        print(f'{t}: error: TYPE_ERROR')\n"
            "raise SystemExit(1 if bad else 0)\n",
            encoding="utf-8")
        (root / "good.py").write_text("value: int = 1\n", encoding="utf-8")
        (root / "bad.py").write_text("# TYPE_ERROR\nvalue = 'x'\n",
                                     encoding="utf-8")
        checker = [sys.executable, str(fake)]
        _, run = coding_typecheck_tool(str(root), checker=checker)

        passed = run({"targets": ["good.py"]})
        self.assertTrue(passed.ok)
        self.assertTrue(passed.goal_reached)
        self.assertTrue(passed.evidence["available"])

        failed = run({"targets": ["bad.py"]})
        self.assertFalse(failed.ok)
        self.assertFalse(failed.goal_reached)
        self.assertFalse(failed.independently_verified)
        self.assertEqual(failed.evidence["returncode"], 1)

    # ---- 测试执行 ----

    def test_test_tool_pass_and_fail(self):
        root = self._project()
        tests_dir = root / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_ok.py").write_text(
            "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
        _, run = coding_test_tool(str(root))

        passed = run({"targets": ["tests"]})
        self.assertTrue(passed.ok)
        self.assertTrue(passed.goal_reached)
        self.assertTrue(passed.independently_verified)
        self.assertGreaterEqual(passed.data["passed"], 1)
        self.assertEqual(passed.data["failed"], 0)

        (tests_dir / "test_bad.py").write_text(
            "def test_bad():\n    assert 1 + 1 == 3\n", encoding="utf-8")
        failed = run({"targets": ["tests"]})
        self.assertFalse(failed.ok)
        self.assertFalse(failed.goal_reached)
        self.assertFalse(failed.independently_verified)
        self.assertGreaterEqual(failed.data["failed"], 1)

    def test_test_tool_no_tests_is_not_success(self):
        root = self._project()
        (root / "empty").mkdir()
        _, run = coding_test_tool(str(root))

        result = run({"targets": ["empty"]})

        self.assertFalse(result.ok)
        self.assertFalse(result.goal_reached)
        self.assertIn("未收集到测试", result.error)

    # ---- 契约与能力包 ----

    def test_contracts_declare_required_fields(self):
        root = self._project()
        pack = build_coding_pack(str(root))
        self.assertEqual(set(pack), set(CODING_TOOL_IDS))
        for tool_id, (contract, _runner) in pack.items():
            self.assertEqual(contract.tool_id, tool_id)
            self.assertTrue(contract.permission_requirements, tool_id)
            self.assertTrue(contract.cost_model, tool_id)
            self.assertTrue(contract.offline_capable, tool_id)
            self.assertFalse(contract.requires_network, tool_id)
            self.assertTrue(contract.risk_class, tool_id)
            self.assertEqual(contract.contract_test_ref,
                             "tests/test_coding_tools.py")
        self.assertEqual(pack["coding_edit"][0].side_effect_level, "local")
        self.assertEqual(pack["coding_edit"][0].risk_class, "high")
        self.assertEqual(pack["coding_symbols"][0].side_effect_level, "none")

    def test_install_pack_and_manifest(self):
        root = self._project()
        registry = ToolRegistry()
        manifest = install_coding_pack(registry, str(root))

        self.assertEqual(manifest.pack_id, "coding_pack")
        self.assertEqual(manifest.version, "1.0.0")
        self.assertIsNone(manifest.rollback_version)
        self.assertEqual(set(manifest.as_dict()["tool_ids"]),
                         set(CODING_TOOL_IDS))
        self.assertEqual(CODING_PACK, manifest)
        listed = {c.tool_id for c in registry.available_tools()}
        self.assertTrue(set(CODING_TOOL_IDS).issubset(listed))
        self.assertEqual(registry.contract("coding_edit").risk_class, "high")
        self.assertEqual(registry.contract("coding_symbols").risk_class, "low")


if __name__ == "__main__":
    unittest.main()
