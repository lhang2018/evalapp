"""台账声明校验器：把 IMPLEMENTATION.md 里的每条声明变成可执行断言。

背景
----
「修记账」只改了文字，判定依据仍是我的阅读结论 —— 不可复现，也无法防止
后续提交再次把「接口在、逻辑空」记成 ✅。本工具把台账变成**可执行断言**：

  1. 引用校验：台账里每个 `路径:行号` 引用必须真实存在且该行非空。
     引用错了 = 证据不成立 = 判定不可信。
  2. 符号校验：台账点名的符号必须在 evoagent/ 内**有引用**（不只是定义）。
     「定义了但零引用」= 死字段/死代码，正是本次审计要剔除的对象。
  3. 缺席校验：台账声称「零命中」「未实现」的模式必须真的搜不到。
     声称不存在却存在 = 判定反了。
  4. 状态自洽：分组表 ✅/🔹/⬜ 计数必须与文末复核命令一致。

用法
----
    python tools/verify_claims.py --check        # 校验台账，有错则退出码 1
    python tools/verify_claims.py --check --json

判定标准（与 FEATURE_SPLIT.md §5.4 DoD 一致）
    ✅  实现逻辑 + 产生它的代码路径 + 走通该路径的测试
    🔹  主路径可用，但关键分支缺失 / 字段无读取点 / 依赖未接线
    ⬜  无对应物
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "evoagent")
TESTS = os.path.join(ROOT, "tests")
LEDGER = os.path.join(ROOT, "IMPLEMENTATION.md")

# 台账里出现的路径前缀 -> 实际目录
PATH_HINTS = {
    "evoagent/": PKG,
    "android-app/": os.path.join(ROOT, "android-app"),
    "tests/": TESTS,
    "": ROOT,
}

# 状态符号
OK, PART, MISS = "✅", "🔹", "⬜"


# ---------------------------------------------------------------- 工具函数

def resolve(path_hint: str) -> str | None:
    """把台账里的路径片段解析成磁盘绝对路径。

    台账里的引用有三种写法，都必须解析成功，否则证据链断裂：
      1. 带前缀     evoagent/react/engine.py
      2. 相对模块   react/engine.py  -> evoagent/react/engine.py
      3. 客户端     offline/policy.ts -> android-app/src/offline/policy.ts
    """
    for prefix, base in PATH_HINTS.items():
        if prefix and path_hint.startswith(prefix):
            tail = path_hint[len(prefix):]
            cand = os.path.join(base, tail)
            if os.path.exists(cand):
                return cand
    # 按后缀在 evoagent/ 与 android-app/ 下搜索
    hint = path_hint.replace("\\", "/")
    for base in (PKG, os.path.join(ROOT, "android-app"), TESTS, ROOT):
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != "node_modules"]
            for fn in files:
                full = os.path.join(dirpath, fn)
                r = rel(full)
                if r.endswith(hint) or fn == hint:
                    return full
    return None

def iter_py(root: str):
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def iter_source(root: str):
    """源码文件（Python + TypeScript/Kotlin），排除 node_modules 与构建产物。"""
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in ("node_modules", "build", ".gradle",
                                "__pycache__", ".git")]
        for fn in files:
            if fn.endswith((".py", ".ts", ".tsx", ".kt")):
                yield os.path.join(dirpath, fn)


def rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace("\\", "/")


# ---------------------------------------------------------------- 符号引用

def build_symbol_index() -> dict[str, dict]:
    """符号 -> {defined_at, refs, calls, test_calls, module}

    refs       —— 被引用总次数（含字典装配、属性访问、框架回调的间接引用）
    calls      —— 直接调用次数
    test_calls —— **在 tests/ 内的直接调用次数**

    判定死代码用 refs==0；判定「有走通该路径的测试」用 test_calls>0。
    """
    defs: dict[str, tuple[str, int, str]] = {}
    trees: dict[str, ast.Module] = {}
    for path in iter_py(PKG):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (SyntaxError, UnicodeDecodeError):
            continue
        trees[path] = tree
        mod = rel(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                defs.setdefault(node.name, (f"{mod}:{node.lineno}",
                                           node.lineno, mod))
    refs: dict[str, int] = defaultdict(int)
    calls: dict[str, int] = defaultdict(int)
    for path, tree in trees.items():
        mod = rel(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in defs:
                    dmod, dline, _ = defs[node.id]
                    if not (dmod == mod and dline == node.lineno):
                        refs[node.id] += 1
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                if node.attr in defs:
                    dmod, dline, _ = defs[node.attr]
                    if not (dmod == mod and dline == node.lineno):
                        refs[node.attr] += 1
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else (
                    fn.attr if isinstance(fn, ast.Attribute) else None)
                if name and name in defs:
                    dmod, dline, _ = defs[name]
                    if not (dmod == mod and dline == node.lineno):
                        calls[name] += 1

    # tests/ 内的直接调用 -> test_calls
    test_calls: dict[str, int] = defaultdict(int)
    # 模块级测试覆盖：tests/ 里 import 了哪些 evoagent 模块
    module_tested: set[str] = set()
    for path in iter_py(TESTS):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
                tree = ast.parse(src, filename=path)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else (
                    fn.attr if isinstance(fn, ast.Attribute) else None)
                if name and name in defs:
                    test_calls[name] += 1
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("evoagent"):
                    module_tested.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("evoagent"):
                        module_tested.add(alias.name)

    idx = {}
    for n, (loc, _l, mod) in defs.items():
        modpath = mod[:-3] if mod.endswith(".py") else mod
        idx[n] = {
            "defined_at": loc, "module": mod, "refs": refs.get(n, 0),
            "calls": calls.get(n, 0), "test_calls": test_calls.get(n, 0),
            "module_tested": (modpath in module_tested
                              or modpath + ".__init__" in module_tested
                              or any(m.startswith(modpath + ".")
                                     for m in module_tested)),
        }
    return idx


# ---------------------------------------------------------------- 解析台账

CITE_RE = re.compile(r"([A-Za-z0-9_\-./]+\.(?:py|ts|kt|tsx|md|json)):(\d+)")
# 「符号（`path:line`）」这种引用形态：校验该行确实定义了这个符号。
#
# 两条约束，缺一不可：
#   1. 符号与路径之间不允许再出现反引号 —— 否则会跨过中间的引用去配对，
#      把 `check_op`（`permissions.py:64`）…`engine.py:80` 错配成一对。
#   2. 符号与路径之间不允许出现中日韩字符 —— 「与专家的 `memory_scope_key`
#      语义不同；且 `store.py:248` 检索过滤」这种中文散文里的两个引用
#      不构成「符号定义在该行」的关系。
SEM_CITE_RE = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]*)`[^`\n\u4e00-\u9fff]{0,20}?"
    r"`([A-Za-z0-9_\-./]+\.(?:py|ts|kt|tsx)):(\d+)`")
# 主题行：`| P-01 ... | ✅ | 说明 |`，也支持 `M-01..05` 合并写法
STATUS_RE = re.compile(r"^\*{0,2}([PRMCEGOS])-([0-9]+)")


def split_row(line: str) -> list[str]:
    """切分 Markdown 表格行，**不把转义竖线当列分隔符**。

    台账里有 `grep "capability_gap\\|质量退化\\|成本异常"` 这种内容，
    里面的 `\\|` 是**行内代码的一部分**，不是列分隔符。
    用朴素的 line.split("|") 会把这一行切成 7 段，说明列被从中间截断 ——
    导致后半段声明（如 E-07 的 register_problem）根本抽不出来。
    """
    cells: list[str] = []
    cur: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n and line[i + 1] == "|":
            cur.append("|")          # 转义竖线：还原为字面量
            i += 2
            continue
        if ch == "|":
            cells.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    cells.append("".join(cur))
    return [c.strip() for c in cells]


def parse_ledger(text: str) -> list[dict]:
    """抽出分组表每一行：主题、状态、说明。

    表格行的结构是 `| 主题 | 状态 | 说明 |`，切分后 cells[1]=主题、
    cells[2]=状态、cells[3]=说明（cells[0] 是行首空串）。
    """
    rows = []
    for lineno, line in enumerate(text.split("\n"), 1):
        if not line.startswith("| "):
            continue
        cells = split_row(line)
        # 需要至少 主题|状态|说明 三列（含首尾空串即 len>=4）
        if len(cells) < 4:
            continue
        m = STATUS_RE.match(cells[1])
        if not m:
            continue
        status = cells[2].replace("*", "").strip()
        if status not in (OK, PART, MISS):
            continue
        rows.append({
            "line": lineno,
            "theme": cells[1],
            "status": status,
            "note": cells[3],
        })
    return rows


def check_citations(text: str) -> list[dict]:
    """校验每个 `路径:行号` 引用。"""
    problems = []
    seen = set()
    for lineno, line in enumerate(text.split("\n"), 1):
        if line.startswith("#"):
            continue
        for m in CITE_RE.finditer(line):
            path_hint, num = m.group(1), int(m.group(2))
            key = (path_hint, num)
            if key in seen:
                continue
            seen.add(key)
            full = resolve(path_hint)
            if full is None:
                problems.append({
                    "kind": "citation_path_missing",
                    "citation": f"{path_hint}:{num}",
                    "ledger_line": lineno,
                    "detail": "文件不存在",
                })
                continue
            if not full.endswith((".py", ".ts", ".tsx", ".kt")):
                continue
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    lines = fh.read().split("\n")
            except (OSError, UnicodeDecodeError):
                continue
            if num > len(lines):
                problems.append({
                    "kind": "citation_line_out_of_range",
                    "citation": f"{path_hint}:{num}",
                    "ledger_line": lineno,
                    "detail": f"文件只有 {len(lines)} 行",
                })
    return problems


# ---------------------------------------------------------------- 声明抽取
#
# 设计要点（第二轮修正）：
#   第一版把「死代码声明」「缺席声明」写成硬编码列表，结果只能验证预先知道的
#   那几条 —— 往台账里写一条新声明它不会管，往列表里加一条又要改代码。
#   现在改为**从台账正文反向抽取声明**：凡是写了
#     「零调用 / 零引用 / 无调用方 / 零命中 / 全仓无 / 未实现 / 不存在」
#   的符号，都会被自动取出并逐条验证。
#   这样新增声明自动纳管，判定与证据永远同步。

# 声明「该符号无调用方 / 无引用」的措辞。
# 符号写法要兼容 `foo` 与 `foo()` 两种（台账里两种都有）——
# 注意 `foo()` 的括号在反引号**内部**，所以要把 `(\(\))?` 放在反引号之前。
DEAD_PAT = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]*)(\(\))?`"
    r"[^\n|]{0,40}?"
    r"(零调用方|零调用|零引用|无调用方|全仓零调用|永不读取|零调用点|"
    r"无任何调用方|没有任何代码在用|无代码在依赖)")

# 声明「该模式不存在」的措辞。
# 关键：台账的缺席声明**常带作用域限定**，例如
#   「`WAITING_USER` 在 `api/server.py` 零命中（无接口提交澄清答案）」
# 这里 WAITING_USER 在 controller.py / state_machine.py 确实存在（是 Task 状态），
# 但声明的真实含义是「server.py 里没有提交澄清答案的端点」。
# 因此抽取时必须连同作用域一起取出，校验时只在作用域内搜索。
#
# 另：被引用的内容必须是**像标识符的东西**，否则会把
# 「六参数全必填，**四类信号（失败/成本异常/质量退化/能力缺口）在」这种
# 中文散文片段当成模式。限定为标识符/路径形态即可。
ABSENT_PAT = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]*(?:\(\))?)`"
    r"(?:\s*在\s*`([^`]{2,80})`)?"
    r"[^\n|]{0,30}?"
    r"(零命中|全仓零命中|零匹配|不存在|没有该|未实现)")

# 缺席声明里已知的假阳性豁免（合法但名字相近的符号）
ABSENT_EXEMPT = {
    "class Goal": r"^class Goal\b",   # GoalOrigin 是合法既有契约
    "AST": r"\bAST\b",
    "LSP": r"\bLSP\b",
    "ContextManifest": r"\bContextManifest\b",
}


def extract_dead_claims(rows: list[dict]) -> list[dict]:
    """从台账每行说明里抽出「声称零调用」的符号声明。"""
    out = []
    for r in rows:
        for m in DEAD_PAT.finditer(r["note"]):
            out.append({
                "theme": r["theme"].split()[0],
                "symbol": m.group(1),
                "ledger_line": r["line"],
            })
    return out


def extract_absent_claims(rows: list[dict]) -> list[dict]:
    """从台账每行说明里抽出「声称不存在」的模式声明（含作用域限定）。"""
    out = []
    for r in rows:
        for m in ABSENT_PAT.finditer(r["note"]):
            out.append({
                "theme": r["theme"].split()[0],
                "pattern": m.group(1),
                "scope": m.group(2),      # 例如 "api/server.py"；None = 全仓
                "ledger_line": r["line"],
            })
    return out


def check_semantic_citations(text: str) -> list[dict]:
    """校验「符号（`path:line`）」这种引用：该行必须真的定义了这个符号。

    这是比「路径存在、行号在范围内」更强的判据 —— 它能抓住
    「行号漂移」：代码改过之后引用还指向旧的行，而那一行现在
    已经是别的语句。
    """
    problems = []
    seen = set()
    for lineno, line in enumerate(text.split("\n"), 1):
        for m in SEM_CITE_RE.finditer(line):
            sym, path_hint, num = m.group(1), m.group(2), int(m.group(3))
            key = (sym, path_hint, num)
            if key in seen:
                continue
            seen.add(key)
            full = resolve(path_hint)
            if full is None:
                continue  # 路径问题已由 check_citations 报过
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    lines = fh.read().split("\n")
            except (OSError, UnicodeDecodeError):
                continue
            if num > len(lines):
                continue  # 行号越界已由 check_citations 报过
            target = lines[num - 1]
            # 该行必须提到这个符号（定义行、装饰器行、引用行都算）
            if sym not in target:
                problems.append({
                    "kind": "semantic_citation_mismatch",
                    "citation": f"{path_hint}:{num} -> `{sym}`",
                    "ledger_line": lineno,
                    "detail": f"该行未提及 `{sym}`，实际内容：{target.strip()[:60]}",
                })
    return problems


def check_dead_claims(index: dict[str, dict], claims: list[dict]) -> list[dict]:
    """验证「声称零调用」的符号确实零引用。

    两种结果：
      符号在索引里且 refs>0  -> 判定与代码不符，判错
      符号不在索引里          -> **无法验证**（可能是字典键/字段名，
                                不是函数）。这种情况不能静默放过：
                                要么改成函数名，要么去掉该措辞。
    """
    problems = []
    for c in claims:
        sym = c["symbol"]
        info = index.get(sym)
        if info is None:
            problems.append({
                "kind": "dead_claim_unverifiable",
                "theme": c["theme"], "symbol": sym,
                "detail": f"台账称 `{sym}` 零调用，但它不是可索引的函数/类"
                          f"（可能是字典键或字段名），无法验证该声明",
                "ledger_line": c["ledger_line"],
            })
            continue
        if info["refs"] > 0:
            problems.append({
                "kind": "dead_claim_but_referenced",
                "theme": c["theme"], "symbol": sym,
                "detail": f"台账称 `{sym}` 零调用，但 {info['defined_at']} "
                          f"有 {info['refs']} 处引用",
                "ledger_line": c["ledger_line"],
            })
    return problems


def check_absent_claims(claims: list[dict]) -> list[dict]:
    """验证「声称不存在」的模式确实搜不到。

    作用域处理：声明里带「在 `X` 零命中」的，只在 X 内搜；
    无作用域的（"全仓零命中"）在全仓搜。
    """
    problems = []
    for c in claims:
        pat = c["pattern"]
        scope = c.get("scope")
        rx_src = ABSENT_EXEMPT.get(pat)
        if rx_src is None:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", pat):
                rx_src = rf"\b{re.escape(pat)}\b"
            else:
                rx_src = re.escape(pat)
        rx = re.compile(rx_src)

        # 决定搜索范围
        if scope:
            target = resolve(scope)
            bases = [os.path.dirname(target)] if target else []
            if not target:
                problems.append({
                    "kind": "absent_claim_scope_missing",
                    "theme": c["theme"], "pattern": pat,
                    "detail": f"声明作用域 `{scope}` 无法定位到文件",
                    "ledger_line": c["ledger_line"],
                })
                continue
        else:
            bases = [PKG, os.path.join(ROOT, "android-app", "src")]

        hits = []
        for base in bases:
            for path in iter_source(base):
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            hits.append(f"{rel(path)}:{i}")
                if len(hits) > 5:
                    break
        if hits:
            problems.append({
                "kind": "absent_claim_but_present",
                "theme": c["theme"], "pattern": pat,
                "detail": (f"台账称 `{pat}` 在 {scope or '全仓'} 不存在，"
                           f"实际命中 {hits[:4]}"),
                "ledger_line": c["ledger_line"],
            })
    return problems


def check_positive_claims(rows: list[dict], index: dict[str, dict],
                          strict: bool = True) -> tuple[list[dict], list[dict]]:
    """校验 ✅ 行的**正面证据**。

    DoD 三条硬门槛：
      (1) 有实现逻辑  (2) 有产生它的代码路径  (3) 有走通该路径的测试

    证据来源两种（满足其一即可）：
      A. 符号级 —— 说明里点名的符号，refs>0（有人用它）且 test_calls>0
         （有测试真的调用了它）
      B. 模块级 —— 说明里用 `file.py` 点名的模块，出现在 tests/ 的 import 里
         （有测试覆盖该模块）

    返回 (problems, weak_rows)。weak_rows 是「证据不足但暂不判错」的行，
    用于生成待补证据清单。
    """
    problems: list[dict] = []
    weak: list[dict] = []
    for r in rows:
        if r["status"] != OK:
            continue
        note = r["note"]

        # --- 证据 A：符号级 ---
        syms = []
        for m in re.finditer(r"`([A-Za-z_][A-Za-z0-9_\.]*)`", note):
            s = m.group(1).split(".")[-1]
            if s in index:
                syms.append(s)
        sym_ok = [s for s in syms
                  if index[s]["refs"] > 0 and index[s]["test_calls"] > 0]
        sym_path_only = [s for s in syms if index[s]["refs"] > 0]

        # --- 证据 B：模块级 ---
        mod_ok = []
        for m in re.finditer(r"`([A-Za-z0-9_\-./]+\.(?:py|ts|tsx|kt))`", note):
            fname = m.group(1).split("/")[-1]
            for s, info in index.items():
                if info["module"].split("/")[-1] == fname and info["module_tested"]:
                    mod_ok.append(fname)
                    break

        if sym_ok or mod_ok:
            continue

        # 证据不足
        detail_bits = []
        if syms:
            detail_bits.append(f"点名符号 {syms} 中"
                               f"{'有路径但无测试调用' if sym_path_only else '无路径'}")
        if not syms:
            detail_bits.append("未点名可校验符号")
        if not mod_ok:
            detail_bits.append("所引模块无 tests/ 覆盖")
        detail = "；".join(detail_bits)

        item = {
            "kind": "positive_claim_weak_evidence",
            "theme": r["theme"],
            "detail": f"✅ 行证据不足：{detail}",
            "ledger_line": r["line"],
        }
        if strict:
            problems.append(item)
        else:
            weak.append(item)
    return problems, weak


# ---------------------------------------------------------------- 状态自洽

def count_statuses(rows: list[dict]) -> dict:
    """按**主题**计数（不是表格行数）。

    分组表用合并写法压缩了行数：`P-01/02` 覆盖 2 个主题、`M-01..05` 覆盖 5 个。
    台账「总体差距」表声明的是 89 主题口径，所以必须展开后统计。
    """
    c = defaultdict(int)
    for r in rows:
        c[r["status"]] += expand_theme_count(r["theme"])
    return dict(c)


def expand_theme_count(theme_cell: str) -> int:
    """从主题单元格算出它覆盖几个主题。

    `P-01/02 产品定位与全量 PRD` -> 2
    `M-01..05 用户/项目/...`      -> 5
    `R-01 统一入口与 Task of Record` -> 1
    """
    head = theme_cell.split()[0] if theme_cell.split() else ""
    head = head.replace("*", "")
    if ".." in head:
        rng = head.split("..")
        try:
            a = int(rng[0].split("-")[-1])
            b = int(rng[1])
            return max(1, b - a + 1)
        except (ValueError, IndexError):
            return 1
    if "/" in head:
        return len([p for p in head.split("/") if p.strip()])
    return 1


def check_status_ledger(rows: list[dict], text: str) -> list[dict]:
    """台账正文声明的计数必须等于实际表格计数。"""
    problems = []
    actual = count_statuses(rows)
    # 从「总体差距」表读出声明值
    m = re.search(r"\| ✅ 已实现[^|]*\|\s*(\d+)", text)
    if m:
        declared_ok = int(m.group(1))
        if actual.get(OK, 0) != declared_ok:
            problems.append({
                "kind": "status_count_mismatch",
                "detail": f"✅ 声明 {declared_ok}，实际表格 {actual.get(OK, 0)}",
            })
    m = re.search(r"\| 🔹 部分/接口已留[^|]*\|\s*(\d+)", text)
    if m:
        d = int(m.group(1))
        if actual.get(PART, 0) != d:
            problems.append({
                "kind": "status_count_mismatch",
                "detail": f"🔹 声明 {d}，实际表格 {actual.get(PART, 0)}",
            })
    m = re.search(r"\| ⬜ 未实现[^|]*\|\s*(\d+)", text)
    if m:
        d = int(m.group(1))
        if actual.get(MISS, 0) != d:
            problems.append({
                "kind": "status_count_mismatch",
                "detail": f"⬜ 声明 {d}，实际表格 {actual.get(MISS, 0)}",
            })
    return problems


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="校验台账声明")
    ap.add_argument("--gap", action="store_true",
                    help="列出 ✅ 行证据不足的清单（待补证据工作项）")
    ap.add_argument("--lenient", action="store_true",
                    help="证据不足只报告不判错")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ledger", default=LEDGER)
    args = ap.parse_args()

    with open(args.ledger, "r", encoding="utf-8") as fh:
        text = fh.read()

    rows = parse_ledger(text)
    index = build_symbol_index()

    dead_claims = extract_dead_claims(rows)
    absent_claims = extract_absent_claims(rows)

    problems: list[dict] = []
    problems += check_citations(text)
    problems += check_semantic_citations(text)
    problems += check_dead_claims(index, dead_claims)
    problems += check_absent_claims(absent_claims)
    pos_problems, weak = check_positive_claims(
        rows, index, strict=not args.lenient)
    problems += pos_problems
    problems += check_status_ledger(rows, text)

    actual = count_statuses(rows)
    report = {
        "ledger": rel(args.ledger),
        "rows_parsed": len(rows),
        "status_counts": actual,
        "symbols_indexed": len(index),
        "claims_checked": {
            "dead_claims": len(dead_claims),
            "absent_claims": len(absent_claims),
        },
        "weak_positive_rows": len(weak),
        "problems": problems,
        "ok": not problems,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=" * 68)
        print("台账声明校验（IMPLEMENTATION.md）")
        print("=" * 68)
        print(f"解析主题行        : {len(rows)}")
        print(f"状态计数          : {actual}")
        print(f"索引符号          : {len(index)}")
        print(f"自动抽取的声明     : 零调用 {len(dead_claims)} 条 / "
              f"不存在 {len(absent_claims)} 条")
        if dead_claims:
            print("  零调用声明明细    : "
                  + ", ".join(f"{c['symbol']}({c['theme']})"
                              for c in dead_claims))
        if absent_claims:
            print("  不存在声明明细    : "
                  + ", ".join(f"{c['pattern']}"
                              f"@{c['scope'] or '全仓'}" for c in absent_claims))
        print(f"证据不足的 ✅ 行   : {len(weak)}")
        print(f"判错              : {len(problems)}")
        print()
        if weak:
            print("--- ✅ 行证据不足（需补 file:line 引用或符号名）---")
            for w in weak:
                print(f"  {w['theme']:<32} {w['detail']}")
            print()
        if not problems:
            print("✅ 台账声明全部通过机器校验")
        else:
            by_kind: dict[str, list] = defaultdict(list)
            for p in problems:
                by_kind[p["kind"]].append(p)
            for kind, items in by_kind.items():
                print(f"--- {kind} ({len(items)}) ---")
                for p in items:
                    theme = p.get("theme", "")
                    detail = p.get("detail", "")
                    extra = (f"引用 {p['citation']} (台账第 {p['ledger_line']} 行)"
                             if "citation" in p else
                             f"(台账第 {p.get('ledger_line', '?')} 行)")
                    print(f"  {theme:<8} {detail} {extra}")
                print()

    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
