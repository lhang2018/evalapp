"""全台账行号漂移扫描：检查所有 .md 里 `文件:行号` 引用是否仍指向所述符号。

`tools/verify_claims.py` 只校验 IMPLEMENTATION.md 的主题表。本工具覆盖面更广：
扫全部顶层 .md，把 `符号`（`文件:行号`）形态的引用逐条核对 —— 该行必须含该符号。

判据与 IMPLEMENTATION.md 校验器一致（避免两套标准）：
- 符号与路径之间不允许再出现反引号
- 符号与路径之间不允许出现 CJK 字符

用法：
    python tools/scan_all_ledgers.py            # 人类可读
    python tools/scan_all_ledgers.py --json
    python tools/scan_all_ledgers.py --fix-hint # 给出建议的新行号
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 与 verify_claims 保持一致的两条约束
SEM_CITE_RE = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]*)`[^`\n\u4e00-\u9fff]{0,20}?"
    r"`([A-Za-z0-9_\-./]+\.(?:py|ts|kt|tsx)):(\d+)`")
# 裸引用：`file.py:123`（无符号名）—— 只校验文件存在与行号在范围内
BARE_CITE_RE = re.compile(
    r"`([A-Za-z0-9_\-./]+\.(?:py|ts|kt|tsx)):(\d+)`")

# **符号锚定引用**：`file.py::symbol` —— 不依赖行号，代码加行也不失效。
#
# 为什么需要这一种：并行开发中任何人在文件里加几行，`file.py:123` 就会指向
# 别的语句。实测中 controller.py 的 submit_task 因并行工作流的 F-03 改动
# 从 77 行移到 80 行，导致引用漂移。符号锚定形式把「指向哪个定义」与
# 「它在第几行」解耦，前者稳定，后者易变。
ANCHOR_CITE_RE = re.compile(
    r"`([A-Za-z0-9_\-./]+\.(?:py|ts|kt|tsx))::([A-Za-z_][A-Za-z0-9_]*)`")

SKIP_DIRS = {"node_modules", ".git", "__pycache__", "build", ".gradle",
             "outputs", "docs", "资料", "chat"}


def resolve(path_hint: str) -> str | None:
    """把路径片段解析成磁盘绝对路径（与 verify_claims 同口径）。"""
    candidates = [
        (os.path.join(ROOT, "evoagent"), path_hint),
        (os.path.join(ROOT, "android-app"), path_hint),
        (os.path.join(ROOT, "tests"), path_hint),
        (ROOT, path_hint),
    ]
    for base, hint in candidates:
        cand = os.path.join(base, hint)
        if os.path.exists(cand):
            return cand
    # 后缀搜索
    hint = path_hint.replace("\\", "/")
    for base in (os.path.join(ROOT, "evoagent"),
                 os.path.join(ROOT, "android-app"),
                 os.path.join(ROOT, "tests"), ROOT):
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fn in files:
                full = os.path.join(dirpath, fn)
                r = os.path.relpath(full, ROOT).replace("\\", "/")
                if r.endswith(hint) or fn == hint:
                    return full
    return None


def read_lines(path: str) -> list[str] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().split("\n")
    except (OSError, UnicodeDecodeError):
        return None


def find_symbol_line(full: str, sym: str) -> int | None:
    """在文件里找符号的定义行（def/class/字段赋值/import）。"""
    lines = read_lines(full)
    if lines is None:
        return None
    pat = re.compile(rf"(\bdef\s+{re.escape(sym)}\b|\bclass\s+{re.escape(sym)}\b"
                     rf"|\b{re.escape(sym)}\s*[:=]\s*"
                     rf"|^\s*{re.escape(sym)}\s*[:=])")
    for i, line in enumerate(lines, 1):
        if pat.search(line):
            return i
    return None


def suggest_line(full: str, sym: str) -> int | None:
    """在该文件里找符号的定义行（def/class/字段赋值），用于修复建议。"""
    return find_symbol_line(full, sym)


def scan(paths: list[str]) -> dict:
    problems = []
    checked = 0
    bare_checked = 0
    anchor_checked = 0
    for rel_path in paths:
        full = os.path.join(ROOT, rel_path)
        lines = read_lines(full)
        if lines is None:
            continue
        for lineno, line in enumerate(lines, 1):
            # --- 符号锚定引用：`file.py::symbol` ---
            for m in ANCHOR_CITE_RE.finditer(line):
                path_hint, sym = m.group(1), m.group(2)
                target = resolve(path_hint)
                if target is None:
                    problems.append({
                        "doc": rel_path, "doc_line": lineno,
                        "kind": "anchor_path_missing",
                        "detail": f"锚定引用 `{path_hint}::{sym}` 文件不存在",
                    })
                    continue
                anchor_checked += 1
                if find_symbol_line(target, sym) is None:
                    problems.append({
                        "doc": rel_path, "doc_line": lineno,
                        "kind": "anchor_symbol_missing",
                        "detail": (f"锚定引用 `{path_hint}::{sym}` "
                                   f"在文件里找不到该符号"),
                    })

            # --- 语义引用：符号 + 路径 + 行号 ---
            for m in SEM_CITE_RE.finditer(line):
                sym, path_hint, num = m.group(1), m.group(2), int(m.group(3))
                target = resolve(path_hint)
                if target is None:
                    problems.append({
                        "doc": rel_path, "doc_line": lineno,
                        "kind": "path_missing",
                        "detail": f"`{path_hint}:{num}` 文件不存在",
                    })
                    continue
                tlines = read_lines(target)
                if tlines is None or num > len(tlines):
                    problems.append({
                        "doc": rel_path, "doc_line": lineno,
                        "kind": "line_out_of_range",
                        "detail": f"`{path_hint}:{num}` 行号越界",
                    })
                    continue
                checked += 1
                if sym not in tlines[num - 1]:
                    sug = suggest_line(target, sym)
                    problems.append({
                        "doc": rel_path, "doc_line": lineno,
                        "kind": "symbol_drift",
                        "detail": (f"`{path_hint}:{num}` 应指向 `{sym}`，"
                                   f"实际: {tlines[num-1].strip()[:50]}"),
                        "suggest": (f"{os.path.relpath(target, ROOT)}:{sug}"
                                    if sug else None),
                        "symbol": sym,
                    })

            # --- 裸引用：`file.py:123` ---
            # 判据（保守）：文件必须存在、行号必须在范围内、且该行**非空**
            # 非空是最低门槛 —— 一条引用指向空行/注释尾行往往意味着已漂移。
            for m in BARE_CITE_RE.finditer(line):
                path_hint, num = m.group(1), int(m.group(2))
                # 跳过已被语义引用覆盖的（同一段已单独报过）
                if SEM_CITE_RE.search(line):
                    continue
                target = resolve(path_hint)
                if target is None:
                    problems.append({
                        "doc": rel_path, "doc_line": lineno,
                        "kind": "bare_path_missing",
                        "detail": f"裸引用 `{path_hint}:{num}` 文件不存在",
                    })
                    continue
                tlines = read_lines(target)
                if tlines is None:
                    continue
                if num > len(tlines):
                    problems.append({
                        "doc": rel_path, "doc_line": lineno,
                        "kind": "bare_line_out_of_range",
                        "detail": (f"裸引用 `{path_hint}:{num}` 越界"
                                   f"（文件只有 {len(tlines)} 行）"),
                    })
                    continue
                bare_checked += 1
                if not tlines[num - 1].strip():
                    problems.append({
                        "doc": rel_path, "doc_line": lineno,
                        "kind": "bare_points_to_blank",
                        "detail": f"裸引用 `{path_hint}:{num}` 指向空行",
                    })
    return {"checked": checked, "bare_checked": bare_checked,
            "anchor_checked": anchor_checked,
            "problems": problems}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--docs", nargs="*", default=None)
    args = ap.parse_args()

    docs = args.docs
    if not docs:
        docs = sorted(f for f in os.listdir(ROOT)
                      if f.endswith(".md") and f != "README.md") + ["README.md"]

    rep = scan(docs)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print("=" * 68)
        print("全台账行号漂移扫描")
        print("=" * 68)
        print(f"文档      : {', '.join(docs)}")
        print(f"语义引用  : {rep['checked']} 条已核对")
        print(f"裸引用    : {rep['bare_checked']} 条已核对")
        print(f"锚定引用  : {rep.get('anchor_checked', 0)} 条已核对")
        print(f"发现问题  : {len(rep['problems'])}")
        print()
        by_doc: dict[str, list] = {}
        for p in rep["problems"]:
            by_doc.setdefault(p["doc"], []).append(p)
        for doc, items in by_doc.items():
            print(f"--- {doc} ({len(items)}) ---")
            for p in items:
                sug = f"  → 建议改为 {p['suggest']}" if p.get("suggest") else ""
                print(f"  第 {p['doc_line']} 行 [{p['kind']}] {p['detail']}{sug}")
            print()
    return 0 if not rep["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
