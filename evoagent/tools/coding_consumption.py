"""编码能力包的真实消费运行时（ALN-18-G）。

18-A~F 交付的编码工具集此前只有单元级验证：`install_coding_pack`
没有任何生产消费方。本模块把「任务 → 编程专家 → 编码工具跨文件修改
→ 验收 → 产物归档 → 失败回滚」串成可复核的消费链，并落实三条验收：

1. **包生命周期与精确版本记录**：`install_for_task` 把包安装进注册表
   （注册表仍是唯一权威），返回绑定记录（pack_id/version/tool_ids/
   root）供任务运行清单冻结（对齐 06-R 精确消费语义）；
2. **产物归档（消费 24-A）**：`archive_modified` 把本轮实际修改过的
   文件经 24-A ArtifactStore 归档为任务产物证据；
3. **回滚闭环**：`snapshot_project`/`restore_snapshot` 以文件级快照
   提供验收失败后的恢复（coding_patch_recover 之外的整项目层兜底）。

不宣称 LSP：`coding_symbols`/`coding_references` 是 AST 级分析
（18-A 交付口径），本模块不新增语言服务能力。
未接入 controller 自动装配（app.py 不在本卡写入边界）——生产挂载
留缺口，同 06-R/07-B 模式。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .coding import CODING_PACK, CodingPackManifest, install_coding_pack


class ConsumptionError(ValueError):
    """消费链配置或状态错误。"""


DEFAULT_SKIP_DIRS = (".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv")
DEFAULT_MAX_BYTES = 64 * 1024 * 1024


def _positive_limit(value, name):
    if type(value) is not int or value <= 0:
        raise ConsumptionError(name + "必须为正整数")
    return value


def _base(root):
    if not isinstance(root, (str, Path)) or not str(root).strip():
        raise ConsumptionError("项目根不能为空")
    base = Path(root).resolve()
    if not base.is_dir():
        raise ConsumptionError(f"项目根不存在: {root}")
    return base


def _path(base, rel):
    if (not isinstance(rel, str) or not rel or "\\" in rel or ":" in rel
            or any(part in ("", ".", "..") for part in rel.split("/"))):
        raise ConsumptionError("非法快照相对路径")
    path = base / rel
    try:
        path.resolve().relative_to(base)
    except ValueError:
        raise ConsumptionError("快照路径越出项目根") from None
    current = base
    for part in rel.split("/"):
        current = current / part
        if current.is_symlink() or current.resolve() != current:
            raise ConsumptionError("快照不跟随符号链接或目录联接")
    if path.exists() and not path.is_file():
        raise ConsumptionError("快照文件路径已变成目录")
    return path


def _files(base, skip_dirs, max_files):
    count = 0
    for directory, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        for name in dirs:
            candidate = Path(directory) / name
            if candidate.is_symlink() or candidate.resolve() != candidate:
                raise ConsumptionError("快照不跟随符号链接或目录联接")
        for name in sorted(files):
            rel = (Path(directory) / name).relative_to(base).as_posix()
            path = _path(base, rel)
            count += 1
            if count > max_files:
                raise ConsumptionError(f"项目文件数超过快照上限 {max_files}")
            yield rel, path


def _read(path, max_bytes):
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ConsumptionError("项目内容超过字节上限")
    return data


@dataclass
class ProjectSnapshot:
    """Bounded project snapshot; exclusions apply to later comparison too."""
    root: str
    taken_at: float
    files: Dict[str, Optional[bytes]]
    skip_dirs: Tuple[str, ...] = DEFAULT_SKIP_DIRS
    max_files: int = 2000
    max_bytes: int = DEFAULT_MAX_BYTES

    def _paths(self, root=None):
        if not Path(self.root).is_absolute():
            raise ConsumptionError("快照必须冻结绝对项目根")
        base = _base(self.root)
        if base != Path(self.root):
            raise ConsumptionError("快照项目根已变成链接或联接")
        if root is not None and _base(root) != base:
            raise ConsumptionError("快照与归档项目根不一致")
        _positive_limit(self.max_files, "文件上限")
        _positive_limit(self.max_bytes, "字节上限")
        if len(self.files) > self.max_files or any(v is not None and not isinstance(v, bytes) for v in self.files.values()):
            raise ConsumptionError("非法或超限快照")
        if sum(len(v) for v in self.files.values() if v is not None) > self.max_bytes:
            raise ConsumptionError("快照内容超过字节上限")
        return base, {rel: _path(base, rel) for rel in self.files}

    def changed_paths(self, root: str) -> List[str]:
        base, paths = self._paths(root)
        changed = []
        for rel, path in paths.items():
            old = self.files[rel]
            if not path.is_file():
                different = old is not None
            else:
                different = old is None or path.stat().st_size != len(old) or _read(path, self.max_bytes) != old
            if different:
                changed.append(rel)
        for rel, path in _files(base, self.skip_dirs, self.max_files):
            if rel not in self.files:
                changed.append(rel)
        return sorted(changed)


def snapshot_project(root: str, *, skip_dirs: Tuple[str, ...] = DEFAULT_SKIP_DIRS,
                     max_files: int = 2000,
                     max_bytes: int = DEFAULT_MAX_BYTES) -> ProjectSnapshot:
    """Freeze an absolute root, prune excluded directories and bound memory."""
    base = _base(root)
    _positive_limit(max_files, "文件上限")
    _positive_limit(max_bytes, "字节上限")
    files = {}
    used = 0
    for rel, path in _files(base, skip_dirs, max_files):
        content = _read(path, max_bytes - used)
        files[rel] = content
        used += len(content)
    return ProjectSnapshot(str(base), time.time(), files, tuple(skip_dirs), max_files, max_bytes)


def restore_snapshot(snapshot: ProjectSnapshot) -> List[str]:
    """Preflight every path before restoring; unknown new files stay untouched."""
    base, paths = snapshot._paths()
    touched = []
    for rel, path in paths.items():
        old = snapshot.files[rel]
        if old is None:
            if path.is_file():
                path.unlink()
                touched.append(rel)
        elif not path.exists() or path.stat().st_size != len(old) or _read(path, snapshot.max_bytes) != old:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(old)
            touched.append(rel)
    return sorted(touched)


class CodingPackRuntime:
    """编码能力包的任务级消费运行时。"""

    def __init__(self, registry: Any, artifact_store: Optional[Any] = None):
        self.registry = registry
        self.artifact_store = artifact_store

    def install_for_task(self, task_id: str, root: str) -> Dict[str, Any]:
        """把编码包安装进注册表并产出绑定记录（精确版本消费语义）。

        同一注册表与项目重复安装幂等；root变化必须使用独立注册表，
        防止同名工具闭包把已绑定任务静默重定向到其他项目。
        """
        base = _base(root)
        if not isinstance(task_id, str) or not task_id.strip():
            raise ConsumptionError("消费任务身份不能为空")
        lock = self.registry.__dict__.setdefault("_coding_consumption_lock", threading.RLock())
        with lock:
            context = getattr(self.registry, "_coding_consumption_context", None)
            if context is not None:
                old_root, contracts = context
                if old_root != str(base):
                    raise ConsumptionError("注册表已绑定其他项目，请使用独立注册表")
                if any(self.registry.contract(tid) is not contract or contract.tool_version != CODING_PACK.version
                       for tid, contract in contracts.items()):
                    raise ConsumptionError("注册表编码能力已变化，拒绝沿用旧消费绑定")
            else:
                if any(self.registry.contract(tid) is not None for tid in CODING_PACK.tool_ids):
                    raise ConsumptionError("已有编码工具缺少项目绑定，拒绝覆盖")
                install_coding_pack(self.registry, str(base))
                self.registry._coding_consumption_context = (str(base), {
                    tid: self.registry.contract(tid) for tid in CODING_PACK.tool_ids})
        manifest: CodingPackManifest = CODING_PACK
        return {
            "pack_id": manifest.pack_id,
            "pack_version": manifest.version,
            "tool_ids": list(manifest.tool_ids),
            "rollback_version": manifest.rollback_version,
            "root": str(base),
            "task_id": task_id,
            "installed_at": time.time(),
        }

    def archive_modified(self, task_id: str, root: str,
                         snapshot: ProjectSnapshot, *,
                         max_files: int = 50) -> Dict[str, Any]:
        """把快照之后实际修改/新增的文件经 24-A ArtifactStore 归档。

        返回 {archived: [evidence...], skipped: [原因]}；无 ArtifactStore
        时明确 skipped，不假装归档。
        """
        _positive_limit(max_files, "归档文件上限")
        snapshot._paths(root)
        if self.artifact_store is None:
            return {"archived": [], "skipped": ["no_artifact_store"]}
        changed = snapshot.changed_paths(root)
        archived: List[Dict[str, Any]] = []
        skipped: List[str] = []
        base = _base(root)
        for rel in changed[:max_files]:
            path = _path(base, rel)
            if not path.is_file():
                skipped.append(f"{rel}:deleted")
                continue
            content = _read(path, snapshot.max_bytes)
            if not content:
                skipped.append(f"{rel}:empty")
                continue
            kind = "json" if rel.endswith(".json") else "text"
            try:
                evidence = self.artifact_store.save(
                    task_id, "file_" + hashlib.sha256(rel.encode("utf-8")).hexdigest(),
                    kind, content,
                    description=f"coding consumption: {rel}")
            except Exception as exc:  # 单文件失败不阻断其余归档
                skipped.append(f"{rel}:{type(exc).__name__}")
                continue
            evidence = dict(evidence)
            evidence["source_path"] = rel
            archived.append(evidence)
        if len(changed) > max_files:
            skipped.append(f"truncated:{len(changed) - max_files}")
        return {"archived": archived, "skipped": skipped}

    @staticmethod
    def pack_identity() -> Dict[str, Any]:
        return CODING_PACK.as_dict()
