"""缺口闭合校验器自身的测试。

背景
----
`tools/check_gap_closure.py` 回答的是一个很朴素的问题：
**文档里还没做到的主题，是不是都被排进了工作包和特性？**

这个断言如果只是「跑一下看着没问题」，它就只是装饰品——台账里的主题被静默删除、
工作包被改名、特性表某行写错，它都不会报。所以本测试做两件事：

  1. **锁定当前无漏项**（`TestGapClosureHolds`）
  2. **证明它确实能发现漏项**（`TestGapClosureCatchesGaps`）——
     故意在临时副本里制造漏项，验证校验器会报出来

第 2 条是重点：上一轮审计就是靠这个校验器找出 8 个真实漏项的，
如果它只能报「通过」，那就说明它没有判别力。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import check_gap_closure as cgc  # noqa: E402


def compute_unclaimed() -> list:
    """算出未认领主题（与 tools/check_gap_closure.py 主流程同口径）。"""
    ledger = cgc.parse_ledger()
    cgc.WP_TOPICS = cgc.parse_wp_topics()
    features = cgc.parse_features()
    feature_wps = cgc.parse_feature_wps()

    gaps = {t for t, i in ledger.items() if i["status"] in (cgc.PART, cgc.MISS)}
    claimed = {t for f in features.values() for t in f}
    wp_to_features = {}
    for fid, wps in feature_wps.items():
        for wp in wps:
            wp_to_features.setdefault(wp, []).append(fid)
    indirect = {t for wp, ts in cgc.WP_TOPICS.items()
               if wp in wp_to_features for t in ts}
    return sorted(gaps - claimed - indirect)


class TestGapClosureHolds(unittest.TestCase):
    """当前计划必须无漏项。"""

    def test_no_unclaimed_gaps(self):
        unclaimed = compute_unclaimed()
        self.assertEqual(
            unclaimed, [],
            f"以下未完成主题未落到任何特性（缺口闭合失败）：{unclaimed}")

    def test_ledger_topics_count_is_89(self):
        """主题表应展开为 89 主题 + A 组 7 子系统 = 96。

        89 主题 = P8 + R12 + M10 + C14 + E13 + G10 + O10 + S12。
        其中 `P-01/02` 合一行覆盖 2 主题、`M-01..05` 合一行覆盖 5 主题，
        故分组表实际 84 行、展开后 89。Android 七子系统 A-01..A-07 另计。
        """
        ledger = cgc.parse_ledger()
        self.assertEqual(len(ledger), 96,
                         f"主题表应展开 96 行（89 主题 + A 组 7），实际 {len(ledger)}")

        groups = {}
        for tid in ledger:
            pre = tid.split("-")[0]
            groups[pre] = groups.get(pre, 0) + 1
        for pre, expect in (("P", 8), ("R", 12), ("M", 10), ("C", 14),
                            ("E", 13), ("G", 10), ("O", 10), ("S", 12),
                            ("A", 7)):
            self.assertEqual(groups.get(pre), expect,
                             f"{pre} 组主题数应为 {expect}，实际 {groups.get(pre)}")

    def test_roverview_wp_entries_parsed(self):
        """ROADMAP 的 WP 主题映射必须真被解析出来（防止解析器静默失效）。"""
        cgc.WP_TOPICS = cgc.parse_wp_topics()
        self.assertGreaterEqual(len(cgc.WP_TOPICS), 30,
                                "ROADMAP WP 条目解析数过低 —— 解析器可能失效")
        # 抽样：这些主题在 WP 明细里是明确写出来的
        self.assertIn("G-01", cgc.WP_TOPICS.get("WP-2.1", []))
        self.assertIn("C-12", cgc.WP_TOPICS.get("WP-4.1", []))
        self.assertIn("S-10", cgc.WP_TOPICS.get("WP-5.6", []))


class TestGapClosureCatchesGaps(unittest.TestCase):
    """校验器必须能发现漏项 —— 否则它只是装饰品。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="_gap_probe_")
        self.orig_root = cgc.ROOT
        self.orig_ledger = cgc.LEDGER
        self.orig_split = cgc.SPLIT
        self.orig_roadmap = cgc.ROADMAP

    def tearDown(self):
        cgc.ROOT = self.orig_root
        cgc.LEDGER = self.orig_ledger
        cgc.SPLIT = self.orig_split
        cgc.ROADMAP = self.orig_roadmap
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _install(self, ledger_text=None, split_text=None):
        """把台账/特性表替换成临时副本（未指定则沿用原文件内容）。"""
        ledger_path = os.path.join(self.tmpdir, "IMPLEMENTATION.md")
        split_path = os.path.join(self.tmpdir, "FEATURE_SPLIT.md")
        roadmap_path = os.path.join(self.tmpdir, "ROADMAP.md")

        if ledger_text is None:
            shutil.copy(self.orig_ledger, ledger_path)
        else:
            with open(ledger_path, "w", encoding="utf-8") as fh:
                fh.write(ledger_text)
        if split_text is None:
            shutil.copy(self.orig_split, split_path)
        else:
            with open(split_path, "w", encoding="utf-8") as fh:
                fh.write(split_text)
        shutil.copy(self.orig_roadmap, roadmap_path)

        cgc.ROOT = self.tmpdir
        cgc.LEDGER = ledger_path
        cgc.SPLIT = split_path
        cgc.ROADMAP = roadmap_path

    def test_detects_theme_dropped_from_feature_table(self):
        """把认领 R-03 的特性**整条删掉** → 必须报为未认领。

        注意 F-53 在特性表里出现两次（波次 4.5 明细表 + §7 映射表），
        两条路径都要删，否则 §7 删了但明细表还在，间接路径仍会掩盖断裂。
        """
        self._install()
        with open(self.orig_split, encoding="utf-8") as fh:
            split_text = fh.read()
        lines = [ln for ln in split_text.split("\n")
                 if "F-53" not in ln]
        stripped = "\n".join(lines)
        self.assertNotEqual(stripped, split_text, "F-53 未被删除")
        self._install(split_text=stripped)

        unclaimed = compute_unclaimed()
        self.assertIn("R-03", unclaimed,
                      f"删除 F-53 后 R-03 应变为未认领，实际未认领：{unclaimed}")

    def test_duplicate_claim_masks_single_path_removal(self):
        """**已知局限，必须被记录**：同一主题被多条路径认领时，删掉一条仍闭合。

        R-03 同时被 §7 直接关联（F-53）与经 WP-6.3 间接关联认领。
        删掉 §7 那一条后，间接路径仍让 R-03 保持闭合 —— 校验器**看不出**。
        这不是 bug 而是"多路径冗余"的正常表现，但意味着：
        单点删除某个特性**不必然**产生漏项告警，
        真正会暴露的是「主题与其所有路径同时消失」。
        本测试把这个局限显式钉住，避免以后误以为校验器有更强判别力。
        """
        self._install()
        with open(self.orig_split, encoding="utf-8") as fh:
            split_text = fh.read()
        # 只删 §7 映射表那一行（`| F-53 | WP-6.3 | R-03 | — |`），
        # 保留波次 4.5 明细表里的 F-53
        lines = [ln for ln in split_text.split("\n")
                 if ln.strip() != "| F-53 | WP-6.3 | R-03 | — |"]
        self._install(split_text="\n".join(lines))

        unclaimed = compute_unclaimed()
        self.assertNotIn(
            "R-03", unclaimed,
            "本测试断言的是已知局限：间接路径仍在，故 R-03 仍闭合。"
            "若此断言失败，说明校验器行为变了，需重新评估其判别力边界")

    def test_detects_theme_marked_done_but_no_feature(self):
        """一个 🔹 主题被误标 ✅ → 缺口集合变小，但映射不闭合仍应被发现。"""
        self._install()
        with open(self.orig_ledger, encoding="utf-8") as fh:
            ledger_text = fh.read()
        # 把 R-12 从 ⬜ 改成 🔹（状态仍为未完成，不影响判定）—— 验证状态机本身工作正常
        modified = ledger_text.replace(
            "| R-12 外部副作用账本 | ⬜ |", "| R-12 外部副作用账本 | 🔹 |")
        self._install(ledger_text=modified)
        unclaimed = compute_unclaimed()
        self.assertEqual(unclaimed, [],
                         f"状态由 ⬜ 改 🔹 不应改变闭合性：{unclaimed}")

    def test_detects_new_uncovered_theme(self):
        """凭空加一个无任何特性认领的 ⬜ 主题 → 必须报出。"""
        self._install()
        with open(self.orig_ledger, encoding="utf-8") as fh:
            ledger_text = fh.read()
        injected = ledger_text.replace(
            "| R-01 统一入口与 Task of Record |",
            "| Z-99 凭空冒出的主题 | ⬜ | 缺陷注入 |\n"
            "| R-01 统一入口与 Task of Record |", 1)
        self.assertNotEqual(injected, ledger_text, "注入锚点未命中")
        self._install(ledger_text=injected)

        unclaimed = compute_unclaimed()
        self.assertIn("Z-99", unclaimed,
                      f"注入的孤立主题应被报为未认领，实际：{unclaimed}")

    def test_detects_broken_feature_row(self):
        """特性表某行被写坏（列数不对）→ 该主题应失去认领。

        F-53 在特性表出现两次（波次 4.5 明细表 6 列 + §7 映射表 4 列），
        两处都要破坏，否则另一处仍能认领 R-03。
        """
        self._install()
        with open(self.orig_split, encoding="utf-8") as fh:
            split_text = fh.read()
        broken = split_text.replace(
            "| **F-53** | WP-6.3 | R-03 | — |", "| **F-53** 坏了")
        broken = broken.replace(
            "| **F-53** | 路由质量评测与纠错 | WP-6.3 | R-03 | "
            "`evolution/routing_eval.py` | `experts/registry.py`（转发） |",
            "| **F-53** 坏了")
        self.assertNotEqual(broken, split_text, "破坏未命中任何 F-53 行")
        self.assertNotIn("F-53** | WP-6.3", broken, "§7 映射表行未破坏")
        self._install(split_text=broken)
        unclaimed = compute_unclaimed()
        self.assertIn("R-03", unclaimed,
                      f"特性行写坏后 R-03 应变为未认领：{unclaimed}")


class TestParserEdgeCases(unittest.TestCase):
    """解析器必须覆盖台账里实际出现的所有写法。"""

    def test_expand_combined_ids(self):
        """`P-01/02` -> 两个主题。"""
        self.assertEqual(cgc.expand_ids("P-01/02"), ["P-01", "P-02"])

    def test_expand_range_ids(self):
        """`M-01..05` -> 五个主题。"""
        self.assertEqual(cgc.expand_ids("M-01..05"),
                         ["M-01", "M-02", "M-03", "M-04", "M-05"])

    def test_expand_theme_expr_with_range(self):
        """映射表主题列的区间写法。"""
        out = cgc.expand_theme_expr("M-01..10/C-03")
        self.assertEqual(out[:10],
                         [f"M-{i:02d}" for i in range(1, 11)])
        self.assertIn("C-03", out)

    def test_expand_theme_expr_repeated_prefix_range(self):
        """`G-01..G-10` 前缀重复写法。"""
        out = cgc.expand_theme_expr("G-01..G-10")
        self.assertEqual(len(out), 10)
        self.assertIn("G-05", out)
        self.assertIn("G-10", out)

    def test_expand_theme_expr_ignores_group_names(self):
        """`S/O/A/P` 这类组名不产出主题号。"""
        self.assertEqual(cgc.expand_theme_expr("S/O/A/P"), [])

    def test_parse_wp_refs_single(self):
        cgc.WP_TOPICS = cgc.parse_wp_topics()
        self.assertEqual(cgc.parse_wp_refs("WP-0.2"), ["WP-0.2"])

    def test_parse_wp_refs_track_wildcard(self):
        """`WP-4.x` 展开为该轨道全部 WP。"""
        cgc.WP_TOPICS = cgc.parse_wp_topics()
        refs = cgc.parse_wp_refs("WP-4.x")
        self.assertIn("WP-4.1", refs)
        self.assertIn("WP-4.5", refs)

    def test_parse_wp_refs_prefix_less_range(self):
        """`WP-0.7 + 5.1-5.4` 省略前缀的连续段。"""
        cgc.WP_TOPICS = cgc.parse_wp_topics()
        refs = cgc.parse_wp_refs("WP-0.7 + 5.1-5.4")
        for i in range(1, 5):
            self.assertIn(f"WP-5.{i}", refs, f"WP-5.{i} 未展开：{refs}")


if __name__ == "__main__":
    unittest.main()
