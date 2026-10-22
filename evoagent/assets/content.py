"""资产内容规范化与 Pack 字节清单（ALN-06-A 步骤 3/4）。

两步分开是刻意的：
- **内容规范化**保证同一逻辑内容得到同一摘要，登记幂等可判定；
- **Pack 字节清单**用真实文件字节（sha256 + size）验证装载协议，
  本地夹具即可证明“清单与目录一致”，不伪称端侧已有真实装载。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .entity import AssetValidationError, PackVerificationError

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_bytes(content: Any) -> bytes:
    """稳定序列化：键排序、无多余空白、UTF-8。

    同内容不同字典插入顺序必须得到同一字节序列 —— 否则摘要会漂移。
    """
    try:
        text = json.dumps(content, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AssetValidationError(f"资产内容不可规范化: {exc}") from exc
    return text.encode("utf-8")


def content_digest(content: Any) -> str:
    """契约 §6 的 content_digest（64 位小写十六进制）。"""
    return hashlib.sha256(canonical_bytes(content)).hexdigest()


def bytes_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_pack_path(path: str) -> None:
    if not isinstance(path, str) or not path:
        raise AssetValidationError("Pack 文件路径不得为空")
    if "\\" in path:
        raise AssetValidationError(
            f"Pack 文件路径必须使用正斜杠: {path!r}")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise AssetValidationError(f"Pack 文件路径不得为绝对路径: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise AssetValidationError(
            f"Pack 文件路径不得含空段/./..: {path!r}")


@dataclass(frozen=True)
class PackFile:
    path: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        _safe_pack_path(self.path)
        if not _DIGEST_RE.match(self.sha256 or ""):
            raise AssetValidationError(
                f"Pack 文件摘要必须是 64 位小写十六进制: {self.path!r}")
        if not isinstance(self.size, int) or self.size < 0:
            raise AssetValidationError(
                f"Pack 文件大小非法: {self.path!r} -> {self.size!r}")


@dataclass(frozen=True)
class PackManifest:
    """Skill/Capability Pack 的可验证目录。"""

    pack_id: str
    version: str
    files: Tuple[PackFile, ...]
    permissions: Tuple[str, ...] = ()
    dependency_refs: Tuple[str, ...] = ()
    evaluation_evidence_refs: Tuple[str, ...] = ()
    rollback_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.pack_id or not self.version:
            raise AssetValidationError("Pack pack_id/version 不得为空")
        seen = set()
        for item in self.files:
            if item.path in seen:
                raise AssetValidationError(
                    f"Pack 清单含重复路径: {item.path!r}")
            seen.add(item.path)

    def by_path(self) -> Dict[str, PackFile]:
        return {f.path: f for f in self.files}

    def to_dict(self) -> Dict[str, Any]:
        """端侧可消费的字节清单格式（不含任何签名秘密）。"""
        return {
            "pack_id": self.pack_id,
            "version": self.version,
            "files": [{"path": f.path, "sha256": f.sha256,
                       "size": f.size} for f in self.files],
            "permissions": list(self.permissions),
            "dependency_refs": list(self.dependency_refs),
            "evaluation_evidence_refs": list(self.evaluation_evidence_refs),
            "rollback_ref": self.rollback_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PackManifest":
        if not isinstance(data, Mapping):
            raise AssetValidationError("Pack 清单必须是 JSON 对象")
        raw_files = data.get("files")
        if not isinstance(raw_files, (list, tuple)):
            raise AssetValidationError("Pack 清单 files 必须是数组")
        files = []
        for item in raw_files:
            if not isinstance(item, Mapping):
                raise AssetValidationError("Pack 清单文件项必须是对象")
            try:
                size = int(item.get("size", -1))
            except (TypeError, ValueError) as exc:
                raise AssetValidationError(
                    f"Pack 清单文件大小非法: {item.get('size')!r}") from exc
            files.append(PackFile(path=str(item.get("path") or ""),
                                  sha256=str(item.get("sha256") or ""),
                                  size=size))
        return cls(
            pack_id=str(data.get("pack_id") or ""),
            version=str(data.get("version") or ""),
            files=tuple(files),
            permissions=tuple(str(x) for x in data.get("permissions") or ()),
            dependency_refs=tuple(
                str(x) for x in data.get("dependency_refs") or ()),
            evaluation_evidence_refs=tuple(
                str(x) for x in data.get("evaluation_evidence_refs") or ()),
            rollback_ref=(str(data["rollback_ref"])
                          if data.get("rollback_ref") else None))


@dataclass(frozen=True)
class PackVerification:
    ok: bool
    checked: int
    errors: Tuple[str, ...] = ()
    unused_files: Tuple[str, ...] = ()


def verify_pack(manifest: PackManifest,
                files: Mapping[str, bytes],
                require_exact: bool = True) -> PackVerification:
    """逐字节校验清单：摘要、大小、缺失、多余与路径安全。"""
    errors = []
    listed = manifest.by_path()
    for path in sorted(listed):
        entry = listed[path]
        data = files.get(path)
        if data is None:
            errors.append(f"缺少文件: {path}")
            continue
        if not isinstance(data, (bytes, bytearray)):
            errors.append(f"文件内容必须是字节: {path}")
            continue
        actual = bytes_digest(bytes(data))
        if actual != entry.sha256:
            errors.append(
                f"摘要不符: {path} 期望 {entry.sha256} 实际 {actual}")
        if len(data) != entry.size:
            errors.append(
                f"大小不符: {path} 期望 {entry.size} 实际 {len(data)}")
    extra = sorted(set(files) - set(listed))
    if require_exact and extra:
        errors.append(f"清单外文件: {', '.join(extra)}")
    return PackVerification(ok=not errors, checked=len(listed),
                            errors=tuple(errors),
                            unused_files=tuple(extra))


@dataclass(frozen=True)
class LoadedPack:
    """本地验证后的 Pack 视图（仅内存，未安装到端侧）。"""

    manifest: PackManifest
    files: Dict[str, bytes] = field(default_factory=dict)
    installed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pack_id": self.manifest.pack_id,
            "version": self.manifest.version,
            "file_count": len(self.files),
            "permissions": list(self.manifest.permissions),
            "dependency_refs": list(self.manifest.dependency_refs),
            "evaluation_evidence_refs": list(
                self.manifest.evaluation_evidence_refs),
            "rollback_ref": self.manifest.rollback_ref,
            # 诚实标记：本地夹具只验证装载协议，不代表端侧已装载。
            "installed": self.installed,
        }


def load_pack(manifest: PackManifest,
              files: Mapping[str, bytes]) -> LoadedPack:
    """验证并返回内存中的 Pack；不写端侧目录、不声明安装成功。"""
    result = verify_pack(manifest, files)
    if not result.ok:
        raise PackVerificationError("; ".join(result.errors))
    ordered = {p: bytes(files[p]) for p in sorted(files)}
    return LoadedPack(manifest=manifest, files=ordered, installed=False)


def build_manifest(pack_id: str, version: str,
                   files: Mapping[str, bytes],
                   **kwargs: Any) -> PackManifest:
    """由真实字节生成清单 —— 夹具与生产端共用同一构造。"""
    entries = tuple(
        PackFile(path=p, sha256=bytes_digest(bytes(files[p])),
                 size=len(files[p]))
        for p in sorted(files))
    return PackManifest(pack_id=pack_id, version=version, files=entries,
                        **kwargs)


def manifest_paths(manifest: PackManifest) -> Iterable[str]:
    return (f.path for f in manifest.files)
