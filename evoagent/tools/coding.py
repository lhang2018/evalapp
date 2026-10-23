"""真实编程能力包（ALN-18-A / C-12，WP-4.1）。

定位 → 改写 → 检查/测试，全部以项目根为边界：
- 符号定位用 ``ast``，返回 ``file:line`` 与限定名；语法错误单列，不静默跳过。
- 改写要求旧文本唯一命中；``.py`` 改写后语法不合法一律拒绝写盘，写后重读比对。
- 语法检查/类型检查/测试都是真实子进程，非零退出不得被判成功；
  类型检查器不可用时返回「不可用」，不以成功冒充。

本模块不接管执行路径，也不在 App 启动时自动注册；接线另卡。
"""

import ast
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .contract import ToolContract, ToolResult, class_error

_PACK_ID = "coding_pack"
_PACK_VERSION = "1.3.0"
_ROLLBACK_VERSION = "1.2.0"
_CONTRACT_TEST_REF = "tests/test_coding_tools.py"
_JOURNAL_DIR = ".evoagent_patch_journal"
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".mypy_cache", ".pytest_cache", "build", "dist", ".tox",
    _JOURNAL_DIR,
}
_MAX_SOURCE_BYTES = 2_000_000


def _resolve_root(root: str) -> Path:
    p = Path(root or ".").resolve()
    if not p.is_dir():
        raise ValueError(f"项目根不存在或不是目录: {root}")
    return p


def _resolve_within(rootp: Path, rel: str) -> Path:
    raw = str(rel or ".")
    if raw.startswith("-"):
        raise ValueError(f"非法路径: {raw}")
    candidate = Path(raw)
    p = candidate.resolve() if candidate.is_absolute() \
        else (rootp / candidate).resolve()
    if not p.is_relative_to(rootp):
        raise ValueError(f"路径越界: {raw}")
    return p


def _tail(text: str, limit: int = 2000) -> str:
    text = text or ""
    return text[-limit:]


def _run(cmd: List[str], cwd: Path, timeout: float,
         env: Optional[Dict[str, str]] = None):
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout, check=False, env=env)
        return proc, None
    except subprocess.TimeoutExpired as exc:
        return None, ("timeout", exc)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, ("error", exc)


def _check_targets(rootp: Path, targets: List[str]) -> List[Path]:
    resolved = []
    for raw in targets:
        p = _resolve_within(rootp, raw)
        if not p.exists():
            raise ValueError(f"目标不存在: {raw}")
        resolved.append(p)
    return resolved


def _signature(rel: str, node: ast.AST) -> Dict[str, object]:
    entry = {
        "name": node.name,
        "file": rel,
        "line": node.lineno,
        "end_line": getattr(node, "end_lineno", None),
    }
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        entry["signature"] = f"({ast.unparse(node.args)})"
        if node.returns is not None:
            entry["returns"] = ast.unparse(node.returns)
    return entry


def _module_name(rel: str) -> str:
    name = rel[:-3] if rel.endswith(".py") else rel
    name = name.replace("/", ".")
    if name == "__init__" or name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    return name


def analyze_project(root: str = ".", subdir: str = "", name_filter: str = "",
                    limit: int = 500) -> Dict[str, object]:
    rootp = _resolve_root(root)
    target = rootp if not subdir else _resolve_within(rootp, subdir)
    if not target.is_dir():
        raise ValueError(f"不是目录: {subdir}")
    limit = max(1, min(5000, int(limit)))
    needle = (name_filter or "").lower()

    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
            and (Path(dirpath) / d).resolve().is_relative_to(rootp))
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                path = Path(dirpath) / fn
                if path.resolve().is_relative_to(rootp):
                    files.append(path)

    symbols: List[Dict[str, object]] = []
    errors: List[Dict[str, object]] = []
    imports: Dict[str, List[str]] = {}
    truncated = False
    for path in files:
        rel = path.relative_to(rootp).as_posix()
        try:
            size = path.stat().st_size
            if size > _MAX_SOURCE_BYTES:
                errors.append({"file": rel, "error": "文件过大，跳过"})
                continue
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append({"file": rel, "error": str(exc)})
            continue
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            errors.append({"file": rel, "line": exc.lineno,
                           "error": exc.msg})
            continue

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
        if imported:
            imports[rel] = sorted(imported)

        stack: List[str] = []

        def visit(node: ast.AST) -> None:
            nonlocal truncated
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef,
                                      ast.AsyncFunctionDef)):
                    qual = ".".join(stack + [child.name])
                    kind = ("class" if isinstance(child, ast.ClassDef)
                            else "function")
                    if not needle or needle in child.name.lower():
                        if len(symbols) >= limit:
                            truncated = True
                        else:
                            entry = _signature(rel, child)
                            entry.update({"kind": kind, "qualname": qual})
                            symbols.append(entry)
                    stack.append(child.name)
                    visit(child)
                    stack.pop()
                else:
                    visit(child)

        visit(tree)

    return {
        "root": str(rootp),
        "files_scanned": len(files),
        "symbols": symbols,
        "imports": imports,
        "errors": errors,
        "truncated": truncated,
    }


def find_references(root: str = ".", name: str = "", subdir: str = "",
                    limit: int = 500) -> Dict[str, object]:
    """静态引用/调用启发式：类型未解析，按导入关系区分置信度。"""
    rootp = _resolve_root(root)
    if not name:
        raise ValueError("name 不能为空")
    target = rootp if not subdir else _resolve_within(rootp, subdir)
    if not target.is_dir():
        raise ValueError(f"不是目录: {subdir}")
    limit = max(1, min(5000, int(limit)))

    analysis = analyze_project(str(rootp), subdir=subdir, name_filter=name,
                               limit=5000)
    definitions = [s for s in analysis["symbols"] if s["name"] == name]
    def_files = {str(s["file"]) for s in definitions}
    def_prefixes = set()
    for rel in def_files:
        parts = _module_name(rel).split(".")
        for i in range(1, len(parts) + 1):
            def_prefixes.add(".".join(parts[:i]))
    imports_map = analysis["imports"]

    references: List[Dict[str, object]] = []
    import_edges: List[Dict[str, str]] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
            and (Path(dirpath) / d).resolve().is_relative_to(rootp))
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            path = Path(dirpath) / fn
            if not path.resolve().is_relative_to(rootp):
                continue
            rel = path.relative_to(rootp).as_posix()
            try:
                if path.stat().st_size > _MAX_SOURCE_BYTES:
                    continue
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=rel)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue

            imported = set(imports_map.get(rel, []))
            matched = sorted(m for m in imported if m in def_prefixes)
            for module in matched:
                import_edges.append({"file": rel, "module": module})
            linked = rel in def_files or bool(matched)
            name_conf = "high" if linked else "low"
            attr_conf = "medium" if linked else "low"

            parents = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[id(child)] = parent

            stack: List[str] = []

            def visit(node: ast.AST) -> None:
                nonlocal truncated
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.ClassDef, ast.FunctionDef,
                                          ast.AsyncFunctionDef)):
                        stack.append(child.name)
                        visit(child)
                        stack.pop()
                        continue
                    kind = None
                    confidence = None
                    if isinstance(child, ast.Name) and child.id == name:
                        kind, confidence = "name", name_conf
                    elif isinstance(child, ast.Attribute) and child.attr == name:
                        kind, confidence = "attribute", attr_conf
                    if kind:
                        parent = parents.get(id(child))
                        if isinstance(parent, ast.Call) and parent.func is child:
                            kind = "call"
                        if len(references) >= limit:
                            truncated = True
                        else:
                            references.append({
                                "file": rel,
                                "line": child.lineno,
                                "kind": kind,
                                "context": ".".join(stack) or None,
                                "confidence": confidence,
                            })
                    visit(child)

            visit(tree)

    return {
        "name": name,
        "definitions": definitions,
        "references": references,
        "import_edges": import_edges,
        "files_scanned": analysis["files_scanned"],
        "errors": analysis["errors"],
        "truncated": truncated,
    }


def coding_references_tool(root: str = ".") -> Tuple[ToolContract, Callable]:
    def run(args: Dict) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name:
            return ToolResult.error_result("name 不能为空", "invalid_arguments")
        try:
            data = find_references(
                root, name=name,
                subdir=str(args.get("subdir") or ""),
                limit=max(1, min(5000, int(args.get("limit") or 500))),
            )
        except (ValueError, TypeError) as exc:
            return ToolResult.error_result(str(exc), "invalid_arguments")
        except OSError as exc:
            return ToolResult.error_result(str(exc), "tool_defect_or_env")
        return ToolResult(
            ok=True, data=data, submitted=True, executed=True,
            contract_valid=True, goal_reached=True,
            independently_verified=True,
            evidence={"definitions": len(data["definitions"]),
                      "references": len(data["references"]),
                      "import_edges": len(data["import_edges"]),
                      "parse_errors": len(data["errors"]),
                      "truncated": data["truncated"]})

    contract = ToolContract(
        tool_id="coding_references",
        description="符号引用/调用点静态定位（导入关系区分置信度）",
        tool_version=_PACK_VERSION,
        permission_requirements=["fs.read"],
        input_schema={"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"},
            "subdir": {"type": "string"},
            "limit": {"type": "integer"},
        }},
        side_effect_level="none",
        idempotency_policy="none",
        timeout_policy=60.0,
        health_check=lambda: Path(root or ".").is_dir(),
        risk_class="low",
        offline_capable=True,
        requires_network=False,
        cost_model={"unit": "files", "varies": "project_size"},
        contract_test_ref=_CONTRACT_TEST_REF,
    )
    return contract, run


def coding_symbols_tool(root: str = ".") -> Tuple[ToolContract, Callable]:
    def run(args: Dict) -> ToolResult:
        try:
            limit = max(1, min(5000, int(args.get("limit") or 500)))
            data = analyze_project(
                root,
                subdir=str(args.get("subdir") or ""),
                name_filter=str(args.get("name") or ""),
                limit=limit,
            )
        except (ValueError, TypeError) as exc:
            return ToolResult.error_result(str(exc), "invalid_arguments")
        except OSError as exc:
            return ToolResult.error_result(str(exc), "tool_defect_or_env")
        return ToolResult(
            ok=True, data=data, submitted=True, executed=True,
            contract_valid=True, goal_reached=True,
            independently_verified=True,
            evidence={"files_scanned": data["files_scanned"],
                      "symbols": len(data["symbols"]),
                      "parse_errors": len(data["errors"]),
                      "truncated": data["truncated"]})

    contract = ToolContract(
        tool_id="coding_symbols",
        description="项目级 Python 符号定位（AST，file:line）",
        tool_version=_PACK_VERSION,
        permission_requirements=["fs.read"],
        input_schema={"type": "object", "properties": {
            "subdir": {"type": "string"},
            "name": {"type": "string"},
            "limit": {"type": "integer"},
        }},
        side_effect_level="none",
        idempotency_policy="none",
        timeout_policy=30.0,
        health_check=lambda: Path(root or ".").is_dir(),
        risk_class="low",
        offline_capable=True,
        requires_network=False,
        cost_model={"unit": "files", "varies": "project_size"},
        contract_test_ref=_CONTRACT_TEST_REF,
    )
    return contract, run


def _syntax_error(text: str, filename: str) -> Optional[Dict[str, object]]:
    try:
        ast.parse(text, filename=filename)
    except SyntaxError as exc:
        return {"line": exc.lineno, "offset": exc.offset, "message": exc.msg}
    return None


def coding_edit_tool(root: str = ".") -> Tuple[ToolContract, Callable]:
    def run(args: Dict) -> ToolResult:
        rel = str(args.get("path") or "")
        old_text = str(args.get("old_text") or "")
        new_text = args.get("new_text")
        if not rel:
            return ToolResult.error_result("缺少 path", "invalid_arguments")
        if not old_text:
            return ToolResult.error_result("old_text 不能为空", "invalid_arguments")
        if new_text is None:
            return ToolResult.error_result("缺少 new_text", "invalid_arguments")
        new_text = str(new_text)
        try:
            rootp = _resolve_root(root)
            path = _resolve_within(rootp, rel)
        except ValueError as exc:
            return ToolResult.error_result(str(exc), "auth_or_permission")
        if not path.is_file():
            return ToolResult.error_result(f"文件不存在: {rel}",
                                           "invalid_arguments")
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            return ToolResult.error_result("文件过大，拒绝改写",
                                           "invalid_arguments")
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult.error_result(str(exc), class_error(exc))

        digest_before = hashlib.sha256(original.encode("utf-8")).hexdigest()
        expected = args.get("expected_sha256")
        if expected is not None:
            expected = str(expected).strip().lower()
            if len(expected) < 8:
                return ToolResult.error_result(
                    "expected_sha256 至少 8 位", "invalid_arguments")
            if not digest_before.startswith(expected):
                return ToolResult(
                    ok=False, data=None,
                    error="文件摘要与期望不一致（可能已被并发修改），未写盘",
                    error_class="invalid_arguments",
                    submitted=True, executed=True, contract_valid=True,
                    goal_reached=False, independently_verified=True,
                    evidence={"written": False, "path": rel,
                              "expected_sha256": expected,
                              "actual_sha256": digest_before})

        hits = original.count(old_text)
        if hits == 0:
            return ToolResult.error_result("old_text 未命中，未写盘",
                                           "invalid_arguments")
        if hits > 1:
            return ToolResult.error_result(
                f"old_text 命中 {hits} 处，要求唯一，未写盘",
                "invalid_arguments")

        new_content = original.replace(old_text, new_text, 1)
        if path.suffix == ".py":
            syntax_error = _syntax_error(new_content, rel)
            if syntax_error:
                return ToolResult(
                    ok=False, data=None,
                    error=f"改写后语法错误: 第 {syntax_error['line']} 行 "
                          f"{syntax_error['message']}（未写盘）",
                    error_class="invalid_arguments",
                    submitted=True, executed=True, contract_valid=True,
                    goal_reached=False, independently_verified=True,
                    evidence={"written": False, "path": rel,
                              "syntax_checked": True,
                              "syntax_error": syntax_error})
        else:
            syntax_error = None

        try:
            path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return ToolResult.error_result(str(exc), class_error(exc))
        try:
            verified = path.read_text(encoding="utf-8") == new_content
        except OSError:
            verified = False
        evidence = {
            "path": rel,
            "line": original.count("\n", 0, original.index(old_text)) + 1,
            "replacements": 1,
            "written": True,
            "sha256_before": digest_before,
            "sha256_after": hashlib.sha256(
                new_content.encode("utf-8")).hexdigest(),
            "syntax_checked": path.suffix == ".py",
        }
        if not verified:
            restored = False
            try:
                path.write_text(original, encoding="utf-8")
                restored = path.read_text(encoding="utf-8") == original
            except OSError:
                restored = False
            evidence["restored"] = restored
            return ToolResult(
                ok=False, data=None,
                error="写后校验不一致，已尝试回滚",
                error_class="execution_error",
                submitted=True, executed=True, contract_valid=True,
                goal_reached=False, independently_verified=restored,
                side_effect_uncertain=not restored, evidence=evidence)
        return ToolResult(
            ok=True, data=str(path), submitted=True, executed=True,
            contract_valid=True, goal_reached=True,
            independently_verified=True,
            side_effect_uncertain=False, evidence=evidence)

    contract = ToolContract(
        tool_id="coding_edit",
        description="受约束的代码改写（唯一命中 + 语法门禁 + 写后校验）",
        tool_version=_PACK_VERSION,
        permission_requirements=["fs.read", "fs.write"],
        input_schema={"type": "object", "required": ["path", "old_text", "new_text"],
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"},
                                     "expected_sha256": {"type": "string"}}},
        side_effect_level="local",
        idempotency_policy="state-check",
        timeout_policy=10.0,
        health_check=lambda: Path(root or ".").is_dir()
        and os.access(root or ".", os.W_OK),
        risk_class="high",
        offline_capable=True,
        requires_network=False,
        cost_model={"unit": "bytes", "varies": "content_size"},
        contract_test_ref=_CONTRACT_TEST_REF,
    )
    return contract, run


def _rollback_files(prepared: List[Dict[str, object]]) -> List[str]:
    failed = []
    for item in prepared:
        path = item["path"]
        original = item["original"]
        try:
            path.write_text(original, encoding="utf-8")
            if path.read_text(encoding="utf-8") != original:
                failed.append(str(item["rel"]))
        except OSError:
            failed.append(str(item["rel"]))
    return failed


def _journal_path(rootp: Path) -> Path:
    jdir = rootp / _JOURNAL_DIR
    jdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return jdir / f"patch-{stamp}-{uuid.uuid4().hex[:8]}.json"


def _atomic_write_json(path: Path, payload: Dict[str, object]) -> None:
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _write_journal(rootp: Path,
                   prepared: List[Dict[str, object]]) -> Path:
    path = _journal_path(rootp)
    _atomic_write_json(path, {
        "version": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "pending",
        "files": [{"path": str(item["rel"]),
                   "sha256_before": item["sha256_before"],
                   "sha256_after": item["sha256_after"],
                   "original": item["original"]}
                  for item in prepared],
    })
    return path


def _mark_committed(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "committed"
    _atomic_write_json(path, payload)


def _remove_journal(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _sha_file(path: Path) -> str:
    # 与其他工具一致：对解码后的文本取摘要，避免 Windows 换行折算差异
    return hashlib.sha256(
        path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _recover_journals(rootp: Path, dry_run: bool):
    report: List[Dict[str, object]] = []
    errors: List[Dict[str, str]] = []
    restored = 0
    conflicts = 0
    uncertain = False
    jdir = rootp / _JOURNAL_DIR
    if not jdir.is_dir():
        return report, restored, conflicts, errors, uncertain
    for path in sorted(jdir.glob("*.json")):
        entry: Dict[str, object] = {"journal": path.name, "files": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            files = payload.get("files")
            if not isinstance(files, list):
                raise ValueError("缺少 files")
            status = payload.get("status")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append({"journal": path.name, "error": str(exc)})
            continue
        entry["status"] = status
        if status == "committed":
            if not dry_run:
                _remove_journal(path)
            entry["action"] = ("committed_pending_cleanup" if dry_run
                               else "committed")
            report.append(entry)
            continue

        file_rows: List[Dict[str, str]] = []
        unresolved = 0
        for item in files:
            if not isinstance(item, dict):
                item = {}
            rel = str(item.get("path") or "")
            before = str(item.get("sha256_before") or "")
            after = str(item.get("sha256_after") or "")
            original = item.get("original")
            action = "conflict"
            attempted_write = False
            try:
                if not rel or not before or not after \
                        or not isinstance(original, str):
                    raise ValueError("日志条目不完整")
                if hashlib.sha256(
                        original.encode("utf-8")).hexdigest() != before:
                    raise ValueError("原内容摘要与日志不符")
                target = _resolve_within(rootp, rel)
                current = _sha_file(target) if target.is_file() else ""
                if current == after:
                    if not dry_run:
                        attempted_write = True
                        target.write_text(original, encoding="utf-8")
                        if _sha_file(target) != before:
                            raise OSError("恢复后摘要不符")
                    action = "restored"
                    restored += 1
                elif current == before:
                    action = "already_before"
                else:
                    action = "conflict"
                    conflicts += 1
            except (ValueError, OSError) as exc:
                action = "error"
                uncertain = uncertain or attempted_write
                entry.setdefault("errors", []).append(
                    {"file": rel, "error": str(exc)})
            if action in ("conflict", "error"):
                unresolved += 1
            file_rows.append({"file": rel, "sha256_before": before,
                              "sha256_after": after, "action": action})
        entry["files"] = file_rows
        if unresolved == 0 and not dry_run:
            _remove_journal(path)
        report.append(entry)
    return report, restored, conflicts, errors, uncertain


def coding_patch_tool(root: str = ".") -> Tuple[ToolContract, Callable]:
    def run(args: Dict) -> ToolResult:
        edits = args.get("edits")
        dry_run = bool(args.get("dry_run"))
        if not isinstance(edits, list) or not edits:
            return ToolResult.error_result("edits 必须是非空数组",
                                           "invalid_arguments")
        if len(edits) > 50:
            return ToolResult.error_result("单次最多 50 条编辑",
                                           "invalid_arguments")
        try:
            rootp = _resolve_root(root)
        except ValueError as exc:
            return ToolResult.error_result(str(exc), "auth_or_permission")

        prepared: List[Dict[str, object]] = []
        seen = set()
        for index, item in enumerate(edits):
            if not isinstance(item, dict):
                return ToolResult.error_result(
                    f"第 {index + 1} 条编辑不是对象", "invalid_arguments")
            rel = str(item.get("path") or "")
            old_text = str(item.get("old_text") or "")
            if not rel or not old_text or item.get("new_text") is None:
                return ToolResult.error_result(
                    f"第 {index + 1} 条编辑缺少 path/old_text/new_text",
                    "invalid_arguments")
            new_text = str(item.get("new_text"))
            try:
                path = _resolve_within(rootp, rel)
            except ValueError as exc:
                return ToolResult.error_result(str(exc), "auth_or_permission")
            if str(path) in seen:
                return ToolResult.error_result(
                    f"同一文件出现多条编辑: {rel}", "invalid_arguments")
            seen.add(str(path))
            if not path.is_file():
                return ToolResult.error_result(
                    f"文件不存在: {rel}", "invalid_arguments")
            if path.stat().st_size > _MAX_SOURCE_BYTES:
                return ToolResult.error_result(
                    f"文件过大: {rel}", "invalid_arguments")
            try:
                original = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return ToolResult.error_result(str(exc), class_error(exc))

            digest_before = hashlib.sha256(
                original.encode("utf-8")).hexdigest()
            expected = item.get("expected_sha256")
            if expected is not None:
                expected = str(expected).strip().lower()
                if len(expected) < 8:
                    return ToolResult.error_result(
                        f"{rel}: expected_sha256 至少 8 位",
                        "invalid_arguments")
                if not digest_before.startswith(expected):
                    return ToolResult(
                        ok=False, data=None,
                        error=f"{rel}: 文件摘要与期望不一致，未写盘",
                        error_class="invalid_arguments",
                        submitted=True, executed=True, contract_valid=True,
                        goal_reached=False, independently_verified=True,
                        evidence={"written": [], "path": rel,
                                  "expected_sha256": expected,
                                  "actual_sha256": digest_before})

            hits = original.count(old_text)
            if hits != 1:
                return ToolResult.error_result(
                    f"{rel}: old_text 命中 {hits} 处，要求唯一，未写盘",
                    "invalid_arguments")
            if new_text == old_text:
                return ToolResult.error_result(
                    f"{rel}: 内容无变化，未写盘", "invalid_arguments")
            content = original.replace(old_text, new_text, 1)
            if path.suffix == ".py":
                syntax_error = _syntax_error(content, rel)
                if syntax_error:
                    return ToolResult(
                        ok=False, data=None,
                        error=f"{rel}: 改写后语法错误（第 "
                              f"{syntax_error['line']} 行），未写盘",
                        error_class="invalid_arguments",
                        submitted=True, executed=True, contract_valid=True,
                        goal_reached=False, independently_verified=True,
                        evidence={"written": [], "path": rel,
                                  "syntax_error": syntax_error})
            prepared.append({
                "path": path,
                "rel": rel,
                "original": original,
                "new_content": content,
                "sha256_before": digest_before,
                "sha256_after": hashlib.sha256(
                    content.encode("utf-8")).hexdigest(),
                "line": original.count("\n", 0, original.index(old_text)) + 1,
            })

        if dry_run:
            files = []
            for item in prepared:
                diff = "".join(difflib.unified_diff(
                    str(item["original"]).splitlines(True),
                    str(item["new_content"]).splitlines(True),
                    fromfile=str(item["rel"]), tofile=str(item["rel"])))
                files.append({
                    "file": item["rel"],
                    "line": item["line"],
                    "sha256_before": item["sha256_before"],
                    "sha256_after": item["sha256_after"],
                    "diff": _tail(diff, 4000),
                })
            return ToolResult(
                ok=True, data={"dry_run": True, "files": files},
                submitted=True, executed=True, contract_valid=True,
                goal_reached=True, independently_verified=True,
                evidence={"written": [], "planned": len(files),
                          "dry_run": True})

        try:
            journal = _write_journal(rootp, prepared)
        except OSError as exc:
            return ToolResult(
                ok=False, data=None,
                error=f"事务日志写入失败，未写盘: {exc}",
                error_class=class_error(exc),
                submitted=True, executed=True, contract_valid=True,
                goal_reached=False, independently_verified=True,
                side_effect_uncertain=False,
                evidence={"written": [], "rolled_back": False})

        written: List[str] = []
        try:
            for item in prepared:
                item["path"].write_text(item["new_content"], encoding="utf-8")
                written.append(str(item["rel"]))
        except OSError as exc:
            failed = _rollback_files(prepared)
            if not failed:
                _remove_journal(journal)
            return ToolResult(
                ok=False, data=None,
                error=f"写盘失败并回滚: {exc}",
                error_class=class_error(exc),
                submitted=True, executed=True, contract_valid=True,
                goal_reached=False,
                independently_verified=not failed,
                side_effect_uncertain=bool(failed),
                evidence={"written": written, "rolled_back": True,
                          "restore_failed": failed,
                          "journal": None if not failed else journal.name})

        mismatched = []
        for item in prepared:
            try:
                if item["path"].read_text(encoding="utf-8") \
                        != item["new_content"]:
                    mismatched.append(str(item["rel"]))
            except OSError:
                mismatched.append(str(item["rel"]))
        if mismatched:
            failed = _rollback_files(prepared)
            if not failed:
                _remove_journal(journal)
            return ToolResult(
                ok=False, data=None,
                error=f"写后校验不一致: {', '.join(mismatched)}，已回滚",
                error_class="execution_error",
                submitted=True, executed=True, contract_valid=True,
                goal_reached=False,
                independently_verified=not failed,
                side_effect_uncertain=bool(failed),
                evidence={"written": written, "rolled_back": True,
                          "mismatched": mismatched,
                          "restore_failed": failed,
                          "journal": None if not failed else journal.name})

        try:
            _mark_committed(journal)
            _remove_journal(journal)
        except OSError:
            pass

        files = [{"file": item["rel"], "line": item["line"],
                  "sha256_before": item["sha256_before"],
                  "sha256_after": item["sha256_after"]}
                 for item in prepared]
        return ToolResult(
            ok=True, data={"dry_run": False, "applied": written,
                           "files": files},
            submitted=True, executed=True, contract_valid=True,
            goal_reached=True, independently_verified=True,
            evidence={"written": written, "applied": len(written),
                      "rolled_back": False, "dry_run": False})

    contract = ToolContract(
        tool_id="coding_patch",
        description="原子多文件补丁：全量预检、失败全量回滚、dry-run 预览",
        tool_version=_PACK_VERSION,
        permission_requirements=["fs.read", "fs.write"],
        input_schema={"type": "object", "required": ["edits"], "properties": {
            "edits": {"type": "array"},
            "dry_run": {"type": "boolean"},
        }},
        side_effect_level="local",
        idempotency_policy="state-check",
        timeout_policy=30.0,
        health_check=lambda: Path(root or ".").is_dir()
        and os.access(root or ".", os.W_OK),
        risk_class="high",
        offline_capable=True,
        requires_network=False,
        cost_model={"unit": "edits", "varies": "file_count"},
        contract_test_ref=_CONTRACT_TEST_REF,
    )
    return contract, run


def coding_patch_recover_tool(root: str = "."
                              ) -> Tuple[ToolContract, Callable]:
    def run(args: Dict) -> ToolResult:
        dry_run = bool(args.get("dry_run"))
        try:
            rootp = _resolve_root(root)
        except ValueError as exc:
            return ToolResult.error_result(str(exc), "auth_or_permission")

        report, restored, conflicts, errors, uncertain = _recover_journals(
            rootp, dry_run)
        data = {"dry_run": dry_run, "restored": restored,
                "conflicts": conflicts, "journals": report,
                "errors": errors}
        evidence = {"restored": restored, "conflicts": conflicts,
                    "journals": len(report), "errors": len(errors),
                    "dry_run": dry_run}
        if conflicts or errors:
            parts = []
            if conflicts:
                parts.append(f"{conflicts} 个文件冲突未覆盖")
            if errors:
                parts.append(f"{len(errors)} 个日志无法处理")
            return ToolResult(
                ok=False, data=data,
                error="恢复未完成: " + "，".join(parts),
                error_class="execution_error",
                submitted=True, executed=True, contract_valid=True,
                goal_reached=False,
                independently_verified=not uncertain,
                side_effect_uncertain=uncertain,
                evidence=evidence)
        return ToolResult(
            ok=True, data=data,
            submitted=True, executed=True, contract_valid=True,
            goal_reached=True, independently_verified=True,
            side_effect_uncertain=False, evidence=evidence)

    contract = ToolContract(
        tool_id="coding_patch_recover",
        description="按事务日志恢复中断的多文件补丁，冲突不覆盖",
        tool_version=_PACK_VERSION,
        permission_requirements=["fs.read", "fs.write"],
        input_schema={"type": "object", "properties": {
            "dry_run": {"type": "boolean"},
        }},
        side_effect_level="local",
        idempotency_policy="state-check",
        timeout_policy=30.0,
        health_check=lambda: Path(root or ".").is_dir()
        and os.access(root or ".", os.W_OK),
        risk_class="high",
        offline_capable=True,
        requires_network=False,
        cost_model={"unit": "journals", "varies": "file_count"},
        contract_test_ref=_CONTRACT_TEST_REF,
    )
    return contract, run


def coding_syntax_check_tool(root: str = ".") -> Tuple[ToolContract, Callable]:
    def run(args: Dict) -> ToolResult:
        raw_targets = args.get("targets")
        if raw_targets is None:
            raw_targets = ["."]
        if not isinstance(raw_targets, list) or not raw_targets:
            return ToolResult.error_result("targets 必须是非空数组",
                                           "invalid_arguments")
        try:
            rootp = _resolve_root(root)
            paths = _check_targets(rootp, [str(t) for t in raw_targets])
        except ValueError as exc:
            return ToolResult.error_result(str(exc), "invalid_arguments")

        with tempfile.TemporaryDirectory(prefix="evoagent_pyc_") as cache:
            env = dict(os.environ)
            env["PYTHONPYCACHEPREFIX"] = cache
            started = time.monotonic()
            proc, failure = _run(
                [sys.executable, "-m", "compileall", "-q"]
                + [str(p) for p in paths],
                rootp, timeout=60.0, env=env)
            elapsed = round(time.monotonic() - started, 3)
        if failure:
            kind, exc = failure
            return ToolResult(
                ok=False, error=f"语法检查执行失败: {exc}",
                error_class="timeout" if kind == "timeout" else class_error(exc),
                submitted=True, executed=True, contract_valid=True,
                goal_reached=False, independently_verified=False,
                evidence={"returncode": None, "duration_s": elapsed})
        ok = proc.returncode == 0
        evidence = {
            "returncode": proc.returncode,
            "duration_s": elapsed,
            "targets": [p.relative_to(rootp).as_posix() for p in paths],
            "output_tail": _tail((proc.stdout or "") + (proc.stderr or "")),
        }
        return ToolResult(
            ok=ok, data={"returncode": proc.returncode},
            error=None if ok else "存在语法错误",
            error_class=None if ok else "contract_violation",
            submitted=True, executed=True, contract_valid=True,
            goal_reached=ok, independently_verified=ok, evidence=evidence)

    contract = ToolContract(
        tool_id="coding_syntax_check",
        description="Python 语法检查（compileall，不写项目缓存）",
        tool_version=_PACK_VERSION,
        permission_requirements=["fs.read", "process.exec"],
        input_schema={"type": "object", "properties": {
            "targets": {"type": "array"}}},
        side_effect_level="none",
        idempotency_policy="none",
        timeout_policy=60.0,
        health_check=lambda: _python_probe(),
        risk_class="low",
        offline_capable=True,
        requires_network=False,
        cost_model={"unit": "subprocess", "varies": "project_size"},
        contract_test_ref=_CONTRACT_TEST_REF,
    )
    return contract, run


def _python_probe() -> bool:
    try:
        proc = subprocess.run([sys.executable, "-c", "import ast"],
                              capture_output=True, timeout=5, check=False)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _pytest_probe() -> bool:
    try:
        proc = subprocess.run([sys.executable, "-c", "import pytest"],
                              capture_output=True, timeout=10, check=False)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _checker_probe(checker: List[str], root: Path) -> Tuple[bool, Optional[int]]:
    proc, failure = _run(checker + ["--version"], root, timeout=15.0)
    if failure:
        return False, None
    return proc.returncode == 0, proc.returncode


def coding_typecheck_tool(root: str = ".",
                          checker: Optional[List[str]] = None
                          ) -> Tuple[ToolContract, Callable]:
    command = list(checker) if checker else [sys.executable, "-m", "mypy"]

    def run(args: Dict) -> ToolResult:
        raw_targets = args.get("targets")
        if raw_targets is None:
            raw_targets = ["."]
        if not isinstance(raw_targets, list) or not raw_targets:
            return ToolResult.error_result("targets 必须是非空数组",
                                           "invalid_arguments")
        try:
            rootp = _resolve_root(root)
            targets = _check_targets(rootp, [str(t) for t in raw_targets])
        except ValueError as exc:
            return ToolResult.error_result(str(exc), "invalid_arguments")

        available, probe_rc = _checker_probe(command, rootp)
        if not available:
            return ToolResult(
                ok=False, data=None,
                error="类型检查器不可用（未安装或不可执行），不判成功",
                error_class="tool_defect_or_env",
                submitted=True, executed=False, contract_valid=True,
                goal_reached=None, independently_verified=False,
                evidence={"checker": command, "available": False,
                          "probe_returncode": probe_rc})

        with tempfile.TemporaryDirectory(prefix="evoagent_mypy_") as cache:
            env = dict(os.environ)
            env["PYTHONPYCACHEPREFIX"] = cache
            cmd = command + [str(t) for t in targets]
            if command == [sys.executable, "-m", "mypy"]:
                cmd += ["--cache-dir", cache, "--no-incremental"]
            started = time.monotonic()
            proc, failure = _run(cmd, rootp, timeout=120.0, env=env)
            elapsed = round(time.monotonic() - started, 3)
        if failure:
            kind, exc = failure
            return ToolResult(
                ok=False, error=f"类型检查执行失败: {exc}",
                error_class="timeout" if kind == "timeout" else class_error(exc),
                submitted=True, executed=True, contract_valid=True,
                goal_reached=False, independently_verified=False,
                evidence={"checker": command, "available": True,
                          "returncode": None, "duration_s": elapsed})
        ok = proc.returncode == 0
        return ToolResult(
            ok=ok, data={"returncode": proc.returncode},
            error=None if ok else _tail(proc.stdout or proc.stderr, 500)
            or "类型检查未通过",
            error_class=None if ok else "contract_violation",
            submitted=True, executed=True, contract_valid=True,
            goal_reached=ok, independently_verified=ok,
            evidence={"checker": command, "available": True,
                      "returncode": proc.returncode, "duration_s": elapsed,
                      "targets": [p.relative_to(rootp).as_posix()
                                  for p in targets],
                      "output_tail": _tail((proc.stdout or "")
                                           + (proc.stderr or ""))})

    contract = ToolContract(
        tool_id="coding_typecheck",
        description="Python 类型检查（mypy 适配；不可用不判成功）",
        tool_version=_PACK_VERSION,
        permission_requirements=["fs.read", "process.exec"],
        input_schema={"type": "object", "properties": {
            "targets": {"type": "array"}}},
        side_effect_level="none",
        idempotency_policy="none",
        timeout_policy=120.0,
        health_check=lambda: _checker_probe(command, Path(root or "."))[0],
        risk_class="medium",
        offline_capable=True,
        requires_network=False,
        cost_model={"unit": "subprocess", "varies": "project_size"},
        contract_test_ref=_CONTRACT_TEST_REF,
    )
    return contract, run


_COUNT_RE = {
    "passed": re.compile(r"(\d+) passed"),
    "failed": re.compile(r"(\d+) failed"),
    "errors": re.compile(r"(\d+) error"),
}


def _parse_pytest_summary(output: str) -> Dict[str, int]:
    found = {}
    for key, pattern in _COUNT_RE.items():
        match = pattern.search(output or "")
        found[key] = int(match.group(1)) if match else 0
    return found


def coding_test_tool(root: str = ".") -> Tuple[ToolContract, Callable]:
    def run(args: Dict) -> ToolResult:
        raw_targets = args.get("targets")
        if raw_targets is None:
            raw_targets = ["."]
        if not isinstance(raw_targets, list) or not raw_targets:
            return ToolResult.error_result("targets 必须是非空数组",
                                           "invalid_arguments")
        try:
            timeout = max(5.0, min(600.0, float(args.get("timeout") or 180)))
        except (TypeError, ValueError):
            return ToolResult.error_result("timeout 非法", "invalid_arguments")
        try:
            rootp = _resolve_root(root)
            targets = _check_targets(rootp, [str(t) for t in raw_targets])
        except ValueError as exc:
            return ToolResult.error_result(str(exc), "invalid_arguments")

        with tempfile.TemporaryDirectory(prefix="evoagent_pytest_") as cache:
            env = dict(os.environ)
            env["PYTHONPYCACHEPREFIX"] = cache
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            started = time.monotonic()
            proc, failure = _run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "--"] + [str(p) for p in targets],
                rootp, timeout=timeout, env=env)
            elapsed = round(time.monotonic() - started, 3)
        if failure:
            kind, exc = failure
            return ToolResult(
                ok=False, error=f"测试执行失败: {exc}",
                error_class="timeout" if kind == "timeout" else class_error(exc),
                submitted=True, executed=True, contract_valid=True,
                goal_reached=False, independently_verified=False,
                evidence={"returncode": None, "duration_s": elapsed,
                          "timeout_s": timeout})
        output = (proc.stdout or "") + (proc.stderr or "")
        counts = _parse_pytest_summary(output)
        collected = counts["passed"] + counts["failed"] + counts["errors"]
        ok = proc.returncode == 0 and counts["failed"] == 0 \
            and counts["errors"] == 0 and collected > 0
        if collected == 0:
            error = "未收集到测试，不判成功"
            error_class = "invalid_arguments"
        elif ok:
            error, error_class = None, None
        else:
            error, error_class = "测试未通过", "contract_violation"
        return ToolResult(
            ok=ok, data=counts, error=error, error_class=error_class,
            submitted=True, executed=True, contract_valid=True,
            goal_reached=ok, independently_verified=ok,
            evidence={"returncode": proc.returncode, "duration_s": elapsed,
                      "counts": counts,
                      "targets": [p.relative_to(rootp).as_posix()
                                  for p in targets],
                      "output_tail": _tail(output)})

    contract = ToolContract(
        tool_id="coding_test",
        description="真实 pytest 执行（通过/失败/未收集三态可区分）",
        tool_version=_PACK_VERSION,
        permission_requirements=["fs.read", "process.exec"],
        input_schema={"type": "object", "properties": {
            "targets": {"type": "array"},
            "timeout": {"type": "number"}}},
        # 测试会执行项目内任意代码：副作用未知，按外部副作用对待（fail-closed）
        side_effect_level="external",
        idempotency_policy="none",
        timeout_policy=180.0,
        health_check=lambda: _pytest_probe(),
        risk_class="high",
        offline_capable=True,
        requires_network=False,
        cost_model={"unit": "subprocess", "varies": "test_count"},
        contract_test_ref=_CONTRACT_TEST_REF,
    )
    return contract, run


CODING_TOOL_IDS = (
    "coding_symbols", "coding_references", "coding_edit", "coding_patch",
    "coding_patch_recover", "coding_syntax_check", "coding_typecheck",
    "coding_test",
)


@dataclass(frozen=True)
class CodingPackManifest:
    pack_id: str = _PACK_ID
    version: str = _PACK_VERSION
    tool_ids: Tuple[str, ...] = CODING_TOOL_IDS
    rollback_version: Optional[str] = _ROLLBACK_VERSION
    contract_test_ref: str = _CONTRACT_TEST_REF

    def as_dict(self) -> Dict[str, object]:
        return {
            "pack_id": self.pack_id,
            "version": self.version,
            "tool_ids": list(self.tool_ids),
            "rollback_version": self.rollback_version,
            "contract_test_ref": self.contract_test_ref,
        }


CODING_PACK = CodingPackManifest()


def build_coding_pack(root: str = "."
                      ) -> Dict[str, Tuple[ToolContract, Callable]]:
    return {
        "coding_symbols": coding_symbols_tool(root),
        "coding_references": coding_references_tool(root),
        "coding_edit": coding_edit_tool(root),
        "coding_patch": coding_patch_tool(root),
        "coding_patch_recover": coding_patch_recover_tool(root),
        "coding_syntax_check": coding_syntax_check_tool(root),
        "coding_typecheck": coding_typecheck_tool(root),
        "coding_test": coding_test_tool(root),
    }


def install_coding_pack(registry, root: str = ".") -> CodingPackManifest:
    """把能力包注册进给定注册表；注册表仍是风险推导和执行生命周期的唯一权威。"""
    for contract, runner in build_coding_pack(root).values():
        registry.register(contract, runner)
    return CODING_PACK
