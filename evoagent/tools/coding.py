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
_PACK_VERSION = "1.5.1"
_ROLLBACK_VERSION = "1.5.0"
_CONTRACT_TEST_REF = "tests/test_coding_tools.py"
_JOURNAL_DIR = ".evoagent_patch_journal"
_LOCK_NAME = ".evoagent_patch.lock"
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


def _edit_path(rootp: Path, rel: str) -> Path:
    path = _resolve_within(rootp, rel)
    if any(part in {_JOURNAL_DIR, _LOCK_NAME} for part in Path(rel).parts) or (
            path.relative_to(rootp).parts and path.relative_to(rootp).parts[0] in {_JOURNAL_DIR, _LOCK_NAME}):
        raise ValueError("补丁事务元数据不能作为编辑目标")
    return path


def _serialized(root, runner):
    """One cooperative edit/recovery writer per project, including processes."""
    def call(args):
        fd = None
        try:
            rootp = _resolve_root(root)
            lock = rootp / _LOCK_NAME
            if lock.resolve() != lock or lock.is_symlink():
                raise PermissionError("补丁锁路径存在重定向")
            fd = os.open(str(lock), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError) as exc:
            if fd is not None:
                os.close(fd)
            return ToolResult.error_result(f"项目补丁锁或边界拒绝: {exc}", "auth_or_permission")
        try:
            return runner(args)
        finally:
            os.close(fd)
    return call


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
            path = _edit_path(rootp, rel)
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
            if path.resolve() != path or _sha_file(path) != digest_before:
                return ToolResult.error_result("文件在预检后发生变更，拒绝覆盖", "invalid_arguments")
            path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            try:
                unchanged = path.is_file() and _sha_file(path) == digest_before
            except (OSError, ValueError):
                unchanged = False
            return ToolResult(ok=False, error=str(exc), error_class=class_error(exc),
                submitted=True, executed=True, side_effect_uncertain=not unchanged,
                evidence={"written": not unchanged})
        try:
            verified = path.read_text(encoding="utf-8") == new_content
        except (OSError, UnicodeError):
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
            restored = not _rollback_files([dict(path=path, rel=rel, original=original,
                sha256_before=digest_before, sha256_after=evidence["sha256_after"])])
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
    return contract, _serialized(root, run)


def _action_of(item: Dict[str, object]) -> str:
    if item.get("create"):
        return "created"
    if item.get("deleted"):
        return "deleted"
    return "modified"


def _rollback_files(prepared: List[Dict[str, object]]) -> List[str]:
    failed = []
    for item in prepared:
        path = item["path"]
        original = item["original"]
        try:
            if path.resolve() != path:
                failed.append(str(item["rel"]))
                continue
            current = _sha_file(path) if path.is_file() else ""
            if (not path.exists() and item.get("create")) or (path.is_file() and current == item["sha256_before"]):
                continue
            if current != item["sha256_after"] or (path.exists() and not path.is_file()):
                failed.append(str(item["rel"]))
                continue
            if item.get("create"):
                path.unlink(missing_ok=True)
                if path.exists():
                    failed.append(str(item["rel"]))
                continue
            path.write_text(original, encoding="utf-8")
            if path.read_text(encoding="utf-8") != original:
                failed.append(str(item["rel"]))
        except (OSError, ValueError):
            failed.append(str(item["rel"]))
    return failed


def _journal_path(rootp: Path) -> Path:
    jdir = rootp / _JOURNAL_DIR
    if jdir.resolve() != jdir or jdir.is_symlink():
        raise OSError("事务日志目录不得重定向")
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
                   "original": item["original"],
                   "existed": not item.get("create"),
                   "deleted": bool(item.get("deleted"))}
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
    if jdir.resolve() != jdir or jdir.is_symlink():
        return report, 0, 0, [{"journal": _JOURNAL_DIR, "error": "事务日志目录不得重定向"}], False
    if not jdir.is_dir():
        return report, restored, conflicts, errors, uncertain
    for path in sorted(jdir.glob("*.json")):
        entry: Dict[str, object] = {"journal": path.name, "files": []}
        try:
            if path.resolve().parent != jdir or path.is_symlink():
                raise ValueError("事务日志文件不得重定向")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("非法日志格式或版本")
            files = payload.get("files")
            if not isinstance(files, list):
                raise ValueError("缺少 files")
            status = payload.get("status")
            if status not in {"pending", "committed"}:
                raise ValueError("非法事务状态")
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
            existed = item.get("existed", True) is not False
            deleted = bool(item.get("deleted"))
            action = "conflict"
            attempted_write = False
            try:
                if not rel or not isinstance(original, str):
                    raise ValueError("日志条目不完整")
                if not existed:
                    if before or not after:
                        raise ValueError("新增文件日志条目不完整")
                elif not before or (not deleted and not after):
                    raise ValueError("日志条目不完整")
                if existed and hashlib.sha256(
                        original.encode("utf-8")).hexdigest() != before:
                    raise ValueError("原内容摘要与日志不符")
                target = _edit_path(rootp, rel)
                current = _sha_file(target) if target.is_file() else ""
                if not existed:
                    if target.is_file() and current == after:
                        if not dry_run:
                            attempted_write = True
                            target.unlink()
                        action = "restored"
                        restored += 1
                    elif not target.exists():
                        action = "already_before"
                    else:
                        action = "conflict"
                        conflicts += 1
                elif deleted:
                    if not target.exists():
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
                elif current == after:
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
                errors.append({"journal": path.name, "file": rel, "error": str(exc)})
            if action in ("conflict", "error"):
                unresolved += 1
            file_rows.append({"file": rel, "sha256_before": before,
                              "sha256_after": after, "action": action})
        entry["files"] = file_rows
        if unresolved == 0 and not dry_run:
            _remove_journal(path)
        report.append(entry)
    return report, restored, conflicts, errors, uncertain


_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_HUNK_LIMIT = 50


def _parse_unified_diff(text: str):
    lines = text.replace("\r\n", "\n").replace("\r", "\n") \
        .splitlines(keepends=True)
    files: List[Dict[str, object]] = []
    index = 0
    total = len(lines)
    while index < total:
        head = lines[index]
        if not head.strip():
            index += 1
            continue
        if not head.startswith("--- "):
            return None, f"第 {index + 1} 行不是文件头: {head.rstrip()}"
        if index + 1 >= total or not lines[index + 1].startswith("+++ "):
            return None, f"第 {index + 1} 行缺少 +++ 文件头"
        old_name = head[4:].split("\t")[0].strip()
        new_name = lines[index + 1][4:].split("\t")[0].strip()
        create = old_name == "/dev/null"
        delete = new_name == "/dev/null"
        if create and delete:
            return None, "文件头两侧同为 /dev/null，无法确定路径"
        if old_name.startswith("a/"):
            old_name = old_name[2:]
        if new_name.startswith("b/"):
            new_name = new_name[2:]
        if create:
            rel = new_name
        elif delete:
            rel = old_name
        else:
            if old_name and new_name and old_name != new_name:
                return None, f"文件头不一致: {old_name} -> {new_name}"
            rel = new_name or old_name
        if not rel:
            return None, f"第 {index + 1} 行文件头缺少路径"
        index += 2
        hunks: List[Dict[str, object]] = []
        while index < total and lines[index].startswith("@@"):
            match = _HUNK_HEADER.match(lines[index].rstrip("\r\n"))
            if not match:
                return None, f"第 {index + 1} 行 hunk 头非法: " \
                             f"{lines[index].rstrip()}"
            old_count = int(match.group(2)) if match.group(2) is not None \
                else 1
            new_count = int(match.group(4)) if match.group(4) is not None \
                else 1
            if create and old_count != 0:
                return None, f"{rel}: 新增文件的 hunk 旧计数必须为 0"
            if delete and new_count != 0:
                return None, f"{rel}: 删除文件的 hunk 新计数必须为 0"
            start_line = int(match.group(1) or 1) if delete \
                else int(match.group(3) or 1)
            old_left = old_count
            new_left = new_count
            index += 1
            old_block: List[str] = []
            new_block: List[str] = []
            # 按 hunk 计数消费，避免把以 "--- " 开头的删除行误判为文件头
            while index < total and (old_left > 0 or new_left > 0):
                body = lines[index]
                if body.startswith("\\"):
                    return None, "不支持 \\ No newline at end of file 标记"
                if body.startswith("+"):
                    if new_left <= 0:
                        return None, f"第 {index + 1} 行新增行超出 hunk 计数"
                    new_block.append(body[1:])
                    new_left -= 1
                elif body.startswith("-"):
                    if old_left <= 0:
                        return None, f"第 {index + 1} 行删除行超出 hunk 计数"
                    old_block.append(body[1:])
                    old_left -= 1
                elif body.startswith(" "):
                    if old_left <= 0 or new_left <= 0:
                        return None, f"第 {index + 1} 行上下文超出 hunk 计数"
                    old_block.append(body[1:])
                    new_block.append(body[1:])
                    old_left -= 1
                    new_left -= 1
                elif not body.strip():
                    if old_left <= 0 or new_left <= 0:
                        return None, f"第 {index + 1} 行空行超出 hunk 计数"
                    old_block.append(body)
                    new_block.append(body)
                    old_left -= 1
                    new_left -= 1
                else:
                    return None, f"第 {index + 1} 行不是合法 diff 行: " \
                                 f"{body.rstrip()}"
                index += 1
            if old_left > 0 or new_left > 0:
                return None, f"{rel}: hunk 行数不足"
            if not old_block and not new_block:
                return None, f"{rel}: 第 {start_line} 行 hunk 为空"
            hunks.append({"start": start_line,
                          "old": "".join(old_block),
                          "new": "".join(new_block)})
        if not hunks:
            return None, f"{rel}: 缺少 hunk"
        if (create or delete) and len(hunks) != 1:
            return None, f"{rel}: 新增/删除文件只允许一个 hunk"
        files.append({"path": rel, "hunks": hunks,
                      "create": create, "delete": delete})
    if not files:
        return None, "diff 为空"
    return files, None


_HUNK_FUZZ = 2


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _trim_hunk_context(old_block: str, new_block: str, fuzz: int):
    old_lines = old_block.splitlines(True)
    new_lines = new_block.splitlines(True)
    lead = 0
    while (lead < fuzz and lead < len(old_lines) - 1
           and lead < len(new_lines) - 1
           and old_lines[lead] == new_lines[lead]):
        lead += 1
    trail = 0
    while (trail < fuzz - lead and trail < len(old_lines) - lead - 1
           and trail < len(new_lines) - lead - 1
           and old_lines[-1 - trail] == new_lines[-1 - trail]):
        trail += 1
    return ("".join(old_lines[lead:len(old_lines) - trail]),
            "".join(new_lines[lead:len(new_lines) - trail]), lead)


def _apply_hunk(content: str, old_block: str, new_block: str,
                expected_line: int):
    """定位并替换单个 hunk，返回 (新内容, 行数偏移, 命中行号)。"""
    for fuzz in range(_HUNK_FUZZ + 1):
        old_trim, new_trim, lead = _trim_hunk_context(
            old_block, new_block, fuzz)
        if not old_trim:
            break
        hits = []
        start = 0
        while True:
            found = content.find(old_trim, start)
            if found < 0:
                break
            end = found + len(old_trim)
            if (found == 0 or content[found - 1] == "\n") and (
                    old_trim.endswith("\n") or end == len(content) or content[end] == "\n"):
                hits.append(found)
            start = found + 1
        if not hits:
            continue
        want = expected_line + lead
        exact = [hit for hit in hits if _line_at(content, hit) == want]
        if len(hits) == 1:
            pick = hits[0]
        elif len(exact) == 1:
            pick = exact[0]
        else:
            raise ValueError(
                f"命中 {len(hits)} 处，行号 {want} 无法唯一消歧")
        line_no = _line_at(content, pick)
        updated = content[:pick] + new_trim + content[pick + len(old_trim):]
        delta = new_trim.count("\n") - old_trim.count("\n")
        return updated, delta, line_no
    raise ValueError(f"上下文未命中（容错 {_HUNK_FUZZ} 行后仍无匹配）")


def coding_patch_tool(root: str = ".") -> Tuple[ToolContract, Callable]:
    def run(args: Dict) -> ToolResult:
        edits = args.get("edits")
        diff_text = args.get("diff")
        dry_run = bool(args.get("dry_run"))
        if (edits is None) == (diff_text is None):
            return ToolResult.error_result(
                "edits 与 diff 必须二选一", "invalid_arguments")
        if edits is not None:
            if not isinstance(edits, list) or not edits:
                return ToolResult.error_result("edits 必须是非空数组",
                                               "invalid_arguments")
            if len(edits) > 50:
                return ToolResult.error_result("单次最多 50 条编辑",
                                               "invalid_arguments")
        else:
            if not isinstance(diff_text, str) or not diff_text.strip():
                return ToolResult.error_result("diff 必须是非空字符串",
                                               "invalid_arguments")
            if len(diff_text) > _MAX_SOURCE_BYTES:
                return ToolResult.error_result("diff 过大",
                                               "invalid_arguments")
        try:
            rootp = _resolve_root(root)
        except ValueError as exc:
            return ToolResult.error_result(str(exc), "auth_or_permission")

        diff_ops: Dict[str, str] = {}
        if diff_text is not None:
            parsed, parse_error = _parse_unified_diff(diff_text)
            if parse_error:
                return ToolResult.error_result(parse_error,
                                               "invalid_arguments")
            if len(parsed) > _HUNK_LIMIT:
                return ToolResult.error_result("单次最多 50 个文件",
                                               "invalid_arguments")
            converted = []
            for entry in parsed:
                rel = str(entry["path"])
                create = bool(entry.get("create"))
                delete = bool(entry.get("delete"))
                try:
                    path = _edit_path(rootp, rel)
                except ValueError as exc:
                    return ToolResult.error_result(str(exc),
                                                   "auth_or_permission")
                if create:
                    if path.exists():
                        return ToolResult.error_result(
                            f"{rel}: 目标已存在，无法新增文件",
                            "invalid_arguments")
                    if not path.parent.is_dir():
                        return ToolResult.error_result(
                            f"{rel}: 父目录不存在，无法新增文件",
                            "invalid_arguments")
                    new_block = str(entry["hunks"][0]["new"])
                    if not new_block:
                        return ToolResult.error_result(
                            f"{rel}: 新增文件内容为空", "invalid_arguments")
                    if len(new_block.encode("utf-8")) > _MAX_SOURCE_BYTES:
                        return ToolResult.error_result(
                            f"文件过大: {rel}", "invalid_arguments")
                    converted.append({"path": rel, "old_text": "",
                                      "new_text": new_block, "_line": 1})
                    diff_ops[rel] = "create"
                    continue
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
                if not original:
                    return ToolResult.error_result(
                        f"{rel}: 空文件暂不支持 diff，请使用 edits",
                        "invalid_arguments")
                content = original
                line_hint = None
                for hidx, hunk in enumerate(entry["hunks"], 1):
                    old_block = str(hunk["old"])
                    new_block = str(hunk["new"])
                    if not old_block:
                        return ToolResult.error_result(
                            f"{rel}: 第 {hidx} 个 hunk 缺少上下文/删除行",
                            "invalid_arguments")
                    try:
                        # 同一文件的前序 hunk 已应用，内容按新文件行号定位
                        content, _shift, hit_line = _apply_hunk(
                            content, old_block, new_block,
                            int(hunk["start"]))
                    except ValueError as exc:
                        return ToolResult.error_result(
                            f"{rel}: 第 {hidx} 个 hunk {exc}，未写盘",
                            "invalid_arguments")
                    if line_hint is None:
                        line_hint = hit_line
                if delete and content:
                    return ToolResult.error_result(
                        f"{rel}: 删除文件的 hunk 未覆盖全部内容",
                        "invalid_arguments")
                converted.append({"path": rel, "old_text": original,
                                  "new_text": content, "_line": line_hint})
                if delete:
                    diff_ops[rel] = "delete"
            edits = converted

        prepared: List[Dict[str, object]] = []
        seen = set()
        for index, item in enumerate(edits):
            if not isinstance(item, dict):
                return ToolResult.error_result(
                    f"第 {index + 1} 条编辑不是对象", "invalid_arguments")
            rel = str(item.get("path") or "")
            old_text = str(item.get("old_text") or "")
            is_create = diff_ops.get(rel) == "create"
            is_delete = diff_ops.get(rel) == "delete"
            if not rel or item.get("new_text") is None \
                    or (not old_text and not is_create):
                return ToolResult.error_result(
                    f"第 {index + 1} 条编辑缺少 path/old_text/new_text",
                    "invalid_arguments")
            new_text = str(item.get("new_text"))
            try:
                path = _edit_path(rootp, rel)
            except ValueError as exc:
                return ToolResult.error_result(str(exc), "auth_or_permission")
            if str(path) in seen:
                return ToolResult.error_result(
                    f"同一文件出现多条编辑: {rel}", "invalid_arguments")
            seen.add(str(path))
            if is_create:
                if path.exists():
                    return ToolResult.error_result(
                        f"{rel}: 目标已存在，无法新增文件",
                        "invalid_arguments")
                original = ""
                digest_before = ""
            else:
                if not path.is_file():
                    return ToolResult.error_result(
                        f"文件不存在: {rel}", "invalid_arguments")
                if path.stat().st_size > _MAX_SOURCE_BYTES:
                    return ToolResult.error_result(
                        f"文件过大: {rel}", "invalid_arguments")
                try:
                    original = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    return ToolResult.error_result(
                        str(exc), class_error(exc))
                digest_before = hashlib.sha256(
                    original.encode("utf-8")).hexdigest()
            expected = item.get("expected_sha256")
            if expected is not None and not is_create:
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

            if is_create:
                hits = 1
            else:
                hits = original.count(old_text)
            if hits != 1:
                return ToolResult.error_result(
                    f"{rel}: old_text 命中 {hits} 处，要求唯一，未写盘",
                    "invalid_arguments")
            if new_text == old_text:
                return ToolResult.error_result(
                    f"{rel}: 内容无变化，未写盘", "invalid_arguments")
            content = new_text if is_create \
                else original.replace(old_text, new_text, 1)
            if path.suffix == ".py" and not is_delete:
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
            line_hint = item.get("_line")
            if not isinstance(line_hint, int) or line_hint < 1:
                line_hint = 1 if is_create else original.count(
                    "\n", 0, original.index(old_text)) + 1
            prepared.append({
                "path": path,
                "rel": rel,
                "original": original,
                "new_content": content,
                "sha256_before": digest_before,
                "sha256_after": "" if is_delete else hashlib.sha256(
                    content.encode("utf-8")).hexdigest(),
                "line": line_hint,
                "create": is_create,
                "deleted": is_delete,
            })

        if dry_run:
            files = []
            for item in prepared:
                diff = "".join(difflib.unified_diff(
                    str(item["original"]).splitlines(True),
                    str(item["new_content"]).splitlines(True),
                    fromfile="/dev/null" if item["create"]
                    else str(item["rel"]),
                    tofile="/dev/null" if item["deleted"]
                    else str(item["rel"])))
                files.append({
                    "file": item["rel"],
                    "line": item["line"],
                    "action": _action_of(item),
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
                path = item["path"]
                if path.resolve() != path or (path.exists() if item["create"] else
                        not path.is_file() or _sha_file(path) != item["sha256_before"]):
                    raise OSError(f"预检后文件状态变更，拒绝覆盖: {item['rel']}")
            for item in prepared:
                path = item["path"]
                if path.resolve() != path or (path.exists() if item["create"] else
                        not path.is_file() or _sha_file(path) != item["sha256_before"]):
                    raise OSError(f"写入前文件状态变更，拒绝覆盖: {item['rel']}")
                if item["deleted"]:
                    item["path"].unlink()
                elif item["create"]:
                    with path.open("x", encoding="utf-8") as handle:
                        handle.write(item["new_content"])
                else:
                    item["path"].write_text(item["new_content"],
                                            encoding="utf-8")
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
                if item["deleted"]:
                    if item["path"].exists():
                        mismatched.append(str(item["rel"]))
                elif item["path"].read_text(encoding="utf-8") \
                        != item["new_content"]:
                    mismatched.append(str(item["rel"]))
            except (OSError, UnicodeError):
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
        except OSError as exc:
            return ToolResult(ok=False, data={"applied": written},
                error=f"补丁已写入但提交标记失败，须恢复或对账: {exc}", error_class="execution_error",
                submitted=True, executed=True, contract_valid=True, goal_reached=False,
                independently_verified=False, side_effect_uncertain=True,
                evidence={"written": written, "journal": journal.name, "commit_recorded": False})

        files = [{"file": item["rel"], "line": item["line"],
                  "action": _action_of(item),
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
        description="原子多文件补丁：edits 或统一 diff（含新增/删除文件、行号消歧与上下文容错），全量预检、失败全量回滚、dry-run 预览",
        tool_version=_PACK_VERSION,
        permission_requirements=["fs.read", "fs.write"],
        input_schema={"type": "object", "properties": {
            "edits": {"type": "array"},
            "diff": {"type": "string"},
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
    return contract, _serialized(root, run)


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
    return contract, _serialized(root, run)


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
        side_effect_level="external",
        idempotency_policy="none",
        timeout_policy=120.0,
        health_check=lambda: _checker_probe(command, Path(root or "."))[0],
        risk_class="high",
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
