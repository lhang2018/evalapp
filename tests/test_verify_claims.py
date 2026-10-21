"""台账声明校验器自身的测试。

核心目的：**防止校验器退化成"永远通过"**。
第一版校验器就犯过这个错 —— 主题行解析返回 0 行、状态计数全为 0，
但它报的是"全部通过"。所以这里必须用**故意构造的错误台账**来验证
校验器确实会失败。

覆盖：
  1. 真实台账当前必须通过
  2. 注入 5 类假声明后必须报错（路径错 / 行号漂移 / 符号其实有引用 /
     声称缺席却存在 / 状态计数不符）
  3. 计数口径：合并行展开后等于 89 主题
  4. 符号索引的基本正确性
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import verify_claims as vc  # noqa: E402

LEDGER = os.path.join(ROOT, "IMPLEMENTATION.md")


def read_ledger() -> str:
    with open(LEDGER, "r", encoding="utf-8") as fh:
        return fh.read()


class TestRealLedgerPasses(unittest.TestCase):
    """真实台账必须通过校验 —— 这是防退化的基准线。"""

    @classmethod
    def setUpClass(cls):
        cls.text = read_ledger()
        cls.rows = vc.parse_ledger(cls.text)
        cls.index = vc.build_symbol_index()

    def test_parses_all_theme_rows(self):
        """必须解析出 84 行（89 主题，5 个主题用合并行表示）。

        这是第一版的致命 bug：解析返回 0 行却报"全部通过"。
        """
        self.assertEqual(len(self.rows), 84,
                         "主题行解析数不对；若为 0 说明解析器坏了，"
                         "校验结果不可信")
        self.assertTrue(all(r["theme"] for r in self.rows))

    def test_status_counts_expand_to_89_themes(self):
        counts = vc.count_statuses(self.rows)
        total = sum(counts.values())
        self.assertEqual(total, 89, f"展开后主题数应为 89，实际 {total}")
        self.assertEqual(counts.get(vc.OK, 0), 34)
        self.assertEqual(counts.get(vc.PART, 0), 36)
        self.assertEqual(counts.get(vc.MISS, 0), 19)

    def test_citations_resolve(self):
        problems = vc.check_citations(self.text)
        self.assertEqual(problems, [], f"存在无效引用：{problems[:3]}")

    def test_semantic_citations_resolve(self):
        problems = vc.check_semantic_citations(self.text)
        self.assertEqual(problems, [],
                         f"存在行号漂移的引用：{problems[:3]}")

    def test_every_ok_row_has_verifiable_evidence(self):
        """每个 ✅ 行都必须有可机器验证的证据。

        DoD 三条硬门槛：有实现逻辑 + 有产生它的代码路径 + 有走通该路径的测试。
        """
        problems, weak = vc.check_positive_claims(self.rows, self.index,
                                                  strict=False)
        self.assertEqual(weak, [],
                         f"{len(weak)} 个 ✅ 行缺少可校验证据："
                         f"{[w['theme'] for w in weak][:5]}")

    def test_full_check_passes(self):
        rows = vc.parse_ledger(self.text)
        index = vc.build_symbol_index()
        dead = vc.extract_dead_claims(rows)
        absent = vc.extract_absent_claims(rows)
        problems = []
        problems += vc.check_citations(self.text)
        problems += vc.check_semantic_citations(self.text)
        problems += vc.check_dead_claims(index, dead)
        problems += vc.check_absent_claims(absent)
        problems += vc.check_positive_claims(rows, index)[0]
        problems += vc.check_status_ledger(rows, self.text)
        self.assertEqual(problems, [],
                         f"台账声明未通过机器校验：{problems[:3]}")


class TestCheckerCatchesFalseClaims(unittest.TestCase):
    """**必须证明校验器会失败** —— 否则它只是装饰品。"""

    def setUp(self):
        self.text = read_ledger()

    def _problems(self, text: str) -> list:
        rows = vc.parse_ledger(text)
        index = vc.build_symbol_index()
        problems = []
        problems += vc.check_citations(text)
        problems += vc.check_semantic_citations(text)
        problems += vc.check_dead_claims(index,
                                         vc.extract_dead_claims(rows))
        problems += vc.check_absent_claims(vc.extract_absent_claims(rows))
        problems += vc.check_positive_claims(rows, index)[0]
        problems += vc.check_status_ledger(rows, text)
        return problems

    def test_catches_missing_path(self):
        bad = self.text.replace(
            "| R-12 外部副作用账本 | ⬜ |",
            "| R-12 外部副作用账本 | ⬜ | 见 nonexistent/file.py:9999 |", 1)
        kinds = {p["kind"] for p in self._problems(bad)}
        self.assertIn("citation_path_missing", kinds)

    def test_catches_line_drift(self):
        """符号名对但行号指向别处 —— 语义引用校验必须抓住。

        刻意不硬编码行号：并行开发中文件行号会移动，
        硬编码会让测试假失败。测试要验证的是**校验器逻辑**。
        """
        import re as _re
        m = _re.search(
            r"`([A-Za-z_][A-Za-z0-9_]*)`（`([A-Za-z0-9_\-./]+\.py):(\d+)`）",
            self.text)
        if not m:
            self.skipTest("台账中暂无 `符号`（`文件:行号`）形态的引用")
        sym, path_hint, num = m.group(1), m.group(2), m.group(3)
        # 找一个**行号在范围内但不含该符号**的行 —— 越界会被另一个判据先抓到
        wrong = self._a_line_without(path_hint, sym, int(num))
        if wrong is None:
            self.skipTest(f"{path_hint} 中找不到不含 {sym} 的行")
        old = f"`{sym}`（`{path_hint}:{num}`）"
        bad = self.text.replace(old, f"`{sym}`（`{path_hint}:{wrong}`）", 1)
        self.assertNotEqual(bad, self.text, "注入失败：原文未找到待替换串")
        problems = self._problems(bad)
        kinds = {p["kind"] for p in problems}
        self.assertIn("semantic_citation_mismatch", kinds)

    def _a_line_without(self, path_hint: str, sym: str, near: int) -> int | None:
        """在 path_hint 里找一个不含 sym 的行号（优先靠近 near）。"""
        full = os.path.join(ROOT, path_hint)
        if not os.path.exists(full):
            return None
        with open(full, "r", encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        for delta in (1, 2, 3, -1, -2, -3, 5, -5, 10, -10):
            idx = near + delta
            if 1 <= idx <= len(lines) and sym not in lines[idx - 1]:
                return idx
        return None

    def test_catches_anchor_symbol_missing(self):
        """锚定引用指向不存在的符号 —— 必须报出。"""
        import re as _re
        m = _re.search(r"`([A-Za-z0-9_\-./]+\.py)::([A-Za-z_][A-Za-z0-9_]*)`",
                       self.text)
        if not m:
            self.skipTest("台账中暂无符号锚定引用")
        path_hint = m.group(1)
        old = f"`{path_hint}::{m.group(2)}`"
        bad = self.text.replace(old, f"`{path_hint}::no_such_symbol_xyz`", 1)
        self.assertNotEqual(bad, self.text, "注入失败")
        problems = self._problems(bad)
        kinds = {p["kind"] for p in problems}
        self.assertIn("anchor_symbol_missing", kinds)

    def test_catches_dead_claim_that_is_referenced(self):
        """声称「零调用」但符号实际有引用 —— 判定反了。"""
        bad = self.text.replace(
            "| C-09 能力依赖图与影响分析 | 🔹 |",
            "| C-09 能力依赖图与影响分析 | 🔹 | 附带：`PermissionDecision` 零调用方 |",
            1)
        self.assertNotEqual(bad, self.text, "注入失败")
        kinds = {p["kind"] for p in self._problems(bad)}
        self.assertIn("dead_claim_but_referenced", kinds)

    def test_catches_absent_claim_that_exists(self):
        """声称「不存在」但符号实际存在。"""
        bad = self.text.replace(
            "| M-08 记忆冲突与事实更新 | ⬜ |",
            "| M-08 记忆冲突与事实更新 | ⬜ | 另称 `GoalOrigin` 全仓零命中 |",
            1)
        self.assertNotEqual(bad, self.text, "注入失败")
        kinds = {p["kind"] for p in self._problems(bad)}
        self.assertIn("absent_claim_but_present", kinds)

    def test_catches_status_count_mismatch(self):
        bad = self.text.replace("| ✅ 已实现 | 34 |", "| ✅ 已实现 | 40 |", 1)
        self.assertNotEqual(bad, self.text, "注入失败")
        kinds = {p["kind"] for p in self._problems(bad)}
        self.assertIn("status_count_mismatch", kinds)

    def test_catches_ok_row_without_evidence(self):
        """把一个证据充分的 ✅ 行改成无证据散文，必须被正面证据门禁抓住。"""
        bad = self.text.replace(
            "| M-09 记忆写入门禁 | ✅ |", "| M-09 记忆写入门禁 | ✅ | 很好 |", 1)
        self.assertNotEqual(bad, self.text, "注入失败")
        problems, weak = vc.check_positive_claims(
            vc.parse_ledger(bad), vc.build_symbol_index(), strict=False)
        themes = {w["theme"] for w in weak}
        self.assertTrue(any("M-09" in th for th in themes),
                        f"证据门禁未发现 M-09 失去证据，实际 weak={themes}")


class TestSymbolIndex(unittest.TestCase):
    """符号索引本身的正确性。"""

    @classmethod
    def setUpClass(cls):
        cls.index = vc.build_symbol_index()

    def test_finds_known_symbols(self):
        for sym in ("submit_task", "Task", "deploy_candidate",
                    "impact_analysis", "register_problem"):
            self.assertIn(sym, self.index, f"{sym} 未被索引")

    def test_impact_analysis_has_no_reference(self):
        """C-09 的核心指控：影响分析零调用方。"""
        self.assertEqual(self.index["impact_analysis"]["refs"], 0)

    def test_register_problem_has_no_reference(self):
        """E-07 的核心指控：问题队列永不读取。"""
        self.assertEqual(self.index["register_problem"]["refs"], 0)

    def test_submit_task_has_path_and_test(self):
        """R-01 的正面证据：既有代码路径也有测试调用。"""
        v = self.index["submit_task"]
        self.assertGreater(v["refs"], 0)
        self.assertGreater(v["test_calls"], 0)

    def test_deploy_candidate_lacks_direct_test(self):
        """E-09 的已知缺口：门禁分支本身无直接测试调用。

        这个断言是**回归锁** —— 一旦有人补了测试，这条会失败，
        提醒更新台账里 E-09 的说明。
        """
        self.assertEqual(self.index["deploy_candidate"]["test_calls"], 0,
                         "deploy_candidate 现在有直接测试调用了 —— "
                         "请更新 IMPLEMENTATION.md 中 E-09 的说明")


class TestTestCountCheck(unittest.TestCase):
    """测试数量漂移检查。

    存在的理由：并行开发会持续加测试，台账里的数字必然滞后。
    与其靠人记得更新，不如让工具报出来。
    """

    def test_collects_stated_counts(self):
        counts = vc.collect_stated_counts(
            "测试基线：Python 443 项 + 客户端 128 项。\n"
            "python -m pytest tests/ -q  # 443 passed\n")
        self.assertIn(443, counts)

    def test_takes_max_as_current(self):
        """历史基线数字会并存，取最大者作为当前口径。"""
        counts = vc.collect_stated_counts(
            "Python 397 项（当轮基线；当前基线 443）\n"
            "首轮基线 358\n")
        self.assertEqual(max(counts), 443)

    def test_detects_drift(self):
        """台账数字与实测不符时必须报出。"""
        actual = vc.measure_test_count()
        if actual is None:
            self.skipTest("无法实测测试数量")
        stale = f"Python {actual + 7} 项"
        problems = vc.check_test_count(stale)
        kinds = {p["kind"] for p in problems}
        self.assertIn("test_count_drift", kinds)

    def test_passes_when_matching(self):
        actual = vc.measure_test_count()
        if actual is None:
            self.skipTest("无法实测测试数量")
        problems = vc.check_test_count(f"Python {actual} 项")
        self.assertEqual(problems, [],
                         f"数字一致却报错：{problems}")


class TestClaimExtraction(unittest.TestCase):
    """声明抽取：必须从正文自动取出，而不是硬编码列表。"""

    def setUp(self):
        self.rows = vc.parse_ledger(read_ledger())

    def test_split_row_respects_escaped_pipe(self):
        """转义竖线是行内代码的一部分，不是列分隔符。

        台账 E-07 行含 `grep "capability_gap\\|质量退化\\|成本异常"`。
        用朴素 split("|") 会把该行切成 7 段，说明列被从中间截断 ——
        后半段的声明（register_problem 永不读取）就永远抽不出来。
        """
        cells = vc.split_row(
            '| E-07 x | ⬜ | `grep "a\\|b"` 零命中（`c\\|d`） |')
        self.assertEqual(len(cells), 5, f"切分段数不对：{cells}")
        self.assertEqual(cells[3], '`grep "a|b"` 零命中（`c|d`）')

    def test_e07_note_not_truncated(self):
        """E-07 的说明列必须完整（含 register_problem 声明）。"""
        e07 = [r for r in self.rows if r["theme"].startswith("E-07")]
        self.assertEqual(len(e07), 1)
        self.assertIn("register_problem", e07[0]["note"],
                      "E-07 说明列被截断 —— 表格切分没处理转义竖线")
        self.assertIn("四类信号", e07[0]["note"])

    def test_extracts_dead_claims(self):
        """声明必须能从正文自动抽出，且绑定到正确的主体。

        抽取有两条硬要求：
          1. 声明措辞要紧贴主体（`link`…零调用点），
             否则会绑到邻近的另一个符号上
          2. 主体必须是可索引的函数/类，字典键无法验证
        """
        claims = vc.extract_dead_claims(self.rows)
        syms = {c["symbol"] for c in claims}
        self.assertIn("link", syms, "M-08 的 link 零调用声明未被抽出")
        self.assertIn("register_problem", syms,
                      "E-07 的 register_problem 永不读取声明未被抽出")
        # 不得把字典键/字段名当成主体
        self.assertNotIn("contradicts", syms,
                         "contradicts 是关系名字符串，不是可验证的主体")
        self.assertNotIn("can_reuse_evidence", syms,
                         "can_reuse_evidence 是返回字典的键，不是主体")

    def test_extracts_absent_claims_with_scope(self):
        claims = vc.extract_absent_claims(self.rows)
        scoped = {c["pattern"]: c["scope"] for c in claims}
        self.assertIn("WAITING_USER", scoped)
        # WAITING_USER 的声明带作用域（只在 server.py 里缺席）
        self.assertTrue(scoped["WAITING_USER"],
                        "应识别出作用域限定，避免误判全局缺席")

    def test_expand_theme_count(self):
        self.assertEqual(vc.expand_theme_count("P-01/02 产品定位与全量 PRD"), 2)
        self.assertEqual(vc.expand_theme_count("M-01..05 用户/项目/领域/会话/工作记忆"), 5)
        self.assertEqual(vc.expand_theme_count("R-01 统一入口与 Task of Record"), 1)


if __name__ == "__main__":
    unittest.main()
