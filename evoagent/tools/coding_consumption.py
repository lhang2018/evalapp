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

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .coding import CODING_PACK, CodingPackManifest, install_coding_pack


class ConsumptionError(ValueError):
    """消费链配置或状态错误。"""


@dataclass
class ProjectSnapshot:
    """项目文件级快照（相对路径 → 内容字节）。二进制安全。"""

    root: str
    taken_at: float
    files: Dict[str, Optional[bytes]]   # None = 快照时不存在（新建文件回滚=删除）

    def changed_paths(self, root: str) -> List[str]:
        """与当前磁盘状态对比，返回发生变化的相对路径（修改/新增/删除）。"""
        base = Path(root)
        changed: List[str] = []
        for rel, old in self.files.items():
            path = base / rel
            now = path.read_bytes() if path.is_file() else None
            if now != old:
                changed.append(rel)
        for path in base.rglob("*"):
            if path.is_file():
                rel = str(path.relative_to(base)).replace("\\", "/")
                if rel not in self.files:
                    changed.append(rel)
        return sorted(changed)


def snapshot_project(root: str, *,
                     skip_dirs: Tuple[str, ...] = (
                         ".git", "__pycache__", ".pytest_cache",
                         "node_modules", ".venv", "venv"),
                     max_files: int = 2000) -> ProjectSnapshot:
    """对真实项目根做文件级快照（回滚兜底的依据）。"""
    base = Path(root)
    if not base.is_dir():
        raise ConsumptionError(f"项目根不存在: {root}")
    files: Dict[str, Optional[bytes]] = {}
    count = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(base)).replace("\\", "/")
        if any(part in skip_dirs for part in path.parts):
            continue
        if count >= max_files:
            raise ConsumptionError(f"项目文件数超过快照上限 {max_files}")
        files[rel] = path.read_bytes()
        count += 1
    return ProjectSnapshot(root=str(base), taken_at=time.time(), files=files)


def restore_snapshot(snapshot: ProjectSnapshot) -> List[str]:
    """恢复到快照点；返回被恢复/删除/清理的相对路径。快照本身不动。"""
    base = Path(snapshot.root)
    touched: List[str] = []
    for rel, old in snapshot.files.items():
        path = base / rel
        if old is None:
            if path.exists():
                path.unlink()
                touched.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.read_bytes() != old:
            path.write_bytes(old)
            touched.append(rel)
    # 快照后新建的文件（不在快照里）不属于恢复目标——由调用方用
    # changed_paths 明确列出并决定处置；本函数不越权删除未知文件。
    return sorted(set(touched))


class CodingPackRuntime:
    """编码能力包的任务级消费运行时。"""

    def __init__(self, registry: Any, artifact_store: Optional[Any] = None):
        self.registry = registry
        self.artifact_store = artifact_store

    def install_for_task(self, task_id: str, root: str) -> Dict[str, Any]:
        """把编码包安装进注册表并产出绑定记录（精确版本消费语义）。

        同一注册表重复安装幂等（覆盖同 id 契约）；root 变化属于
        不同消费上下文，必须产出新的绑定记录。
        """
        if not root or not Path(root).is_dir():
            raise ConsumptionError(f"消费项目根不存在: {root}")
        manifest: CodingPackManifest = install_coding_pack(self.registry, root)
        return {
            "pack_id": manifest.pack_id,
            "pack_version": manifest.version,
            "tool_ids": list(manifest.tool_ids),
            "rollback_version": manifest.rollback_version,
            "root": str(Path(root).resolve()),
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
        if self.artifact_store is None:
            return {"archived": [], "skipped": ["no_artifact_store"]}
        changed = snapshot.changed_paths(root)
        archived: List[Dict[str, Any]] = []
        skipped: List[str] = []
        base = Path(root)
        for rel in changed[:max_files]:
            path = base / rel
            if not path.is_file():
                skipped.append(f"{rel}:deleted")
                continue
            content = path.read_bytes()
            if not content:
                skipped.append(f"{rel}:empty")
                continue
            kind = "json" if rel.endswith(".json") else "text"
            try:
                evidence = self.artifact_store.save(
                    task_id, rel.replace("/", "_").replace(".", "_")[-40:],
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
