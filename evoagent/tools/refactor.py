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
import keyword
import os
import symtable
import unicodedata
import sys
import tokenize
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .coding import _MAX_SOURCE_BYTES, _SKIP_DIRS

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
            LANGUAGE_SERVICE_ID,
            LANGUAGE_SERVICE_VERSION,
            None,
            False,
            False,
            f"AST 服务不可用: {exc}",
        )
    return LanguageServiceStatus(
        LANGUAGE_SERVICE_ID,
        LANGUAGE_SERVICE_VERSION,
        LANGUAGE_SERVICE_VERSION,
        True,
        False,
        "仅语法/结构级诊断；语义级 LSP 未实现，不宣称支持",
    )


# ---- 诊断与前后对照 ----


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int
    column: int
    message: str

    def key(self) -> Tuple[str, int, int, str]:
        return (self.path, self.line, self.column, self.message)


class RefactorScopeError(ValueError):
    pass


def _py_files(root: Path) -> List[Path]:
    if not root.is_dir():
        raise RefactorScopeError("项目根不存在或不是目录")
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")
        )
        for name in dirs:
            path = Path(directory) / name
            if path.is_symlink() or path.resolve() != path:
                raise RefactorScopeError("项目范围含目录链接，需先明确分析范围")
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = Path(directory) / name
            if path.is_symlink() or path.resolve() != path:
                raise RefactorScopeError("项目范围含文件链接，拒绝读取外部源码")
            files.append(path)
    return sorted(files)


def _source(path):
    with path.open("rb") as stream:
        raw = stream.read(_MAX_SOURCE_BYTES + 1)
    if len(raw) > _MAX_SOURCE_BYTES:
        raise RefactorScopeError("源码文件超过分析字节上限")
    # coding_patch reads in universal-newline mode; its full-file old_text
    # guard must receive the same logical text on Windows and Unix.
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _rel(path: Path, base: Path) -> str:
    """仓库内相对路径统一用正斜杠，跨平台一致。"""
    return path.relative_to(base).as_posix()


def collect_diagnostics(root: str | Path) -> Tuple[Diagnostic, ...]:
    """采集语法级诊断（固定解释器版本）。解析全部 .py，报告语法错误。"""
    base = Path(root).resolve()
    out: List[Diagnostic] = []
    try:
        files = _py_files(base)
    except (OSError, RefactorScopeError) as exc:
        return (Diagnostic(".", 0, 0, str(exc)),)
    for path in files:
        rel = _rel(path, base)
        try:
            source = _source(path)
        except (OSError, UnicodeDecodeError, RefactorScopeError) as exc:
            out.append(Diagnostic(rel, 0, 0, f"读取失败: {exc}"))
            continue
        try:
            compile(source, rel, "exec")
        except SyntaxError as exc:
            out.append(
                Diagnostic(
                    rel, int(exc.lineno or 0), int(exc.offset or 0), str(exc.msg)
                )
            )
    return tuple(sorted(out, key=lambda d: d.key()))


@dataclass(frozen=True)
class DiagnosticComparison:
    new: Tuple[Diagnostic, ...]
    fixed: Tuple[Diagnostic, ...]
    remaining: Tuple[Diagnostic, ...]


def compare_diagnostics(
    before: Sequence[Diagnostic], after: Sequence[Diagnostic]
) -> DiagnosticComparison:
    """重构前后诊断对照：新增 / 消失 / 存留（按多重集计数）。"""

    def counts(items: Sequence[Diagnostic]) -> Dict[Tuple[str, int, int, str], int]:
        result: Dict[Tuple[str, int, int, str], int] = {}
        for diag in items:
            result[diag.key()] = result.get(diag.key(), 0) + 1
        return result

    before_counts = counts(before)
    after_counts = counts(after)

    def expand(source: Dict[Tuple[str, int, int, str], int]) -> Tuple[Diagnostic, ...]:
        return tuple(
            sorted(
                (
                    Diagnostic(*key)
                    for key, count in source.items()
                    for _ in range(count)
                ),
                key=lambda d: d.key(),
            )
        )

    new = expand(
        {k: max(0, v - before_counts.get(k, 0)) for k, v in after_counts.items()}
    )
    fixed = expand(
        {k: max(0, v - after_counts.get(k, 0)) for k, v in before_counts.items()}
    )
    remaining = expand(
        {k: min(v, after_counts.get(k, 0)) for k, v in before_counts.items()}
    )
    return DiagnosticComparison(new=new, fixed=fixed, remaining=remaining)


# ---- 跨文件引用分析与重构计划 ----

_DYNAMIC_CALLS = {
    "getattr",
    "setattr",
    "globals",
    "locals",
    "eval",
    "exec",
    "vars",
    "__import__",
}


@dataclass(frozen=True)
class RenameDecision:
    status: str  # ok | needs_clarification
    symbol: str
    new_name: str
    references: int
    definition_modules: Tuple[str, ...]
    edits: Tuple[Dict[str, str], ...]
    questions: Tuple[str, ...]
    language_service_version: str


def _module_name(rel):
    parts = list(Path(rel).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _from_module(node, rel):
    if not node.level:
        return node.module or ""
    parts = list(Path(rel).with_suffix("").parts)[:-1]
    if Path(rel).name == "__init__.py":
        parts = list(Path(rel).parent.parts)
        if parts == ["."]:
            parts = []
    if node.level > len(parts) + 1:
        return None
    parts = parts[: len(parts) - node.level + 1]
    return ".".join(parts + ([node.module] if node.module else []))


def _local_binding(table, name):
    for child in table.get_children():
        if name in child.get_identifiers():
            symbol = child.lookup(name)
            if symbol.is_local() and not symbol.is_global():
                return True
        if _local_binding(child, name):
            return True
    return False


def _binding_nodes(tree, name):
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            yield node
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and node.id == name
        ):
            yield node
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            yield node
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
            yield node
        elif isinstance(node, ast.MatchMapping) and node.rest == name:
            yield node


def _top_definition(tree, name):
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            yield node
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            yield node
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            yield node


def _namespace_dict_write(node):
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, (ast.Store, ast.Del))
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "__dict__"
    )


def analyze_rename(root: str | Path, symbol: str, new_name: str) -> RenameDecision:
    """Prove project-local identity or refuse; this is not a semantic LSP."""
    base = Path(root).resolve()
    symbol, new_name = str(symbol).strip(), str(new_name).strip()

    def clarify(questions, references=0, modules=()):
        return RenameDecision(
            "needs_clarification",
            symbol,
            new_name,
            references,
            tuple(modules),
            (),
            tuple(sorted(set(questions))),
            LANGUAGE_SERVICE_VERSION,
        )

    if symbol == new_name or any(
        not n.isidentifier()
        or keyword.iskeyword(n)
        or unicodedata.normalize("NFKC", n) != n
        for n in (symbol, new_name)
    ):
        return clarify(["符号名和新名必须是不同、规范且非关键字的Python标识符"])
    try:
        files = _py_files(base)
    except (OSError, RefactorScopeError) as exc:
        return clarify([str(exc)])
    if not files:
        return clarify(["项目内没有可分析的 .py 文件"])
    data = {}
    questions = []
    definitions = []
    top = []
    for path in files:
        rel = _rel(path, base)
        try:
            source = _source(
                path
            )  # One consistent snapshot, never re-read while planning.
            compile(source, rel, "exec")
            tree = ast.parse(source, filename=rel)
            names = [
                t
                for t in tokenize.generate_tokens(io.StringIO(source).readline)
                if t.type == tokenize.NAME
            ]
            scope = symtable.symtable(source, rel, "exec")
        except (
            SyntaxError,
            tokenize.TokenError,
            OSError,
            UnicodeError,
            RefactorScopeError,
        ) as exc:
            questions.append(
                f"{rel} 无法解析，需先修复或限定范围（{type(exc).__name__}）"
            )
            continue
        data[path] = (source, tree, names, scope)
        definitions.extend((path, node) for node in _binding_nodes(tree, symbol))
        top.extend((path, node) for node in _top_definition(tree, symbol))
        if any(t.string == new_name for t in names) or any(
            isinstance(n, ast.Name) and n.id == new_name for n in ast.walk(tree)
        ):
            questions.append(f"{rel} 已存在名为 {new_name} 的标识符，重命名会冲突")
        if any(
            unicodedata.normalize("NFKC", t.string) == symbol and t.string != symbol
            for t in names
        ):
            questions.append(f"{rel} 存在同一符号的非规范化拼写，无法证明完整引用")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                a.name == "*" for a in node.names
            ):
                questions.append(f"{rel} 含星号导入，无法确定全部引用")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _DYNAMIC_CALLS
            ):
                questions.append(f"{rel} 含动态引用，需明确分析范围")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("import_module", "exec_module")
            ):
                questions.append(f"{rel} 含动态模块装载，需明确分析范围")
        if _local_binding(scope, symbol):
            questions.append(f"{rel} 存在局部/参数绑定，无法证明与目标符号相同")
    modules = sorted({_rel(path, base) for path, _ in definitions})
    if len(modules) > 1:
        questions.append(
            f"符号 {symbol} 在多个模块定义（{', '.join(modules)}），请指明目标"
        )
    elif len(definitions) > 1:
        questions.append("同一符号存在多处绑定，不能作全局同形词替换")
    if len(top) != 1 or len(definitions) != 1:
        questions.append(f"找不到符号 {symbol} 的唯一模块级定义，请明确目标")
    if questions:
        return clarify(questions, modules=modules)
    definition_path, definition_node = top[0]
    target_modules = {_module_name(_rel(definition_path, base))}
    if (base / "__init__.py").is_file():
        target_modules |= {base.name + ("." + m if m else "") for m in target_modules}
    module_sources = {}
    root_is_package = (base / "__init__.py").is_file()
    for source_path, (_text, source_tree, _names, _scope) in data.items():
        name = _module_name(_rel(source_path, base))
        variants = {name}
        if root_is_package:
            variants.add(base.name + ("." + name if name else ""))
        for variant in variants:
            module_sources.setdefault(variant, []).append((source_path, source_tree))

    @lru_cache(maxsize=None)
    def ambiguous_module(module):
        # A dotted import is not proof that a package attribute is that module.
        if len(module_sources.get(module, ())) != 1:
            return True
        parts = module.split(".")
        for cut in range(len(parts)):
            parent, child = ".".join(parts[:cut]), parts[cut]
            parents = module_sources.get(parent, ())
            if len(parents) > 1:
                return True
            for parent_path, parent_tree in parents:
                if parent_path.name != "__init__.py" or list(_binding_nodes(parent_tree, child)):
                    return True
                parent_names = {name for name, sources in module_sources.items()
                                if any(path == parent_path for path, _tree in sources)}
                child_names = {(name + "." if name else "") + child for name in parent_names}
                for statement in ast.walk(parent_tree):
                    if (isinstance(statement, ast.Attribute)
                            and isinstance(statement.ctx, (ast.Store, ast.Del))
                            and statement.attr == child):
                        return True
                    if _namespace_dict_write(statement):
                        if not isinstance(statement.slice, ast.Constant) or statement.slice.value == child:
                            return True
                    if (isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and statement.name in ("__getattr__", "__getattribute__")):
                        return True
                    if isinstance(statement, ast.Import):
                        for item in statement.names:
                            if (item.asname or item.name.split(".")[0]) == child and item.name not in child_names:
                                return True
                    elif isinstance(statement, ast.ImportFrom):
                        for item in statement.names:
                            if (item.asname or item.name) == child and (
                                item.name != child or _from_module(statement, _rel(parent_path, base)) not in parent_names
                            ):
                                return True
        return False

    planned = []
    total = 0
    for path, (source, tree, names, scope) in data.items():
        rel = _rel(path, base)
        lines = source.splitlines(keepends=True)
        spans = set()
        direct = path == definition_path
        aliases = {}
        used_aliases = set()

        def column(row, byte_offset):
            return len(lines[row - 1].encode("utf-8")[:byte_offset].decode("utf-8"))

        def add(row, start, end):
            if lines[row - 1][start:end] != symbol:
                questions.append(f"{rel} 发现无法证明的符号拼写/位置")
            else:
                spans.add((row, start, end))

        def add_node_name(node):
            row = node.lineno
            add(row, column(row, node.col_offset), column(row, node.end_col_offset))

        def module_value(node):
            if isinstance(node, ast.Name):
                value = aliases.get(node.id)
                if value is not None:
                    used_aliases.add(node.id)
                return value
            if isinstance(node, ast.Attribute):
                parent = module_value(node.value)
                return parent + "." + node.attr if parent else None
            return None

        for node in tree.body:
            if isinstance(node, ast.Import):
                for item in node.names:
                    local = item.asname or item.name.split(".")[0]
                    module = item.name if item.asname else item.name.split(".")[0]
                    if local == symbol:
                        questions.append(f"{rel} 模块别名与目标同名，需澄清")
                    if local in aliases and aliases[local] != module:
                        questions.append(f"{rel} 模块别名被重复绑定，需澄清")
                    aliases[local] = module
            elif isinstance(node, ast.ImportFrom):
                module = _from_module(node, rel)
                for item in node.names:
                    local = item.asname or item.name
                    resolved = (module + "." if module else "") + item.name if module is not None else None
                    if local in aliases and aliases[local] != resolved:
                        questions.append(f"{rel} 导入别名被重复绑定，无法证明每处引用身份")
                    aliases[local] = resolved
                    if item.name == symbol:
                        if module not in target_modules:
                            questions.append(
                                f"{rel} 同名导入来自其他/未知模块，无法证明身份"
                            )
                            continue
                        direct |= item.asname is None or item.asname == symbol
                        for token in names:
                            if (
                                token.string == symbol
                                and token.start
                                >= (item.lineno, column(item.lineno, item.col_offset))
                                and token.end
                                <= (
                                    item.end_lineno,
                                    column(item.end_lineno, item.end_col_offset),
                                )
                            ):
                                add(token.start[0], token.start[1], token.end[1])
                    elif local == symbol:
                        questions.append(f"{rel} 同名导入绑定了其他对象，需澄清")
        rebound_modules = set()
        for candidate in ast.walk(tree):
            if isinstance(candidate, ast.Attribute) and isinstance(candidate.ctx, (ast.Store, ast.Del)):
                resolved = module_value(candidate)
                if resolved:
                    rebound_modules.add(resolved)
            elif _namespace_dict_write(candidate):
                owner = module_value(candidate.value.value)
                if owner:
                    key = candidate.slice.value if isinstance(candidate.slice, ast.Constant) else None
                    rebound_modules.add(owner + "." + key if isinstance(key, str) and key != symbol else owner)
        if path == definition_path and isinstance(
            definition_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            token = next(
                t
                for t in names
                if t.string == symbol and t.start[0] == definition_node.lineno
            )
            add(token.start[0], token.start[1], token.end[1])
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == symbol:
                if direct:
                    add_node_name(node)
                else:
                    questions.append(f"{rel} 未证明同名引用的导入来源")
            elif isinstance(node, ast.Attribute) and node.attr == symbol:
                owner_module = module_value(node.value)
                if (owner_module in target_modules and not ambiguous_module(owner_module)
                        and not any(owner_module == ref or owner_module.startswith(ref + ".") for ref in rebound_modules)):
                    end = column(node.end_lineno, node.end_col_offset)
                    add(node.end_lineno, end - len(symbol), end)
                else:
                    questions.append(f"{rel} 属性引用无法证明属于目标符号")
            elif isinstance(node, ast.Global) and symbol in node.names:
                if not direct:
                    questions.append(f"{rel} global引用来源不明")
                for token in names:
                    if token.string == symbol and token.start[0] == node.lineno:
                        add(token.start[0], token.start[1], token.end[1])
            elif isinstance(node, ast.Nonlocal) and symbol in node.names:
                questions.append(f"{rel} nonlocal绑定需语义服务确认")
            elif (
                isinstance(node, ast.ImportFrom)
                and node not in tree.body
                and any(a.name == symbol for a in node.names)
            ):
                questions.append(f"{rel} 条件/局部导入需先明确作用域")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotations = [
                    arg.annotation
                    for arg in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                        node.args.vararg,
                        node.args.kwarg,
                    )
                    if arg is not None
                ] + [node.returns]
            elif isinstance(node, ast.AnnAssign):
                annotations = [node.annotation]
            else:
                annotations = []
            for annotation in annotations:
                if annotation is not None and any(
                    isinstance(a, ast.Constant)
                    and isinstance(a.value, str)
                    and symbol in a.value
                    for a in ast.walk(annotation)
                ):
                    questions.append(f"{rel} 字符串类型注解需语义服务确认")
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
            ):
                if any(
                    isinstance(a, ast.Constant) and a.value == symbol
                    for a in ast.walk(node.value)
                ):
                    questions.append(f"{rel} 显式导出字符串需确认重命名范围")
        for alias in used_aliases:
            if list(_binding_nodes(tree, alias)) or _local_binding(scope, alias):
                questions.append(f"{rel} 模块别名被赋值或局部遮蔽，无法证明属性身份")
        if not spans:
            continue
        total += len(spans)
        changed = list(lines)
        for row, start, end in sorted(spans, reverse=True):
            changed[row - 1] = (
                changed[row - 1][:start] + new_name + changed[row - 1][end:]
            )
        replacement = "".join(changed)
        try:
            compile(replacement, rel, "exec")
        except SyntaxError:
            questions.append(f"{rel} 重命名计划无法编译，拒绝生成")
        planned.append({"path": rel, "old_text": source, "new_text": replacement})
    if questions:
        return clarify(questions, total, modules)
    return RenameDecision(
        "ok",
        symbol,
        new_name,
        total,
        tuple(modules),
        tuple(planned),
        (),
        LANGUAGE_SERVICE_VERSION,
    )
