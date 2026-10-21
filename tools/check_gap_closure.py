"""缺口闭合校验器：把「文档还没做到的主题」与「特性清单」做机器交叉。

背景
----
台账说「还差哪些」靠的是阅读 `IMPLEMENTATION.md` 的分组表，人读会漏、
并行开发时台账又会漂移。本工具把「缺口 → 特性」的映射变成可执行断言：

  1. 抽取 `IMPLEMENTATION.md` 分组表里全部非 ✅ 主题（🔹 部分 / ⬜ 未实现）
  2. 抽取 `FEATURE_SPLIT.md` §7 映射表里声明的「关联主题」
  3. 逐主题判定：是否已被某个特性认领

输出三类：
  COVERED     已被特性认领
  UNCLAIMED  未被任何特性认领（真实漏项）
  ORPHAN     特性表里声明了但主题表里没有（台账过期/写错主题号）

判定口径与 FEATURE_SPLIT.md §5.4 DoD 一致，但注意**主题 → 特性是多对一**
（F-05 覆盖 M-01..10 十个主题），所以「未覆盖」是严重信号，「多对一」正常。

用法
----
    python tools/check_gap_closure.py
    python tools/check_gap_closure.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(ROOT, "IMPLEMENTATION.md")
SPLIT = os.path.join(ROOT, "FEATURE_SPLIT.md")
ROADMAP = os.path.join(ROOT, "ROADMAP.md")

OK, PART, MISS = "✅", "🔹", "⬜"

# 主题行：`| P-04 Windows 桌面端 | ⬜ | 说明 |`
# 主题号写法有两种：单号 `P-04`、合号 `P-01/02`（斜杠后省略前缀）、
# 区间 `M-01..05`（见 RANGE_ROW）。
TOPIC_ROW = re.compile(
    r"^\|\s*(?P<ids>[A-Z]{1,2}-\d{2}(?:\s*/\s*(?:[A-Z]{1,2}-)?\d{2})?)"
    r"\s+(?P<title>[^|]*?)\s*\|\s*"
    r"(?P<status>[✅🔹⬜][^|]*?)\s*\|"
)
# 「M-01..05」这类区间写法
RANGE_ROW = re.compile(
    r"^\|\s*(?P<start>[A-Z]{1,2})-(?P<lo>\d{2})\.\.(?P<hi>\d{2})\s+"
    r"(?P<title>[^|]*?)\s*\|\s*(?P<status>[✅🔹⬜][^|]*?)\s*\|"
)
# 特性映射表行：`| F-03 | WP-0.2 | R-12 | **用例 4** ✅ |`
# 也支持区间写法 `| F-17..F-25 | WP-2.1..2.9 | G-01..G-10 | ... |`
# 特性号后可能跟状态标记（`F-01 ✅`），故允许 `**`/空白/符号在特性号与 `|` 之间。
FEATURE_ROW = re.compile(
    r"^\|\s*\*{0,2}(?P<fid>F-\d{2}(?:\.\.(?:F-)?\d{2})?)\*{0,2}"
    r"[^|]*?\|\s*(?P<rest>[^|]*)\|\s*"
    r"(?P<topics>[^|]*)\|\s*(?P<accept>[^|]*)\|"
)
# ROADMAP 的 WP 明细表行：`| WP-4.1 | C-12 | 编程专家 AST/LSP/... | 备注 |`
WP_ROW = re.compile(
    r"^\|\s*(?P<wp>WP-\d{1,2}\.\d{1,2})\s*\|\s*(?P<topics>[^|]*)\|"
)
# ROADMAP 的 WP 小节标题：`#### WP-2.4 无进展检测与停止控制器（G-06）`
WP_HEADING = re.compile(
    r"^#{3,4}\s*(?P<wp>WP-\d{1,2}\.\d{1,2})[^（(]*[（(](?P<topics>[^）)]*)[）)]"
)

# 模块级缓存：WP -> 主题列表。parse_wp_refs 展开 `WP-4.x` 时需要它，
# 而它在解析特性表时就会被用到，故不能只做局部变量。
WP_TOPICS: dict[str, list[str]] = {}
# 表格中出现的裸主题号
THEME_TOKEN = re.compile(r"\b([A-Z]{1,2}-\d{2})\b")
# 区间写法：`M-01..10` / `G-01..G-10`（前缀可重复也可省略）
THEME_RANGE = re.compile(r"\b([A-Z]{1,2})-(\d{2})\.\.((?:[A-Z]{1,2}-)?)(\d{2})\b")


def expand_theme_expr(expr: str) -> list[str]:
    """把特性映射表的主题列展开成主题号列表。

    映射表里主题列是自由文本，实测有这些写法：
      `R-12`            单号
      `M-01..10/C-03`  区间 + 混合
      `G-01..G-10`     区间
      `S/O/A/P`         组名（不产出主题号）
      `横切契约`         描述（不产出主题号）
    区间必须展开成逐个主题号，否则 G-02..G-09 这类会被误判成「未认领」。
    """
    out: list[str] = []
    # 先把区间替换成展开后的显式列表，再抓单号
    def _expand_range(m: re.Match) -> str:
        pre, lo, hi = m.group(1), int(m.group(2)), int(m.group(4))
        out.extend(f"{pre}-{i:02d}" for i in range(lo, hi + 1))
        return " "

    expr = THEME_RANGE.sub(_expand_range, expr)
    out.extend(THEME_TOKEN.findall(expr))
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def expand_ids(raw: str) -> list[str]:
    """`P-01/02` -> ['P-01','P-02']；`M-01..05` -> M-01..M-05。"""
    raw = raw.strip()
    m = re.match(r"^([A-Z]{1,2})-(\d{2})\.\.(\d{2})$", raw)
    if m:
        pre, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
        return [f"{pre}-{i:02d}" for i in range(lo, hi + 1)]
    m = re.match(r"^([A-Z]{1,2})-(\d{2})/([A-Z]{1,2}-)?(\d{2})$", raw)
    if m:
        pre = m.group(1)
        return [f"{pre}-{m.group(2)}", f"{pre}-{m.group(4)}"]
    return [raw]


def parse_ledger() -> dict[str, dict]:
    """抽取主题表：主题号 -> {status, title, line}"""
    out: dict[str, dict] = {}
    with open(LEDGER, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            m = RANGE_ROW.match(line)
            if m:
                for tid in expand_ids(f"{m.group('start')}-{m.group('lo')}..{m.group('hi')}"):
                    out[tid] = {
                        "status": m.group("status").strip()[0],
                        "title": m.group("title").strip(),
                        "line": lineno,
                    }
                continue
            m = TOPIC_ROW.match(line)
            if m:
                for tid in expand_ids(m.group("ids")):
                    out[tid] = {
                        "status": m.group("status").strip()[0],
                        "title": m.group("title").strip(),
                        "line": lineno,
                    }
    return out


def parse_features() -> dict[str, list[str]]:
    """抽取 §7 特性映射表：特性号 -> 声明关联的主题号列表"""
    out: dict[str, list[str]] = {}
    in_section = False
    with open(SPLIT, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("## 7."):
                in_section = True
                continue
            if in_section and line.startswith("## "):
                break
            if not in_section:
                continue
            m = FEATURE_ROW.match(line.rstrip("\n"))
            if m:
                out[m.group("fid")] = expand_theme_expr(m.group("topics"))
    return out


def parse_wp_topics() -> dict[str, list[str]]:
    """抽取 ROADMAP 的 WP -> 主题映射。

    两个来源都要读：
      1. 明细表行 `| WP-4.1 | C-12 | ... |`（表格里主题独占一列）
      2. 小节标题 `#### WP-2.4 无进展检测与停止控制器（G-06）`（括号里带主题）
    """
    out: dict[str, list[str]] = defaultdict(list)
    with open(ROADMAP, "r", encoding="utf-8") as fh:
        for line in fh:
            m = WP_ROW.match(line.rstrip("\n"))
            if m:
                for t in expand_theme_expr(m.group("topics")):
                    if t not in out[m.group("wp")]:
                        out[m.group("wp")].append(t)
                continue
            m = WP_HEADING.match(line.rstrip("\n"))
            if m:
                for t in expand_theme_expr(m.group("topics")):
                    if t not in out[m.group("wp")]:
                        out[m.group("wp")].append(t)
    return dict(out)


def parse_feature_wps() -> dict[str, list[str]]:
    """抽取 §7 特性映射表里的 ROADMAP 来源列：特性号 -> WP 列表。

    这是打通「主题 -> WP -> 特性」三段映射的关键一环：
    §7 表的「关联主题」列对 F-26..F-42 这类聚合行只写了组名
    （如 `C-12/C-13/C-10/H`、`S/O/A/P`），真实主题藏在 WP 侧。
    """
    out: dict[str, list[str]] = {}
    in_section = False
    with open(SPLIT, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("## 7."):
                in_section = True
                continue
            if in_section and line.startswith("## "):
                break
            if not in_section:
                continue
            m = FEATURE_ROW.match(line.rstrip("\n"))
            if m:
                out[m.group("fid")] = parse_wp_refs(m.group("rest"))
    return out


def parse_wp_refs(text: str) -> list[str]:
    """从「ROADMAP 来源」列解析 WP 引用。

    实测有四种写法：
      `WP-0.2`            单个
      `WP-0.7 + 5.1-5.4` 前缀省略的连续区间（锚定到首个 WP 的前缀）
      `WP-2.1..2.9`      区间
      `WP-4.x` / `WP-5.x` 轨道级概括（展开为该轨道全部 WP）
    """
    refs: list[str] = []

    def _add(x: str) -> None:
        if x not in refs:
            refs.append(x)

    # `WP-a.b` 锚点，用于解析 `5.1-5.4` 这种省略前缀的写法
    anchors = re.findall(r"WP-(\d{1,2})\.(\d{1,2})", text)

    # 1) 轨道级概括：WP-4.x -> 该轨道全部 WP
    for trk in re.findall(r"WP-(\d{1,2})\.x", text):
        for wp in WP_TOPICS:
            if wp.startswith(f"WP-{trk}."):
                _add(wp)

    # 2) 显式区间：WP-2.1..2.9
    for m in re.finditer(r"WP-(\d{1,2})\.(\d{1,2})\.\.(?:WP-)?(\d{1,2})\.(\d{1,2})",
                         text):
        trk, lo, hi = m.group(1), int(m.group(2)), int(m.group(4))
        for i in range(lo, hi + 1):
            _add(f"WP-{trk}.{i}")

    # 3) 省略前缀的连续段：`+ 5.1-5.4`（用最近的 WP 锚点的前缀）
    for m in re.finditer(r"(?<![\d.])(\d{1,2})\.(\d{1,2})-(\d{1,2})\.(\d{1,2})",
                         text):
        lo_hi = (int(m.group(2)), int(m.group(4)))
        trk = m.group(1) or (anchors[0][0] if anchors else None)
        if trk is None:
            continue
        for i in range(lo_hi[0], lo_hi[1] + 1):
            _add(f"WP-{trk}.{i}")

    # 4) 完整单号
    for wp in re.findall(r"WP-\d{1,2}\.\d{1,2}", text):
        _add(wp)

    return refs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    global WP_TOPICS
    ledger = parse_ledger()
    WP_TOPICS = parse_wp_topics()
    features = parse_features()
    feature_wps = parse_feature_wps()
    wp_topics = WP_TOPICS

    gaps = {tid: info for tid, info in ledger.items()
            if info["status"] in (PART, MISS)}

    # 反查表：主题 -> 声明它的特性（§7 关联主题列，直接路径）
    claimed: dict[str, list[str]] = defaultdict(list)
    for fid, topics in features.items():
        for t in topics:
            claimed[t].append(fid)

    # 间接路径：主题 -> WP（ROADMAP 明细）-> 特性（§7 的 ROADMAP 来源列）
    # §7 对 F-26..F-42 这类聚合行只写组名（C-12/C-13/C-10/H、S/O/A/P），
    # 真实主题藏在 ROADMAP 的 WP 侧，必须穿透这一层才能判定闭合。
    wp_to_features: dict[str, list[str]] = defaultdict(list)
    for fid, wps in feature_wps.items():
        for wp in wps:
            wp_to_features[wp].append(fid)

    indirect: dict[str, list[str]] = defaultdict(list)
    for wp, topics in wp_topics.items():
        for t in topics:
            for fid in wp_to_features.get(wp, []):
                if fid not in indirect[t]:
                    indirect[t].append(fid)

    covered = {t: sorted(set(v)) for t, v in claimed.items() if t in gaps}
    covered_via_wp = {
        t: sorted(set(indirect[t]))
        for t in gaps if t not in claimed and t in indirect
    }
    unclaimed = sorted(t for t in gaps
                       if t not in claimed and t not in indirect)
    orphan = sorted(t for t in claimed if t not in ledger)

    # 主题表里状态为 ✅ 但仍被特性声明关联的（特性已完成 / 已收口）
    done = sorted(t for t in claimed if t in ledger and ledger[t]["status"] == OK)

    result = {
        "ledger_topics": len(ledger),
        "gap_count": len(gaps),
        "features_with_topics": len(features),
        "wp_entries": len(wp_topics),
        "covered": covered,
        "covered_via_wp": covered_via_wp,
        "unclaimed": unclaimed,
        "orphan": orphan,
        "already_done": done,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if unclaimed else 0

    print("=" * 68)
    print("缺口闭合校验：未完成主题 → 特性认领")
    print("=" * 68)
    print(f"主题表主题数  : {len(ledger)}")
    print(f"未完成(🔹+⬜) : {len(gaps)}")
    print(f"特性映射条目  : {len(features)}")
    print(f"ROADMAP WP 条目: {len(wp_topics)}")
    print()
    print(f"✅ 直接闭合（§7 关联主题列）: {len(covered)}")
    for t, fs in sorted(covered.items()):
        st = ledger[t]["status"]
        mark = "✅" if st == PART else "⬜"
        print(f"   {t} {mark} -> {', '.join(sorted(set(fs)))}")
    print()
    print(f"✅ 间接闭合（经 ROADMAP WP 追溯）: {len(covered_via_wp)}")
    for t, fs in sorted(covered_via_wp.items()):
        st = ledger[t]["status"]
        mark = "✅" if st == PART else "⬜"
        print(f"   {t} {mark} -> {', '.join(fs)}")
    print()
    print(f"⬜ 未认领（真实漏项）: {len(unclaimed)}")
    for t in unclaimed:
        st = ledger[t]["status"]
        print(f"   {t} {st} {ledger[t]['title'][:38]}")
    print()
    if orphan:
        print(f"⚠️  特性表声明了但主题表没有: {len(orphan)}")
        for t in orphan:
            print(f"   {t} <- {', '.join(sorted(set(claimed[t])))}")
    if done:
        print(f"ℹ️  状态已 ✅ 但仍被特性关联（特性收口后应解绑）: {len(done)}")
        for t in done:
            print(f"   {t} -> {', '.join(sorted(set(claimed[t])))}")
    print()

    if unclaimed:
        print("❌ 存在未落到特性的缺口")
        return 1
    print("✅ 全部未完成主题均已落到特性")
    return 0


if __name__ == "__main__":
    sys.exit(main())
