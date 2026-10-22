"""能力资产域（契约 §6 / F-11 / ALN-06-A）。

只包含资产身份、内容规范化、Pack 校验与不可变仓储；
共享契约/持久化/运行清单/Android 接入不在本包首轮范围内。
"""

from .content import (PackFile, PackManifest, PackVerification,
                      build_manifest, canonical_bytes, content_digest,
                      load_pack, verify_pack)
from .entity import (ALLOWED_TRANSITIONS, EVIDENCE_REQUIRED_STATES, Asset,
                     AssetConflict, AssetError, AssetNotFound, AssetRef,
                     AssetValidationError, DependencyError, LifecycleError,
                     PackVerificationError, can_transition)
from .mapping import (AssetDraft, DEFAULT_NAMESPACE, REGISTERABLE_STATES,
                      asset_id_for, capability_to_asset, expert_to_asset,
                      map_inventory, model_profile_to_asset,
                      workflow_to_asset)
from .refs import manifest_asset_ref, verify_manifest_ref
from .repository import AssetRepository, repository_for

__all__ = [
    "ALLOWED_TRANSITIONS", "EVIDENCE_REQUIRED_STATES", "Asset",
    "AssetConflict", "AssetDraft", "AssetError", "AssetNotFound", "AssetRef",
    "AssetRepository", "AssetValidationError", "DEFAULT_NAMESPACE",
    "DependencyError", "LifecycleError", "PackFile", "PackManifest",
    "PackVerification", "PackVerificationError", "REGISTERABLE_STATES",
    "asset_id_for", "build_manifest", "can_transition", "canonical_bytes",
    "capability_to_asset", "content_digest", "expert_to_asset", "load_pack",
    "manifest_asset_ref", "map_inventory", "model_profile_to_asset",
    "repository_for", "verify_manifest_ref", "verify_pack",
    "workflow_to_asset",
]
