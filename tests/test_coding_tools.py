# -*- coding: utf-8 -*-
"""ALN-18-A 编程工具包：定位、改写、检查/测试与能力包清单。

负向优先：坏代码、失败测试、错误路径都不得被判成功。
"""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evoagent.tools.coding import (
    CODING_PACK, CODING_TOOL_IDS, build_coding_pack, coding_edit_tool,
    coding_patch_recover_tool, coding_patch_tool, coding_references_tool,
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
        self.assertEqual(method["signature"], "(self)")
        outer = next(s for s in data["symbols"] if s["qualname"] == "outer")
        self.assertEqual(outer["signature"], "()")
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

    # ---- 引用/调用点 ----

    def test_references_distinguish_linked_and_unlinked_names(self):
        root = self._project()
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (root / "pkg" / "a.py").write_text(
            "def target(x):\n    return x\n", encoding="utf-8")
        (root / "pkg" / "b.py").write_text(
            "from pkg.a import target\n"
            "\n"
            "def run():\n"
            "    return target(1)\n", encoding="utf-8")
        (root / "loose.py").write_text(
            "def unrelated():\n"
            "    target()\n", encoding="utf-8")

        _, run = coding_references_tool(str(root))
        result = run({"name": "target"})

        self.assertTrue(result.ok)
        self.assertTrue(result.goal_reached)
        definitions = result.data["definitions"]
        self.assertEqual([d["file"] for d in definitions], ["pkg/a.py"])
        refs = result.data["references"]
        linked = [r for r in refs if r["file"] == "pkg/b.py"]
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["kind"], "call")
        self.assertEqual(linked[0]["context"], "run")
        self.assertEqual(linked[0]["confidence"], "high")
        loose = [r for r in refs if r["file"] == "loose.py"]
        self.assertEqual(len(loose), 1)
        self.assertEqual(loose[0]["confidence"], "low")
        self.assertEqual(result.data["import_edges"],
                         [{"file": "pkg/b.py", "module": "pkg.a"}])

    def test_references_attribute_and_same_file(self):
        root = self._project()
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (root / "pkg" / "a.py").write_text(
            "def target():\n    return 1\n"
            "\n"
            "def local_call():\n"
            "    return target()\n", encoding="utf-8")
        (root / "user.py").write_text(
            "import pkg.a\n"
            "\n"
            "def go():\n"
            "    return pkg.a.target()\n", encoding="utf-8")

        _, run = coding_references_tool(str(root))
        result = run({"name": "target"})

        kinds = {(r["file"], r["kind"], r["confidence"])
                 for r in result.data["references"]}
        self.assertIn(("pkg/a.py", "call", "high"), kinds)
        self.assertIn(("user.py", "call", "medium"), kinds)

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

    def test_edit_expected_sha_rejects_concurrent_change(self):
        root = self._project()
        path = root / "app.py"
        path.write_text("value = 1\n", encoding="utf-8")
        stale = hashlib.sha256(b"value = 0\n").hexdigest()
        _, run = coding_edit_tool(str(root))

        rejected = run({"path": "app.py", "old_text": "value = 1",
                        "new_text": "value = 2",
                        "expected_sha256": stale})
        self.assertFalse(rejected.ok)
        self.assertFalse(rejected.goal_reached)
        self.assertEqual(rejected.evidence["expected_sha256"], stale)
        self.assertEqual(path.read_text(encoding="utf-8"), "value = 1\n")

        current = hashlib.sha256(b"value = 1\n").hexdigest()
        accepted = run({"path": "app.py", "old_text": "value = 1",
                        "new_text": "value = 2",
                        "expected_sha256": current[:12]})
        self.assertTrue(accepted.ok)
        self.assertEqual(accepted.evidence["sha256_before"], current)
        self.assertEqual(path.read_text(encoding="utf-8"), "value = 2\n")

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

    # ---- 原子多文件补丁 ----

    def test_patch_multi_file_success_evidence(self):
        root = self._project()
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        (root / "b.txt").write_text("hello\n", encoding="utf-8")
        _, run = coding_patch_tool(str(root))

        result = run({"edits": [
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
            {"path": "b.txt", "old_text": "hello", "new_text": "world"},
        ]})

        self.assertTrue(result.ok)
        self.assertTrue(result.goal_reached)
        self.assertTrue(result.independently_verified)
        self.assertFalse(result.data["dry_run"])
        self.assertEqual(result.data["applied"], ["a.py", "b.txt"])
        self.assertFalse((root / ".evoagent_patch_journal").exists())
        self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "x = 2\n")
        self.assertEqual((root / "b.txt").read_text(encoding="utf-8"),
                         "world\n")
        for item in result.data["files"]:
            self.assertEqual(len(item["sha256_before"]), 64)
            self.assertEqual(len(item["sha256_after"]), 64)
            self.assertEqual(item["line"], 1)
        after = {f["file"]: f["sha256_after"] for f in result.data["files"]}
        self.assertEqual(
            after["a.py"],
            hashlib.sha256(b"x = 2\n").hexdigest())

        # 证据摘要可作为下一次的并发保护输入
        again = run({"edits": [
            {"path": "a.py", "old_text": "x = 2", "new_text": "x = 3",
             "expected_sha256": after["a.py"][:12]},
        ]})
        self.assertTrue(again.ok)

    def test_patch_dry_run_previews_without_writing(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("x = 1\n", encoding="utf-8")
        _, run = coding_patch_tool(str(root))

        result = run({"dry_run": True, "edits": [
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
        ]})

        self.assertTrue(result.ok)
        self.assertTrue(result.goal_reached)
        self.assertTrue(result.data["dry_run"])
        self.assertEqual(len(result.data["files"]), 1)
        self.assertIn("-x = 1", result.data["files"][0]["diff"])
        self.assertIn("+x = 2", result.data["files"][0]["diff"])
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\n")
        self.assertEqual(result.evidence["written"], [])

    def test_patch_second_edit_failure_writes_nothing(self):
        root = self._project()
        first = root / "a.py"
        second = root / "b.txt"
        first.write_text("x = 1\n", encoding="utf-8")
        second.write_text("hello\n", encoding="utf-8")
        _, run = coding_patch_tool(str(root))

        result = run({"edits": [
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
            {"path": "b.txt", "old_text": "absent", "new_text": "world"},
        ]})

        self.assertFalse(result.ok)
        self.assertFalse(result.goal_reached)
        self.assertEqual(result.error_class, "invalid_arguments")
        self.assertEqual(first.read_text(encoding="utf-8"), "x = 1\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "hello\n")

    def test_patch_syntax_error_aborts_all(self):
        root = self._project()
        first = root / "a.py"
        second = root / "b.py"
        first.write_text("x = 1\n", encoding="utf-8")
        second.write_text("y = 2\n", encoding="utf-8")
        _, run = coding_patch_tool(str(root))

        result = run({"edits": [
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
            {"path": "b.py", "old_text": "y = 2", "new_text": "y = ("},
        ]})

        self.assertFalse(result.ok)
        self.assertFalse(result.goal_reached)
        self.assertEqual(first.read_text(encoding="utf-8"), "x = 1\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "y = 2\n")

    def test_patch_rejects_duplicate_path_and_noop(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("x = 1\n", encoding="utf-8")
        _, run = coding_patch_tool(str(root))

        dup = run({"edits": [
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
            {"path": "a.py", "old_text": "x = 2", "new_text": "x = 3"},
        ]})
        self.assertFalse(dup.ok)
        self.assertIn("同一文件", dup.error)
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\n")

        alias = run({"edits": [
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
            {"path": "./a.py", "old_text": "x = 1", "new_text": "x = 3"},
        ]})
        self.assertFalse(alias.ok)
        self.assertIn("同一文件", alias.error)
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\n")

        noop = run({"edits": [
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 1"},
        ]})
        self.assertFalse(noop.ok)
        self.assertIn("无变化", noop.error)
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\n")

        empty = run({"edits": []})
        self.assertFalse(empty.ok)
        too_many = run({"edits": [
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
        ] * 51})
        self.assertFalse(too_many.ok)
        self.assertIn("最多 50", too_many.error)
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\n")

    def test_patch_stale_sha_aborts_all(self):
        root = self._project()
        first = root / "a.py"
        second = root / "b.py"
        first.write_text("x = 1\n", encoding="utf-8")
        second.write_text("y = 2\n", encoding="utf-8")
        stale = hashlib.sha256(b"y = 0\n").hexdigest()
        _, run = coding_patch_tool(str(root))

        result = run({"edits": [
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
            {"path": "b.py", "old_text": "y = 2", "new_text": "y = 3",
             "expected_sha256": stale},
        ]})

        self.assertFalse(result.ok)
        self.assertEqual(result.evidence["expected_sha256"], stale)
        self.assertEqual(result.evidence["actual_sha256"],
                         hashlib.sha256(b"y = 2\n").hexdigest())
        self.assertEqual(first.read_text(encoding="utf-8"), "x = 1\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "y = 2\n")

    def test_patch_post_write_mismatch_rolls_back(self):
        root = self._project()
        first = root / "a.py"
        second = root / "b.py"
        first.write_text("x = 1\n", encoding="utf-8")
        second.write_text("y = 2\n", encoding="utf-8")
        original_write = Path.write_text
        seen = {"n": 0}

        def corrupt_second(self, data, *args, **kwargs):
            if self.name == "b.py" and seen["n"] == 0:
                seen["n"] += 1
                return original_write(self, "corrupted\n", *args, **kwargs)
            return original_write(self, data, *args, **kwargs)

        _, run = coding_patch_tool(str(root))
        with mock.patch.object(Path, "write_text", corrupt_second):
            result = run({"edits": [
                {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
                {"path": "b.py", "old_text": "y = 2", "new_text": "y = 3"},
            ]})

        self.assertFalse(result.ok)
        self.assertFalse(result.goal_reached)
        self.assertTrue(result.evidence["rolled_back"])
        # A foreign/corrupted post-write value is not proof that it is ours to
        # overwrite. Preserve it and retain the journal for reconciliation.
        self.assertTrue(result.side_effect_uncertain)
        self.assertIsNotNone(result.evidence["journal"])
        self.assertTrue((root / ".evoagent_patch_journal").exists())
        self.assertEqual(first.read_text(encoding="utf-8"), "x = 1\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "corrupted\n")

    def test_patch_rollback_failure_is_flagged_uncertain(self):
        root = self._project()
        first = root / "a.py"
        second = root / "b.py"
        first.write_text("x = 1\n", encoding="utf-8")
        second.write_text("y = 2\n", encoding="utf-8")
        original_write = Path.write_text

        def fail_second(self, data, *args, **kwargs):
            if self.name == "b.py":
                original_write(self, "partial write\n", *args, **kwargs)
                raise OSError("disk locked")
            return original_write(self, data, *args, **kwargs)

        _, run = coding_patch_tool(str(root))
        with mock.patch.object(Path, "write_text", fail_second):
            result = run({"edits": [
                {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
                {"path": "b.py", "old_text": "y = 2", "new_text": "y = 3"},
            ]})

        self.assertFalse(result.ok)
        self.assertFalse(result.goal_reached)
        self.assertTrue(result.side_effect_uncertain)
        self.assertFalse(result.independently_verified)
        self.assertIn("b.py", result.evidence["restore_failed"])
        self.assertIsNotNone(result.evidence["journal"])
        journals = list((root / ".evoagent_patch_journal").glob("*.json"))
        self.assertEqual(len(journals), 1)
        self.assertEqual(first.read_text(encoding="utf-8"), "x = 1\n")

    # ---- 统一 diff 输入 ----

    def test_patch_diff_multi_file_applies(self):
        root = self._project()
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        (root / "b.txt").write_text("hello\n", encoding="utf-8")
        diff = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
            "--- a/b.txt\n"
            "+++ b/b.txt\n"
            "@@ -1 +1 @@\n"
            "-hello\n"
            "+world\n"
        )
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertTrue(result.ok)
        self.assertTrue(result.goal_reached)
        self.assertEqual(result.data["applied"], ["a.py", "b.txt"])
        self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "x = 2\n")
        self.assertEqual((root / "b.txt").read_text(encoding="utf-8"),
                         "world\n")
        for item in result.data["files"]:
            self.assertEqual(len(item["sha256_before"]), 64)
            self.assertEqual(item["line"], 1)
        self.assertFalse((root / ".evoagent_patch_journal").exists())

    def test_patch_diff_multiple_hunks_same_file(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("one = 1\nmid\nthree = 3\n", encoding="utf-8")
        diff = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            "-one = 1\n"
            "+one = 10\n"
            "@@ -3 +3 @@\n"
            "-three = 3\n"
            "+three = 30\n"
        )
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["applied"], ["a.py"])
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "one = 10\nmid\nthree = 30\n")

    def test_patch_diff_hunks_use_new_file_line_numbers(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("one = 1\ntwo = 2\nthree = 3\n", encoding="utf-8")
        diff = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1,2 @@\n"
            "-one = 1\n"
            "+one = 10\n"
            "+extra = 0\n"
            "@@ -3 +4 @@\n"
            "-three = 3\n"
            "+three = 30\n"
        )
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertTrue(result.ok)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "one = 10\nextra = 0\ntwo = 2\nthree = 30\n")

    def test_patch_diff_context_mismatch_aborts_all(self):
        root = self._project()
        first = root / "a.py"
        second = root / "b.py"
        first.write_text("x = 1\n", encoding="utf-8")
        second.write_text("y = 2\n", encoding="utf-8")
        diff = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1 +1 @@\n"
            "-absent\n"
            "+y = 3\n"
        )
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertFalse(result.ok)
        self.assertIn("hunk", result.error)
        self.assertEqual(first.read_text(encoding="utf-8"), "x = 1\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "y = 2\n")
        self.assertFalse((root / ".evoagent_patch_journal").exists())

    def test_patch_diff_rejects_malformed_inputs(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("x = 1\n", encoding="utf-8")
        _, run = coding_patch_tool(str(root))

        both = run({"edits": [{"path": "a.py", "old_text": "x = 1",
                               "new_text": "x = 2"}],
                    "diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"})
        self.assertFalse(both.ok)
        self.assertIn("二选一", both.error)

        neither = run({})
        self.assertFalse(neither.ok)

        devnull = run({"diff": "--- /dev/null\n+++ /dev/null\n"
                               "@@ -0,0 +1 @@\n+x = 1\n"})
        self.assertFalse(devnull.ok)
        self.assertIn("同为 /dev/null", devnull.error)

        marker = run({"diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n"
                              "\\ No newline at end of file\n+x = 2\n"})
        self.assertFalse(marker.ok)
        self.assertIn("No newline", marker.error)

        bad_header = run({"diff": "--- a/a.py\n+++ b/a.py\n@@ bad @@\n-x\n+y\n"})
        self.assertFalse(bad_header.ok)
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\n")

    def test_patch_diff_removed_line_resembling_header(self):
        root = self._project()
        path = root / "notes.txt"
        path.write_text("-- note\nvalue = 1\n", encoding="utf-8")
        diff = (
            "--- a/notes.txt\n"
            "+++ b/notes.txt\n"
            "@@ -1,2 +1,2 @@\n"
            "--- note\n"
            "+// note\n"
            " value = 1\n"
        )
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertTrue(result.ok)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "// note\nvalue = 1\n")

    def test_patch_diff_crlf_input_normalized(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("x = 1\n", encoding="utf-8")
        diff = ("--- a/a.py\r\n+++ b/a.py\r\n@@ -1 +1 @@\r\n"
                "-x = 1\r\n+x = 2\r\n")
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertTrue(result.ok)
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 2\n")

    def test_patch_diff_dry_run_previews(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("x = 1\n", encoding="utf-8")
        diff = ("--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n")
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff, "dry_run": True})

        self.assertTrue(result.ok)
        self.assertTrue(result.data["dry_run"])
        self.assertIn("-x = 1", result.data["files"][0]["diff"])
        self.assertIn("+x = 2", result.data["files"][0]["diff"])
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\n")

    # ---- 新增/删除文件与 hunk 位置容错 ----

    def test_patch_diff_creates_file(self):
        root = self._project()
        diff = ("--- /dev/null\n"
                "+++ b/new_mod.py\n"
                "@@ -0,0 +1,2 @@\n"
                "+x = 1\n"
                "+y = 2\n")
        _, run = coding_patch_tool(str(root))

        preview = run({"diff": diff, "dry_run": True})
        self.assertTrue(preview.ok)
        self.assertIn("+x = 1", preview.data["files"][0]["diff"])
        self.assertEqual(preview.data["files"][0]["sha256_before"], "")
        self.assertFalse((root / "new_mod.py").exists())

        result = run({"diff": diff})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["files"][0]["action"], "created")
        self.assertEqual(result.data["files"][0]["sha256_before"], "")
        self.assertEqual(len(result.data["files"][0]["sha256_after"]), 64)
        self.assertEqual((root / "new_mod.py").read_text(encoding="utf-8"),
                         "x = 1\ny = 2\n")
        self.assertFalse((root / ".evoagent_patch_journal").exists())

    def test_patch_diff_create_guards(self):
        root = self._project()
        (root / "exists.py").write_text("x = 1\n", encoding="utf-8")
        _, run = coding_patch_tool(str(root))

        existing = run({"diff": "--- /dev/null\n+++ b/exists.py\n"
                                "@@ -0,0 +1 @@\n+x = 2\n"})
        self.assertFalse(existing.ok)
        self.assertIn("目标已存在", existing.error)
        self.assertEqual((root / "exists.py").read_text(encoding="utf-8"),
                         "x = 1\n")

        missing_parent = run({"diff": "--- /dev/null\n+++ b/sub/x.py\n"
                                      "@@ -0,0 +1 @@\n+x = 1\n"})
        self.assertFalse(missing_parent.ok)
        self.assertIn("父目录不存在", missing_parent.error)

        with_removal = run({"diff": "--- /dev/null\n+++ b/bad.py\n"
                                    "@@ -1,1 +1,1 @@\n-x\n+y\n"})
        self.assertFalse(with_removal.ok)
        self.assertIn("旧计数必须为 0", with_removal.error)
        self.assertFalse((root / "bad.py").exists())

    def test_patch_diff_deletes_file(self):
        root = self._project()
        path = root / "gone.txt"
        path.write_text("x = 1\ny = 2\n", encoding="utf-8")
        diff = ("--- a/gone.txt\n"
                "+++ /dev/null\n"
                "@@ -1,2 +0,0 @@\n"
                "-x = 1\n"
                "-y = 2\n")
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["files"][0]["action"], "deleted")
        self.assertEqual(result.data["files"][0]["sha256_after"], "")
        self.assertFalse(path.exists())
        self.assertFalse((root / ".evoagent_patch_journal").exists())

    def test_patch_diff_delete_requires_full_removal(self):
        root = self._project()
        path = root / "gone.txt"
        path.write_text("x = 1\ny = 2\n", encoding="utf-8")
        diff = ("--- a/gone.txt\n"
                "+++ /dev/null\n"
                "@@ -1 +0,0 @@\n"
                "-x = 1\n")
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertFalse(result.ok)
        self.assertIn("未覆盖全部内容", result.error)
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\ny = 2\n")
        self.assertFalse((root / ".evoagent_patch_journal").exists())

    def test_patch_diff_line_number_disambiguates(self):
        root = self._project()
        path = root / "dup.txt"
        path.write_text("a = 0\nb = 0\nc = 0\nvalue = 1\nd = 0\nvalue = 1\n",
                        encoding="utf-8")
        diff = ("--- a/dup.txt\n+++ b/dup.txt\n@@ -6 +6 @@\n"
                "-value = 1\n+value = 2\n")
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["files"][0]["line"], 6)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "a = 0\nb = 0\nc = 0\nvalue = 1\nd = 0\nvalue = 2\n")

    def test_patch_diff_ambiguous_without_line_hit_rejected(self):
        root = self._project()
        path = root / "dup.txt"
        original = "a = 0\nb = 0\nc = 0\nvalue = 1\nd = 0\nvalue = 1\n"
        path.write_text(original, encoding="utf-8")
        diff = ("--- a/dup.txt\n+++ b/dup.txt\n@@ -3 +3 @@\n"
                "-value = 1\n+value = 2\n")
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertFalse(result.ok)
        self.assertIn("无法唯一消歧", result.error)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_patch_diff_fuzz_trims_context(self):
        root = self._project()
        path = root / "fuzz.txt"
        path.write_text("start = 0\ntarget = 1\nend = 0\n", encoding="utf-8")
        diff = ("--- a/fuzz.txt\n+++ b/fuzz.txt\n@@ -1,3 +1,3 @@\n"
                " stale_top\n"
                "-target = 1\n"
                "+target = 2\n"
                " stale_bottom\n")
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertTrue(result.ok)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "start = 0\ntarget = 2\nend = 0\n")

    def test_patch_diff_fuzz_beyond_limit_rejected(self):
        root = self._project()
        path = root / "fuzz.txt"
        original = "start = 0\ntarget = 1\nend = 0\n"
        path.write_text(original, encoding="utf-8")
        diff = ("--- a/fuzz.txt\n+++ b/fuzz.txt\n@@ -1,5 +1,5 @@\n"
                " stale1\n"
                " stale2\n"
                " stale3\n"
                "-target = 1\n"
                "+target = 2\n"
                " stale4\n")
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertFalse(result.ok)
        self.assertIn("上下文未命中", result.error)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_patch_diff_mixed_create_modify_delete(self):
        root = self._project()
        modified = root / "mod.txt"
        removed = root / "gone.txt"
        modified.write_text("keep\n", encoding="utf-8")
        removed.write_text("bye\n", encoding="utf-8")
        diff = ("--- a/mod.txt\n+++ b/mod.txt\n@@ -1 +1 @@\n"
                "-keep\n+kept\n"
                "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n"
                "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n")
        _, run = coding_patch_tool(str(root))

        result = run({"diff": diff})

        self.assertTrue(result.ok)
        actions = [row["action"] for row in result.data["files"]]
        self.assertEqual(actions, ["modified", "created", "deleted"])
        self.assertEqual(modified.read_text(encoding="utf-8"), "kept\n")
        self.assertEqual((root / "new.txt").read_text(encoding="utf-8"),
                         "new\n")
        self.assertFalse(removed.exists())
        self.assertFalse((root / ".evoagent_patch_journal").exists())

    def test_patch_diff_mixed_failure_rolls_back_all(self):
        root = self._project()
        modified = root / "mod.txt"
        removed = root / "gone.txt"
        modified.write_text("keep\n", encoding="utf-8")
        removed.write_text("bye\n", encoding="utf-8")
        diff = ("--- a/mod.txt\n+++ b/mod.txt\n@@ -1 +1 @@\n"
                "-keep\n+kept\n"
                "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n"
                "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n")
        original_unlink = Path.unlink

        def fail_unlink(self, *args, **kwargs):
            if self.name == "gone.txt":
                raise OSError("locked")
            return original_unlink(self, *args, **kwargs)

        _, run = coding_patch_tool(str(root))
        with mock.patch.object(Path, "unlink", fail_unlink):
            result = run({"diff": diff})

        self.assertFalse(result.ok)
        self.assertIn("写盘失败并回滚", result.error)
        self.assertTrue(result.independently_verified)
        self.assertEqual(modified.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((root / "new.txt").exists())
        self.assertEqual(removed.read_text(encoding="utf-8"), "bye\n")
        self.assertFalse((root / ".evoagent_patch_journal").exists())

    # ---- 事务日志与中断恢复 ----

    def _plant_journal(self, root, files, status="pending",
                       name="patch-test-0001.json"):
        jdir = root / ".evoagent_patch_journal"
        jdir.mkdir(exist_ok=True)
        path = jdir / name
        path.write_text(json.dumps({
            "version": 1, "created": "2026-10-23T00:00:00",
            "status": status, "files": files}), encoding="utf-8")
        return path

    def _palette(self, rel="a.py", content="x = 2\n"):
        before = "x = 1\n"
        return {"path": rel,
                "sha256_before": hashlib.sha256(
                    before.encode("utf-8")).hexdigest(),
                "sha256_after": hashlib.sha256(
                    content.encode("utf-8")).hexdigest(),
                "original": before}

    def test_recover_restores_interrupted_patch(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("x = 2\n", encoding="utf-8")
        self._plant_journal(root, [self._palette()])
        _, run = coding_patch_recover_tool(str(root))

        result = run({})

        self.assertTrue(result.ok)
        self.assertTrue(result.goal_reached)
        self.assertEqual(result.data["restored"], 1)
        self.assertEqual(result.data["conflicts"], 0)
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\n")
        self.assertFalse((root / ".evoagent_patch_journal").exists())

        again = run({})
        self.assertTrue(again.ok)
        self.assertEqual(again.data["journals"], [])

    def test_recover_conflict_is_not_overwritten(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("someone else\n", encoding="utf-8")
        self._plant_journal(root, [self._palette()])
        _, run = coding_patch_recover_tool(str(root))

        result = run({})

        self.assertFalse(result.ok)
        self.assertFalse(result.goal_reached)
        self.assertEqual(result.data["conflicts"], 1)
        self.assertEqual(
            result.data["journals"][0]["files"][0]["action"], "conflict")
        self.assertEqual(path.read_text(encoding="utf-8"), "someone else\n")
        self.assertTrue((root / ".evoagent_patch_journal").is_dir())

    def test_recover_dry_run_does_not_touch(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("x = 2\n", encoding="utf-8")
        self._plant_journal(root, [self._palette()])
        _, run = coding_patch_recover_tool(str(root))

        result = run({"dry_run": True})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["restored"], 1)
        self.assertTrue(result.data["dry_run"])
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 2\n")
        self.assertTrue((root / ".evoagent_patch_journal").is_dir())

    def test_recover_committed_keeps_result_and_corrupt_skipped(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("x = 2\n", encoding="utf-8")
        self._plant_journal(root, [self._palette()], status="committed",
                            name="patch-committed.json")
        bad = root / ".evoagent_patch_journal" / "patch-bad.json"
        bad.write_text("{not json", encoding="utf-8")
        _, run = coding_patch_recover_tool(str(root))

        result = run({})

        self.assertFalse(result.ok)
        self.assertEqual(len(result.data["errors"]), 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 2\n")
        self.assertFalse(
            (root / ".evoagent_patch_journal" / "patch-committed.json").exists())
        self.assertTrue(bad.exists())

    def test_recover_already_before_clears_journal(self):
        root = self._project()
        path = root / "a.py"
        path.write_text("x = 1\n", encoding="utf-8")
        self._plant_journal(root, [self._palette()])
        _, run = coding_patch_recover_tool(str(root))

        result = run({})

        self.assertTrue(result.ok)
        self.assertEqual(
            result.data["journals"][0]["files"][0]["action"], "already_before")
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\n")
        self.assertFalse((root / ".evoagent_patch_journal").exists())

    def _create_row(self, rel="new.py", content="x = 1\n"):
        return {"path": rel, "sha256_before": "",
                "sha256_after": hashlib.sha256(
                    content.encode("utf-8")).hexdigest(),
                "original": "", "existed": False, "deleted": False}

    def _delete_row(self, rel="gone.py", original="x = 1\n"):
        return {"path": rel,
                "sha256_before": hashlib.sha256(
                    original.encode("utf-8")).hexdigest(),
                "sha256_after": "", "original": original,
                "existed": True, "deleted": True}

    def test_recover_removes_created_file(self):
        root = self._project()
        path = root / "new.py"
        path.write_text("x = 1\n", encoding="utf-8")
        self._plant_journal(root, [self._create_row()])
        _, run = coding_patch_recover_tool(str(root))

        result = run({})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["restored"], 1)
        self.assertFalse(path.exists())
        self.assertFalse((root / ".evoagent_patch_journal").exists())

    def test_recover_restores_deleted_file(self):
        root = self._project()
        path = root / "gone.py"
        self._plant_journal(root, [self._delete_row()])
        _, run = coding_patch_recover_tool(str(root))

        result = run({})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["restored"], 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "x = 1\n")
        self.assertFalse((root / ".evoagent_patch_journal").exists())

    def test_recover_created_file_conflict_not_removed(self):
        root = self._project()
        path = root / "new.py"
        path.write_text("someone else\n", encoding="utf-8")
        self._plant_journal(root, [self._create_row()])
        _, run = coding_patch_recover_tool(str(root))

        result = run({})

        self.assertFalse(result.ok)
        self.assertEqual(result.data["conflicts"], 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "someone else\n")
        self.assertTrue((root / ".evoagent_patch_journal").is_dir())

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
        self.assertEqual(pack["coding_patch"][0].side_effect_level, "local")
        self.assertEqual(pack["coding_patch"][0].risk_class, "high")
        self.assertEqual(pack["coding_patch_recover"][0].side_effect_level,
                         "local")
        self.assertEqual(pack["coding_patch_recover"][0].risk_class, "high")
        self.assertEqual(pack["coding_symbols"][0].side_effect_level, "none")

    def test_install_pack_and_manifest(self):
        root = self._project()
        registry = ToolRegistry()
        manifest = install_coding_pack(registry, str(root))

        self.assertEqual(manifest.pack_id, "coding_pack")
        self.assertEqual(manifest.version, "1.5.1")
        self.assertEqual(manifest.rollback_version, "1.5.0")
        self.assertEqual(len(manifest.tool_ids), 8)
        self.assertIn("coding_patch", manifest.tool_ids)
        self.assertIn("coding_patch_recover", manifest.tool_ids)
        self.assertEqual(set(manifest.as_dict()["tool_ids"]),
                         set(CODING_TOOL_IDS))
        self.assertEqual(CODING_PACK, manifest)
        listed = {c.tool_id for c in registry.available_tools()}
        self.assertTrue(set(CODING_TOOL_IDS).issubset(listed))
        self.assertEqual(registry.contract("coding_edit").risk_class, "high")
        self.assertEqual(registry.contract("coding_symbols").risk_class, "low")


if __name__ == "__main__":
    unittest.main()
