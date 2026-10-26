"""编程专家按项目安装与真实工具消费（ALN-18-H）。

按受信 ``project_root`` 为编程专家装配**既有精确版本编码包**
（``tools/coding.py::install_coding_pack``），每个项目根使用**独立注册表**，
避免全局注册表跨项目覆盖；随后通过该注册表执行真实语法/项目测试工具并
记录命令返回码与磁盘变化：

- 跨根执行、链接替换、包版本不符、未知工具 → 明确拒绝；
- 同根重复安装幂等；不同根注册表互不影响；
- 安装失败实际回滚（丢弃该根注册表并恢复先前版本）后重新执行检查；
- 只使用既有 coding 包工具，不自动安装外部依赖、不提交代码、不扩大授权。

范围：本地组件与拒绝/回滚路径；真实项目与固定测试依赖由用户选定后再做
正式集成（未提供前不冒称真实项目通过）。
"""

import hashlib
import os
import time
from typing import Any, Dict, List, Optional

from ..tools.coding import CODING_PACK, install_coding_pack
from ..tools.registry import ToolRegistry


class CodingConsumerError(ValueError):
    """请求非法/未安装/工具未知。"""


class CodingConsumerRefused(CodingConsumerError):
    """跨根/链接替换/版本不符等安全拒绝。"""


def _hash_tree(root: str) -> Dict[str, str]:
    """项目根下 .py 文件内容摘要（只读快照，用于磁盘变化对账）。"""
    out: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in (".git", "__pycache__", ".evoagent"))
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, "rb") as handle:
                    out[os.path.relpath(path, root).replace("\\", "/")] = \
                        hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                continue
    return out


class CodingExpertConsumer:
    def __init__(self, *, clock=None, registry_factory=None):
        self.clock = clock or time.time
        self._registry_factory = registry_factory or (lambda: ToolRegistry())
        self._roots: Dict[str, Dict[str, Any]] = {}

    def _root(self, project_root: str) -> str:
        if not isinstance(project_root, str) or not project_root.strip():
            raise CodingConsumerError("project_root 不得为空")
        raw = os.path.abspath(project_root)
        if os.path.islink(raw):
            raise CodingConsumerRefused("项目根是符号链接，拒绝安装（链接替换）")
        root = os.path.realpath(raw)
        if not os.path.isdir(root):
            raise CodingConsumerError("项目根不存在或不是目录: " + project_root)
        return root

    def _entry(self, root: str) -> Dict[str, Any]:
        entry = self._roots.get(root)
        if entry is None:
            raise CodingConsumerError("项目根未安装编码包，拒绝跨根执行")
        return entry

    def install(self, project_root: str, *, expected_version: str,
                actor: str, reason: str) -> Dict[str, Any]:
        root = self._root(project_root)
        if not isinstance(expected_version, str) or not expected_version:
            raise CodingConsumerError("expected_version 不得为空")
        if expected_version != CODING_PACK.version:
            raise CodingConsumerRefused(
                f"编码包版本不符：请求 {expected_version}，实际 {CODING_PACK.version}")
        if not isinstance(actor, str) or not actor.strip() or \
                not isinstance(reason, str) or not reason.strip():
            raise CodingConsumerError("安装必须记录 actor 与 reason")
        existing = self._roots.get(root)
        if existing is not None and existing["version"] == CODING_PACK.version:
            return {"ok": True, "already_installed": True, "root": root,
                    "version": existing["version"],
                    "pack_id": CODING_PACK.pack_id,
                    "tool_ids": list(CODING_PACK.tool_ids),
                    "installed_at": existing["installed_at"]}

        previous = existing
        registry = self._registry_factory()
        before = _hash_tree(root)
        try:
            manifest = install_coding_pack(registry, root)
        except BaseException as exc:  # noqa: BLE001 —— 安装失败必须回滚
            # 回滚：丢弃半装注册表，恢复先前条目（首装则保持未安装）
            if previous is not None:
                self._roots[root] = previous
            else:
                self._roots.pop(root, None)
            after = _hash_tree(root)
            recheck = None
            if previous is not None:
                recheck = self._recheck(previous, root)
            return {"ok": False, "rolled_back": True,
                    "error": f"{type(exc).__name__}: {exc}",
                    "root": root, "restored_version":
                        previous["version"] if previous else None,
                    "disk_changes": self._diff(before, after),
                    "recheck": recheck,
                    "note": "安装失败已回滚到先前状态；未安装外部依赖/未提交代码"}
        entry = {"root": root, "registry": registry,
                 "version": manifest.version, "pack_id": manifest.pack_id,
                 "tool_ids": tuple(manifest.tool_ids),
                 "installed_at": self.clock(), "actor": actor,
                 "reason": reason}
        self._roots[root] = entry
        after = _hash_tree(root)
        return {"ok": True, "already_installed": False, "root": root,
                "version": entry["version"], "pack_id": entry["pack_id"],
                "tool_ids": list(entry["tool_ids"]),
                "installed_at": entry["installed_at"],
                "disk_changes": self._diff(before, after),
                "note": "仅装配既有精确版本编码包；未安装外部依赖/未提交代码"}

    def _recheck(self, entry: Dict[str, Any], root: str) -> Dict[str, Any]:
        result = entry["registry"].execute(
            "coding_syntax_check", {"targets": ["."]},
            allowed_tools=["coding_syntax_check"])
        return {"ok": bool(result.ok),
                "returncode": (result.evidence or {}).get("returncode"),
                "error": result.error}

    @staticmethod
    def _diff(before: Dict[str, str], after: Dict[str, str]) -> Dict[str, List[str]]:
        return {
            "added": sorted(set(after) - set(before)),
            "modified": sorted(k for k in set(before) & set(after)
                               if before[k] != after[k]),
            "removed": sorted(set(before) - set(after)),
        }

    def execute(self, project_root: str, tool_id: str, args: Dict[str, Any],
                *, actor: str, reason: str) -> Dict[str, Any]:
        root = self._root(project_root)
        entry = self._entry(root)
        if tool_id not in entry["tool_ids"]:
            raise CodingConsumerRefused("未知工具/不在本编码包: " + str(tool_id))
        if not isinstance(args, dict):
            raise CodingConsumerError("工具参数必须是对象")
        if not isinstance(actor, str) or not actor.strip() or \
                not isinstance(reason, str) or not reason.strip():
            raise CodingConsumerError("执行必须记录 actor 与 reason")
        before = _hash_tree(root)
        started = time.monotonic()
        result = entry["registry"].execute(
            tool_id, dict(args), allowed_tools=[tool_id])
        elapsed = round(time.monotonic() - started, 3)
        after = _hash_tree(root)
        return {"ok": bool(result.ok),
                "tool_id": tool_id,
                "returncode": (result.evidence or {}).get("returncode"),
                "error": result.error,
                "error_class": result.error_class,
                "duration_s": elapsed,
                "disk_changes": self._diff(before, after),
                "evidence": dict(result.evidence or {}),
                "root": root,
                "actor": actor, "reason": reason,
                "note": "真实工具执行；不自动安装依赖/不提交代码"}

    def status(self, project_root: Optional[str] = None) -> Dict[str, Any]:
        if project_root is not None:
            root = self._root(project_root)
            entry = self._roots.get(root)
            if entry is None:
                return {"root": root, "installed": False}
            return {"root": root, "installed": True,
                    "version": entry["version"], "pack_id": entry["pack_id"],
                    "tool_ids": list(entry["tool_ids"]),
                    "installed_at": entry["installed_at"]}
        return {"roots": sorted(self._roots),
                "count": len(self._roots),
                "entries": [
                    {"root": root, "version": entry["version"],
                     "installed_at": entry["installed_at"]}
                    for root, entry in sorted(self._roots.items())]}
