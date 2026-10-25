"""跨文件语义验证与重构门禁（ALN-18-L）。

能力（对应任务卡验收）：

- **跨文件语义验证**：用 tokenize 精确定位符号的所有出现（跨模块），排除字符串/
  注释中的同形词，据此生成重构计划。
- **固定语言服务**：诊断由宿主 Python 的 AST/编译服务提供，版本即解释器版本，
  保证可复现；语义级 LSP **未实现**，如实标注 ``is_semantic=False``，不宣称支持。
- **诊断/修改前后对照**：``compare_diagnostics`` 输出新增/消失/存留，供重构前后核对。
- **无法确定的重构请求澄清**：符号跨模块重名、星号导入、动态引用（getattr/globals/
  eval 等）、新名冲突、不可解析文件等不确定情形一律拒绝执行并返回澄清问题，不猜测。

本模块只产出计划与诊断，不自行写盘；实际改写交给既有原子工具 ``coding_patch``。
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

#: 固定语言服务：宿主 Python 的 AST/编译服务（仅语法/结构级，非语义 LSP）。
LANGUAGE_SERVICE_ID = "python-ast"
LANGUAGE_SERVICE_VERSION = (
    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
)


@dataclass(frozen=True)
class LanguageServiceStatus:
    service_id: str
    required_version: str
    version: Optional[str]
    available: bool
    is_semantic: bool
    reason: str


def language_service_status() -> LanguageServiceStatus:
    """探测语言服务：不可用或版本不符一律 ``available=False``，不判成功。"""
    try:
        compile("pass", "<probe>", "exec")
    except Exception as exc:  # pragma: no cover - 解释器损坏时
        return LanguageServiceStatus(
            LANGUAGE_SERVICE_ID, LANGUAGE_SERVICE_VERSION, None, False, False,
            f"AST 服务不可用: {exc}")
    return LanguageServiceStatus(
        LANGUAGE_SERVICE_ID, LANGUAGE_SERVICE_VERSION, LANGUAGE_SERVICE_VERSION,
        True, False, "仅语法/结构级诊断；语义级 LSP 未实现，不宣称支持")


# ---- 诊断与前后对照 ----

@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int
    column: int
    message: str

    def key(self) -> Tuple[str, int, int, str]:
        return (self.path, self.line, self.column, self.message)


def _py_files(root: Path) -> List[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _rel(path: Path, base: Path) -> str:
    """仓库内相对路径统一用正斜杠，跨平台一致。"""
    return path.relative_to(base).as_posix()


def collect_diagnostics(root: str | Path) -> Tuple[Diagnostic, ...]:
    """采集语法级诊断（固定解释器版本）。解析全部 .py，报告语法错误。"""
    base = Path(root)
    out: List[Diagnostic] = []
    for path in _py_files(base):
        rel = _rel(path, base)
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            out.append(Diagnostic(rel, 0, 0, f"读取失败: {exc}"))
            continue
        try:
            ast.parse(source, filename=rel)
        except SyntaxError as exc:
            out.append(Diagnostic(rel, int(exc.lineno or 0),
                                  int(exc.offset or 0), str(exc.msg)))
    return tuple(sorted(out, key=lambda d: d.key()))


@dataclass(frozen=True)
class DiagnosticComparison:
    new: Tuple[Diagnostic, ...]
    fixed: Tuple[Diagnostic, ...]
    remaining: Tuple[Diagnostic, ...]


def compare_diagnostics(before: Sequence[Diagnostic],
                        after: Sequence[Diagnostic]) -> DiagnosticComparison:
    """重构前后诊断对照：新增 / 消失 / 存留（按多重集计数）。"""
    def counts(items: Sequence[Diagnostic]) -> Dict[Tuple[str, int, int, str], int]:
        result: Dict[Tuple[str, int, int, str], int] = {}
        for diag in items:
            result[diag.key()] = result.get(diag.key(), 0) + 1
        return result

    before_counts = counts(before)
    after_counts = counts(after)

    def expand(source: Dict[Tuple[str, int, int, str], int]) -> Tuple[Diagnostic, ...]:
        return tuple(sorted(
            (Diagnostic(*key) for key, count in source.items() for _ in range(count)),
            key=lambda d: d.key()))

    new = expand({k: max(0, v - before_counts.get(k, 0))
                  for k, v in after_counts.items()})
    fixed = expand({k: max(0, v - after_counts.get(k, 0))
                    for k, v in before_counts.items()})
    remaining = expand({k: min(v, after_counts.get(k, 0))
                        for k, v in before_counts.items()})
    return DiagnosticComparison(new=new, fixed=fixed, remaining=remaining)


# ---- 跨文件引用分析与重构计划 ----

_DYNAMIC_CALLS = {"getattr", "setattr", "globals", "locals", "eval", "exec", "vars"}


@dataclass(frozen=True)
class RenameDecision:
    status: str                      # ok | needs_clarification
    symbol: str
    new_name: str
    references: int
    definition_modules: Tuple[str, ...]
    edits: Tuple[Dict[str, str], ...]
    questions: Tuple[str, ...]
    language_service_version: str


def _name_positions(path: Path, symbol: str) -> List[Tuple[int, int, int]]:
    """tokenize 精确定位 NAME 记号（排除字符串/注释中的同形词）。"""
    source = path.read_text(encoding="utf-8")
    found: List[Tuple[int, int, int]] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.NAME and tok.string == symbol:
            found.append((tok.start[0], tok.start[1], tok.end[1]))
    return found


def _module_facts(path: Path, symbol: str) -> Tuple[int, bool, bool]:
    """返回 (该模块内符号的定义数, 是否有星号导入, 是否有动态引用调用)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definitions = 0
    star_import = False
    dynamic = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                definitions += 1
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == symbol
                   for t in node.targets):
                definitions += 1
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == symbol:
                definitions += 1
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                star_import = True
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _DYNAMIC_CALLS:
            dynamic = True
    return definitions, star_import, dynamic


def analyze_rename(root: str | Path, symbol: str, new_name: str) -> RenameDecision:
    """分析跨文件重命名；不确定即返回澄清问题，不产出可执行计划。"""
    base = Path(root)
    symbol = str(symbol).strip()
    new_name = str(new_name).strip()
    service_version = LANGUAGE_SERVICE_VERSION

    def clarify(questions: List[str], references: int = 0,
                modules: Sequence[str] = ()) -> RenameDecision:
        return RenameDecision("needs_clarification", symbol, new_name,
                              references, tuple(modules), (), tuple(questions),
                              service_version)

    if not symbol or not new_name or symbol == new_name:
        return clarify(["符号名与新名必须非空且不同"], 0, ())

    files = _py_files(base)
    if not files:
        return clarify(["项目内没有可分析的 .py 文件"], 0, ())

    questions: List[str] = []
    per_file: Dict[Path, List[Tuple[int, int, int]]] = {}
    definition_modules: List[str] = []
    total = 0
    for path in files:
        rel = _rel(path, base)
        try:
            positions = _name_positions(path, symbol)
            defs, star, dynamic = _module_facts(path, symbol)
        except (SyntaxError, tokenize.TokenError, OSError) as exc:
            questions.append(f"{rel} 无法解析（{exc}），需先修复或限定范围")
            continue
        if defs:
            definition_modules.append(rel)
        if star:
            questions.append(f"{rel} 含星号导入，可能间接引入 {symbol}，无法确定全部引用")
        if dynamic:
            questions.append(f"{rel} 含动态引用（getattr/globals/eval 等），"
                             f"无法确定 {symbol} 是否被动态访问")
        if positions:
            per_file[path] = positions
            total += len(positions)
        if _name_positions(path, new_name):
            questions.append(f"{rel} 已存在名为 {new_name} 的标识符，重命名会冲突")

    if total == 0:
        return clarify([f"项目内找不到符号 {symbol} 的定义或引用"], 0, ())
    if len(definition_modules) > 1:
        questions.append(
            f"符号 {symbol} 在多个模块定义（{', '.join(definition_modules)}），"
            "请指明要重构哪一个，避免跨模块误改")
    if questions:
        return clarify(sorted(set(questions)), total, tuple(definition_modules))

    edits: List[Dict[str, str]] = []
    for path, positions in per_file.items():
        rel = _rel(path, base)
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines(keepends=True)
        by_line: Dict[int, List[Tuple[int, int]]] = {}
        for row, col_start, col_end in positions:
            by_line.setdefault(row, []).append((col_start, col_end))
        for row, spans in by_line.items():
            original = lines[row - 1]
            rebuilt = original
            # 同一行多处出现按列降序替换，避免位移错位
            for col_start, col_end in sorted(spans, reverse=True):
                rebuilt = rebuilt[:col_start] + new_name + rebuilt[col_end:]
            lines[row - 1] = rebuilt
        new_source = "".join(lines)
        if new_source == source:
            return clarify([f"{rel} 计划无变化，拒绝空改写"],
                           total, tuple(definition_modules))
        # 整文件作为 old_text：唯一，且满足 coding_patch「单文件一条编辑」约束
        edits.append({"path": rel, "old_text": source, "new_text": new_source})

    return RenameDecision("ok", symbol, new_name, total,
                          tuple(sorted(definition_modules)), tuple(edits), (),
                          service_version)
