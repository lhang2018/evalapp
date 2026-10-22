"""冻结运行清单中的资产引用（ALN-06-A 步骤 5 的纯数据边界）。

运行 Manifest 由 runtime/integration 线接线；本模块只定义引用形状与
校验规则：清单同时记录 `asset_id + version + content_digest`，
读取时三者必须一致。别名或“最新指针”即使变化也影响不到已冻结清单。
"""

from __future__ import annotations

from typing import Any, Mapping

from .entity import Asset, AssetConflict, AssetNotFound, AssetValidationError
from .repository import AssetRepository

_FIELDS = ("asset_id", "version", "content_digest")


def manifest_asset_ref(asset: Asset) -> dict:
    """生成可写入 Manifest 的最小引用（无内容载荷）。"""
    return {"asset_id": asset.asset_id, "version": asset.version,
            "content_digest": asset.content_digest}


def verify_manifest_ref(ref: Mapping[str, Any],
                        repo: AssetRepository) -> Asset:
    """按冻结清单精确读取；摘要不符视为清单被篡改。"""
    if not isinstance(ref, Mapping):
        raise AssetValidationError("Manifest 资产引用必须是对象")
    missing = [f for f in _FIELDS if not ref.get(f)]
    if missing:
        raise AssetValidationError(
            f"Manifest 资产引用缺少字段: {', '.join(missing)}")
    asset = repo.get(str(ref["asset_id"]), str(ref["version"]))
    if asset is None:
        raise AssetNotFound(
            f"Manifest 引用的资产不存在: {ref['asset_id']}@{ref['version']}")
    if asset.content_digest != str(ref["content_digest"]):
        raise AssetConflict(
            f"Manifest 摘要不符: {asset.ref} "
            f"清单 {ref['content_digest']} / 实际 {asset.content_digest}")
    return asset
