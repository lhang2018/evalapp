# -*- coding: utf-8 -*-
"""ALN-18-L 跨文件语义与语言服务门禁。

负向优先：歧义/动态/冲突一律澄清不猜测；语言服务不可用不判成功；
字符串与注释中的同形词不得被改写。
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from evoagent.tools.coding import coding_patch_tool
from evoagent.tools.refactor import (
    LANGUAGE_SERVICE_VERSION,
    Diagnostic,
    analyze_rename,
    collect_diagnostics,
    compare_diagnostics,
    language_service_status,
)


def make_project(root: Path, files: dict) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


STARTER = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(x):\n    return x + 1\n",
    "pkg/main.py": (
        "from pkg.util import helper\n"
        "\n"
        "\n"
        "def run(v):\n"
        "    # helper in a comment must stay\n"
        "    label = \"helper\"\n"
        "    return helper(v) + len(label)\n"
    ),
}


class LanguageServiceTest(unittest.TestCase):
    def test_status_pins_version_and_declares_not_semantic(self):
        status = language_service_status()
        self.assertTrue(status.available)
        self.assertEqual(status.version, LANGUAGE_SERVICE_VERSION)
        self.assertFalse(status.is_semantic)  # 未实现 LSP，不宣称支持
        self.assertIn("LSP", status.reason)


class DiagnosticsTest(unittest.TestCase):
    def test_clean_project_has_no_diagnostics(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp), STARTER)
            self.assertEqual(collect_diagnostics(root), ())

    def test_syntax_error_is_reported(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp), {"bad.py": "def broken(:\n"})
            diags = collect_diagnostics(root)
            self.assertEqual(len(diags), 1)
            self.assertEqual(diags[0].path, "bad.py")
            self.assertGreater(diags[0].line, 0)

    def test_before_after_comparison_classifies(self):
        before = [Diagnostic("a.py", 1, 1, "old")]
        after = [Diagnostic("a.py", 1, 1, "new")]
        delta = compare_diagnostics(before, after)
        self.assertEqual([d.message for d in delta.new], ["new"])
        self.assertEqual([d.message for d in delta.fixed], ["old"])
        self.assertEqual(delta.remaining, ())

        same = compare_diagnostics(before, before)
        self.assertEqual(same.new, ())
        self.assertEqual(same.fixed, ())
        self.assertEqual(len(same.remaining), 1)


class RenameAnalysisTest(unittest.TestCase):
    def test_cross_file_rename_plan_covers_all_modules(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp), STARTER)
            decision = analyze_rename(root, "helper", "compute")
            self.assertEqual(decision.status, "ok", decision.questions)
            self.assertEqual(decision.definition_modules, ("pkg/util.py",))
            self.assertEqual(decision.references, 3)
            touched = sorted({e["path"] for e in decision.edits})
            self.assertEqual(touched, ["pkg/main.py", "pkg/util.py"])

    def test_apply_plan_with_real_patch_tool(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp), STARTER)
            before = collect_diagnostics(root)
            decision = analyze_rename(root, "helper", "compute")
            _, run = coding_patch_tool(str(root))
            result = run({"edits": list(decision.edits)})
            self.assertTrue(result.ok, result.error)
            util = (root / "pkg/util.py").read_text(encoding="utf-8")
            main = (root / "pkg/main.py").read_text(encoding="utf-8")
            self.assertIn("def compute(x):", util)
            self.assertIn("from pkg.util import compute", main)
            self.assertIn("return compute(v)", main)
            # 注释与字符串中的同形词必须原样保留
            self.assertIn("# helper in a comment must stay", main)
            self.assertIn('"helper"', main)
            after = collect_diagnostics(root)
            self.assertEqual(after, ())
            self.assertEqual(compare_diagnostics(before, after).new, ())

    def test_symbol_defined_in_multiple_modules_needs_clarification(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp), {
                "a.py": "def helper():\n    return 1\n",
                "b.py": "def helper():\n    return 2\n",
            })
            decision = analyze_rename(root, "helper", "compute")
            self.assertEqual(decision.status, "needs_clarification")
            self.assertEqual(decision.edits, ())
            self.assertTrue(any("多个模块" in q for q in decision.questions))

    def test_star_import_blocks_rename(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp), {
                "util.py": "def helper():\n    return 1\n",
                "main.py": "from util import *\n\nx = helper()\n",
            })
            decision = analyze_rename(root, "helper", "compute")
            self.assertEqual(decision.status, "needs_clarification")
            self.assertTrue(any("星号导入" in q for q in decision.questions))

    def test_dynamic_reference_blocks_rename(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp), {
                "util.py": "def helper():\n    return 1\n",
                "main.py": (
                    "import util\n\n"
                    "def pick(obj):\n"
                    "    return getattr(obj, 'helper')\n"
                ),
            })
            decision = analyze_rename(root, "helper", "compute")
            self.assertEqual(decision.status, "needs_clarification")
            self.assertTrue(any("动态引用" in q for q in decision.questions))

    def test_new_name_conflict_blocks_rename(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp), STARTER)
            decision = analyze_rename(root, "helper", "run")  # main.py 已有 run
            self.assertEqual(decision.status, "needs_clarification")
            self.assertTrue(any("冲突" in q for q in decision.questions))

    def test_missing_symbol_needs_clarification(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp), STARTER)
            decision = analyze_rename(root, "nonexistent", "compute")
            self.assertEqual(decision.status, "needs_clarification")
            self.assertTrue(any("找不到符号" in q for q in decision.questions))

    def test_identity_and_empty_names_rejected(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp), STARTER)
            self.assertEqual(analyze_rename(root, "helper", "helper").status,
                             "needs_clarification")
            self.assertEqual(analyze_rename(root, "", "x").status,
                             "needs_clarification")


if __name__ == "__main__":
    unittest.main()
