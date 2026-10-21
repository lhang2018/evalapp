"""全台账行号漂移扫描器自身的测试。

背景：台账里大量使用 `文件:行号` 引用。代码一改，这些引用就可能指向
别的语句 —— 证据链静默失效，而台账本身看不出任何异常。

本测试锁定两件事：
  1. 当前所有台账**没有漂移**（0 问题）
  2. 扫描器**确实能发现漂移** —— 用故意构造的错误引用验证它会报出来
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import scan_all_ledgers as sal  # noqa: E402

DOCS = ["README.md", "IMPLEMENTATION.md", "FEATURES.md", "FEATURE_SPLIT.md",
        "GAP_ANALYSIS.md", "ROADMAP.md", "progress.md", "task_plan.md"]


class TestNoDrift(unittest.TestCase):
    """当前台账必须无漂移。"""

    def test_all_ledgers_have_no_drift(self):
        rep = sal.scan(DOCS)
        self.assertEqual(
            rep["problems"], [],
            f"台账存在 {len(rep['problems'])} 处引用问题："
            f"{[(p['doc'], p['doc_line'], p['kind']) for p in rep['problems'][:5]]}")

    def test_scan_covers_known_references(self):
        """扫描必须真的核对了引用，而不是空跑。"""
        rep = sal.scan(DOCS)
        self.assertGreater(rep["checked"] + rep.get("anchor_checked", 0), 40,
                           "引用核对数过低 —— 扫描器可能失效")
        self.assertGreater(rep["bare_checked"], 20,
                           "裸引用核对数过低 —— 扫描器可能失效")


class TestScannerCatchesDrift(unittest.TestCase):
    """扫描器必须能发现漂移 —— 否则它只是装饰品。"""

    def setUp(self):
        self.tmp = os.path.join(ROOT, "outputs", "_drift_probe.md")
        os.makedirs(os.path.dirname(self.tmp), exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _scan_one(self, content: str) -> list:
        with open(self.tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        return sal.scan(["outputs/_drift_probe.md"])["problems"]

    def _line_of(self, rel_path: str, needle: str) -> int:
        """动态定位符号当前所在行。

        刻意不硬编码行号：并行开发中任何人在文件里加几行，
        硬编码的断言就会假失败。测试要验证的是**扫描器逻辑**，
        不是某个具体行号。
        """
        full = os.path.join(ROOT, rel_path)
        with open(full, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if needle in line:
                    return i
        self.fail(f"在 {rel_path} 中找不到 {needle}")

    def test_catches_symbol_drift(self):
        """符号名对但行号指向别处。"""
        good = self._line_of("evoagent/core/controller.py", "def submit_task")
        # 故意指向一个不含该符号的行
        bad_line = good + 5
        problems = self._scan_one(
            f"见 `submit_task`（`evoagent/core/controller.py:{bad_line}`）\n")
        kinds = {p["kind"] for p in problems}
        self.assertIn("symbol_drift", kinds)
        self.assertTrue(any(p.get("suggest") for p in problems),
                        "漂移时应给出建议的新行号")

    def test_catches_missing_path(self):
        problems = self._scan_one(
            "见 `x`（`no/such/file.py:10`）\n")
        kinds = {p["kind"] for p in problems}
        self.assertIn("path_missing", kinds)

    def test_catches_out_of_range(self):
        problems = self._scan_one(
            "见 `x`（`evoagent/core/controller.py:99999`）\n")
        kinds = {p["kind"] for p in problems}
        self.assertIn("line_out_of_range", kinds)

    def test_catches_bare_pointing_to_blank(self):
        """裸引用指向空行 —— 常见的漂移形态。"""
        problems = self._scan_one(
            "见 `evoagent/core/controller.py:9999`\n")
        kinds = {p["kind"] for p in problems}
        self.assertIn("bare_line_out_of_range", kinds)

    def test_clean_reference_passes(self):
        """正确的引用不应报错 —— 否则就是误报。"""
        good = self._line_of("evoagent/core/controller.py", "def submit_task")
        problems = self._scan_one(
            f"见 `submit_task`（`evoagent/core/controller.py:{good}`）\n")
        self.assertEqual(problems, [], f"正确引用被误报：{problems}")

    def test_anchor_form_survives_line_shift(self):
        """**符号锚定形式的核心价值**：文件加行后引用依然有效。

        平行开发中任何人在文件里加几行，`file.py:123` 就指向别的语句了
        （实测 controller.py 的 submit_task 因并行工作流的 F-03 改动
        从 77 行移到 80 行）。锚定形式 `file.py::symbol` 不受影响。
        """
        good = self._line_of("evoagent/core/controller.py", "def submit_task")
        # 用一个当前必然不正确的行号 + 锚定形式
        problems = self._scan_one(
            "见 `submit_task`（`evoagent/core/controller.py::submit_task`）\n")
        self.assertEqual(problems, [],
                         f"锚定引用被误报（符号在第 {good} 行）：{problems}")

    def test_catches_anchor_symbol_missing(self):
        """锚定引用指向不存在的符号 —— 必须报出。"""
        problems = self._scan_one(
            "见 `x`（`evoagent/core/controller.py::no_such_symbol_xyz`）\n")
        kinds = {p["kind"] for p in problems}
        self.assertIn("anchor_symbol_missing", kinds)


class TestCriticalClaimsStillHold(unittest.TestCase):
    """台账最核心的几条指控，必须与代码现状一致。

    这些是「修记账」的根据。它们一旦被后续改动推翻，台账就失去意义。
    """

    def test_error_object_is_implemented(self):
        """F-01 已落地 —— 台账若还写「零实现」就是错的。"""
        path = os.path.join(ROOT, "evoagent", "core", "errors.py")
        self.assertTrue(os.path.exists(path),
                        "统一错误对象模块不存在，台账的 ✅ 判定需重新核实")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for field in ("error_code", "retryable", "correlation_id",
                      "related_event_id", "required_action", "safe_to_repeat"):
            self.assertIn(field, src, f"错误对象缺字段 {field}")

    def test_error_object_wired_into_api(self):
        """错误对象必须在 API 层真正接入，而不只是模块存在。"""
        path = os.path.join(ROOT, "evoagent", "api", "server.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("normalize_error_payload", src,
                      "server.py 未接入统一错误对象")

    def test_impact_analysis_still_unreferenced(self):
        """C-09 的核心指控：影响分析零调用方。"""
        from verify_claims import build_symbol_index
        idx = build_symbol_index()
        self.assertIn("impact_analysis", idx)
        self.assertEqual(idx["impact_analysis"]["refs"], 0,
                         "impact_analysis 现在有引用了 —— C-09 判定需更新")

    def test_register_problem_still_unreferenced(self):
        """E-07 的核心指控：问题队列永不读取。"""
        from verify_claims import build_symbol_index
        idx = build_symbol_index()
        self.assertIn("register_problem", idx)
        self.assertEqual(idx["register_problem"]["refs"], 0,
                         "register_problem 现在有引用了 —— E-07 判定需更新")

    def test_builtin_tools_still_do_not_set_health_check(self):
        """R-08 的核心指控：内置工具零设置 health_check → 面板永远全绿。"""
        path = os.path.join(ROOT, "evoagent", "tools", "builtin.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("health_check", src,
                         "内置工具现在设置了 health_check —— R-08 判定需更新")

    def test_experts_still_do_not_consume_quality_metrics(self):
        """C-03 的核心指控：quality_metrics 零消费点。"""
        path = os.path.join(ROOT, "evoagent", "experts", "registry.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        # 只允许定义，不允许读取
        occurrences = [ln for ln in src.split("\n") if "quality_metrics" in ln]
        for ln in occurrences:
            self.assertIn(": List[str] = field(",
                          f"quality_metrics 出现了读取点，需更新 C-03 判定：{ln}")


if __name__ == "__main__":
    unittest.main()
