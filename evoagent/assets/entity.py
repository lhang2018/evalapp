"""能力资产身份（契约 §6 / F-11 / ALN-06-A）。

正式契约以 `docs/.../EvoAgent_核心接口与数据契约正式规范.md` §6 为准；
枚举唯一权威是 `evoagent/core/contracts.py` 的 `ASSET_TYPE` /
`ASSET_NAMESPACE` / `ASSET_LIFECYCLE`。本模块不重复定义取值。

**不可变引用**：消费者必须拿 `AssetRef`（asset_id + version），
不能拿可变的“最新版本”字符串。版本一旦登记，内容摘要不得变化。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..core.contracts import ASSET_LIFECYCLE, ASSET_NAMESPACE, ASSET_TYPE

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

#: 生命周期允许的迁移。终态 retired 不可离开。
ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "draft": ("candidate", "retired"),
    "candidate": ("validating", "draft", "retired"),
    "validating": ("approved", "candidate", "retired"),
    "approved": ("active", "validating", "retired"),
    "active": ("degraded", "deprecated", "retired"),
    "degraded": ("active", "deprecated", "retired"),
    "deprecated": ("retired",),
    "retired": (),
}

#: 进入这些状态必须已经具备评测证据与可解析依赖（验收：证据不足不得晋升）。
EVIDENCE_REQUIRED_STATES = ("approved", "active")
DEPENDENCY_REQUIRED_STATES = ("approved", "active")


class AssetError(Exception):
    """资产域错误基类。"""


class AssetNotFound(AssetError):
    """按精确版本读取时不存在。"""


class AssetConflict(AssetError):
    """同 asset_id/version 的内容摘要不同 —— 不可变契约被违反。"""


class AssetValidationError(AssetError):
    """字段/枚举/引用格式非法。"""


class LifecycleError(AssetError):
    """生命周期迁移不被允许，或晋升前置条件不满足。"""


class DependencyError(AssetError):
    """依赖缺失、成环或兼容性声明不满足。"""


class PackVerificationError(AssetError):
    """Pack 字节清单与目录不符。"""


@dataclass(frozen=True)
class AssetRef:
    """精确资产引用：`asset_id@version`。

    没有“最新版本”解析入口是刻意设计 —— 冻结运行必须能重现它当时
    实际读取的内容，别名漂移会破坏这一点。
    """

    asset_id: str
    version: str

    def __post_init__(self) -> None:
        if not self.asset_id or "@" in self.asset_id:
            raise AssetValidationError(
                f"asset_id 非法（不得为空或含 @）: {self.asset_id!r}")
        if not self.version or "@" in self.version:
            raise AssetValidationError(
                f"version 非法（不得为空或含 @）: {self.version!r}")

    @classmethod
    def parse(cls, text: str) -> "AssetRef":
        if not isinstance(text, str) or "@" not in text:
            raise AssetValidationError(
                f"资产引用必须形如 asset_id@version: {text!r}")
        asset_id, version = text.rsplit("@", 1)
        return cls(asset_id=asset_id, version=version)

    def __str__(self) -> str:
        return f"{self.asset_id}@{self.version}"


@dataclass(frozen=True)
class Asset:
    """契约 §6 的 Capability Asset（十一字段 + 内容载荷）。"""

    asset_id: str
    asset_type: str
    namespace: str
    version: str
    content_digest: str
    source_refs: Tuple[str, ...] = ()
    dependency_refs: Tuple[str, ...] = ()
    compatibility: Dict[str, Any] = field(default_factory=dict)
    lifecycle_state: str = "draft"
    evaluation_evidence_refs: Tuple[str, ...] = ()
    rollback_ref: Optional[str] = None
    #: 规范化后的资产内容（按摘要校验），不属于契约的元数据面。
    content: Dict[str, Any] = field(default_factory=dict,
                                    compare=False, repr=False)
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def ref(self) -> AssetRef:
        return AssetRef(self.asset_id, self.version)

    def validate(self) -> None:
        if not self.asset_id or not self.version:
            raise AssetValidationError("asset_id/version 不得为空")
        if self.asset_type not in ASSET_TYPE:
            raise AssetValidationError(
                f"未知 asset_type: {self.asset_type!r}（契约 §6 八值）")
        if self.namespace not in ASSET_NAMESPACE:
            raise AssetValidationError(
                f"未知 namespace: {self.namespace!r}（契约 §6 五域）")
        if self.lifecycle_state not in ASSET_LIFECYCLE:
            raise AssetValidationError(
                f"未知 lifecycle_state: {self.lifecycle_state!r}（八态）")
        if not _DIGEST_RE.match(self.content_digest or ""):
            raise AssetValidationError(
                "content_digest 必须是 64 位小写十六进制 sha256")
        for text in self.source_refs:
            if not text:
                raise AssetValidationError("source_refs 不得含空引用")
        for text in self.dependency_refs:
            AssetRef.parse(text)
        if self.rollback_ref is not None:
            AssetRef.parse(self.rollback_ref)
        unknown = dict(self.compatibility.get("pins") or {})
        for asset_id, digest in unknown.items():
            if not asset_id or not _DIGEST_RE.match(str(digest)):
                raise AssetValidationError(
                    f"compatibility.pins 非法: {asset_id!r} -> {digest!r}")

    def to_contract_dict(self) -> Dict[str, Any]:
        """只返回契约 §6 字段（不含内容载荷与库内部字段）。"""
        return {
            "asset_id": self.asset_id,
            "asset_type": self.asset_type,
            "namespace": self.namespace,
            "version": self.version,
            "content_digest": self.content_digest,
            "source_refs": list(self.source_refs),
            "dependency_refs": list(self.dependency_refs),
            "compatibility": dict(self.compatibility),
            "lifecycle_state": self.lifecycle_state,
            "evaluation_evidence_refs": list(self.evaluation_evidence_refs),
            "rollback_ref": self.rollback_ref,
        }

    def summary(self) -> Dict[str, Any]:
        out = self.to_contract_dict()
        out["created_at"] = self.created_at
        out["updated_at"] = self.updated_at
        return out


def can_transition(from_state: str, to_state: str) -> bool:
    if from_state not in ASSET_LIFECYCLE or to_state not in ASSET_LIFECYCLE:
        return False
    return to_state in ALLOWED_TRANSITIONS[from_state]


def ensure_transition(from_state: str, to_state: str) -> None:
    if from_state == to_state:
        raise LifecycleError(f"状态未变化: {from_state}")
    if not can_transition(from_state, to_state):
        allowed = ALLOWED_TRANSITIONS.get(from_state, ())
        raise LifecycleError(
            f"非法生命周期迁移 {from_state} -> {to_state}；"
            f"允许: {list(allowed)}")


def refs_of(items: List[str]) -> Tuple[AssetRef, ...]:
    return tuple(AssetRef.parse(x) for x in items)
