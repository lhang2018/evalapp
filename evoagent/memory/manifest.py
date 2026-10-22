"""身份作用域与不可变 ContextManifest（ALN-05）。

身份五类：user / project / session / task / expert。
- user 最宽，project 次之，session 再次，task 最窄；窄作用域可继承宽作用域。
- expert 是横向领域蜂巢，只有显式 expert_id 才能读取。
- 跨作用域读取必须提供带 actor/reason 的 AccessGrant，不能靠空键或自由文本。

ContextManifest 只保存引用与摘要（content_digest / 来源 / 版本 / 时间 /
选择原因 / token 预算 / 压缩前后条目 ID），不保存正文。摘要不含装配时间与
随机 ID，因此相同输入重复装配得到相同 digest。
"""

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

MANIFEST_SCHEMA = "evoagent.context.manifest/1"
SCOPE_FIELDS = {
    "user": "user_id",
    "project": "project_id",
    "session": "session_id",
    "task": "task_id",
    "expert": "expert_id",
}
_IDENTITY_FIELDS = tuple(SCOPE_FIELDS.values())


class MemoryAccessError(PermissionError):
    """越权读写：显式拒绝而不是返回空结果。"""


@dataclass(frozen=True)
class MemoryIdentity:
    user_id: str = ""
    project_id: str = ""
    session_id: str = ""
    task_id: str = ""
    expert_id: str = ""

    def __post_init__(self) -> None:
        for name in _IDENTITY_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"身份字段 {name} 必须是字符串")
            if value != value.strip():
                raise ValueError(f"身份字段 {name} 不能有首尾空白")

    def scope_keys(self) -> Dict[str, str]:
        """窄作用域在前；空键作用域不参与继承。"""
        out: Dict[str, str] = {}
        for scope, name in SCOPE_FIELDS.items():
            value = getattr(self, name)
            if value:
                out[scope] = value
        # 共享层永远可选，但带 owner 的共享记忆仍由 owner 过滤。
        out.setdefault("shared", "")
        return out

    def field_for(self, scope: str) -> str:
        return SCOPE_FIELDS.get(scope, "")

    def to_dict(self) -> Dict[str, str]:
        return {name: getattr(self, name) for name in _IDENTITY_FIELDS}


@dataclass(frozen=True)
class AccessGrant:
    """显式跨作用域授权；actor/reason 是审计必需项。"""

    scopes: Tuple[str, ...]
    identity: MemoryIdentity
    actor: str
    reason: str

    def __post_init__(self) -> None:
        normalized = tuple(sorted({
            str(getattr(scope, "value", scope)) for scope in self.scopes}))
        if not normalized:
            raise ValueError("AccessGrant 至少需要一个作用域")
        unknown = [scope for scope in normalized if scope not in SCOPE_FIELDS]
        if unknown:
            raise ValueError(f"AccessGrant 含未知作用域: {unknown}")
        if not isinstance(self.identity, MemoryIdentity):
            raise ValueError("AccessGrant 必须携带被授权身份")
        if not str(self.actor or "").strip() or not str(self.reason or "").strip():
            raise ValueError("AccessGrant 必须记录 actor 与 reason")
        object.__setattr__(self, "scopes", normalized)

    def scope_map(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for scope in self.scopes:
            value = getattr(self.identity, SCOPE_FIELDS[scope])
            if not value:
                raise ValueError(f"AccessGrant 身份缺少 {scope} 作用域键")
            out[scope] = value
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"scopes": list(self.scopes), "actor": self.actor,
                "reason": self.reason,
                "identity": self.identity.to_dict()}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def token_estimate(text: str) -> int:
    return max(1, math.ceil(len(text or "") / 4))


@dataclass(frozen=True)
class ContextEntry:
    entry_id: str
    kind: str
    source: str = ""
    source_id: str = ""
    source_version: str = ""
    scope: str = ""
    scope_key: str = ""
    reason: str = ""
    included: bool = True
    token_estimate: int = 0
    content_digest: str = ""
    created_at: Optional[float] = None
    conflict_ids: Tuple[str, ...] = ()
    pre_compress_ref: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "source": self.source,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "scope": self.scope,
            "scope_key": self.scope_key,
            "reason": self.reason,
            "included": self.included,
            "token_estimate": self.token_estimate,
            "content_digest": self.content_digest,
            "created_at": self.created_at,
            "conflict_ids": list(self.conflict_ids),
            "pre_compress_ref": self.pre_compress_ref,
        }


def make_entry(kind: str, text: str, *, index: int = 0,
               source: str = "", source_id: str = "", source_version: str = "",
               scope: str = "", scope_key: str = "", reason: str = "",
               included: bool = True, created_at: Optional[float] = None,
               conflict_ids: Iterable[str] = ()) -> ContextEntry:
    digest = _digest_text(text)
    entry_id = f"{kind}:{digest[:12]}"
    if index:
        entry_id = f"{entry_id}:{index}"
    return ContextEntry(
        entry_id=entry_id, kind=kind, source=source, source_id=source_id,
        source_version=source_version, scope=scope, scope_key=scope_key,
        reason=reason, included=included,
        token_estimate=token_estimate(text), content_digest=digest,
        created_at=created_at, conflict_ids=tuple(conflict_ids),
        pre_compress_ref=entry_id)


@dataclass(frozen=True)
class ContextManifest:
    manifest_id: str
    digest: str
    assembly: Mapping[str, Any]
    budget_tokens: int
    entries: Tuple[ContextEntry, ...]
    compression: Mapping[str, Any]
    created_at: float

    @classmethod
    def build(cls, *, assembly: Mapping[str, Any], budget_tokens: int,
              entries: Iterable[ContextEntry],
              compression: Mapping[str, Any],
              created_at: Optional[float] = None) -> "ContextManifest":
        frozen_entries = tuple(entries)
        payload = {
            "schema": MANIFEST_SCHEMA,
            "assembly": dict(assembly),
            "budget_tokens": int(budget_tokens),
            "entries": [entry.to_dict() for entry in frozen_entries],
            "compression": dict(compression),
        }
        digest = hashlib.sha256(_canonical(payload)).hexdigest()
        return cls(manifest_id=f"ctx_{digest[:16]}", digest=digest,
                   assembly=dict(assembly), budget_tokens=int(budget_tokens),
                   entries=frozen_entries, compression=dict(compression),
                   created_at=float(created_at if created_at is not None
                                    else time.time()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": MANIFEST_SCHEMA,
            "manifest_id": self.manifest_id,
            "digest": self.digest,
            "assembly": dict(self.assembly),
            "budget_tokens": self.budget_tokens,
            "entries": [entry.to_dict() for entry in self.entries],
            "compression": dict(self.compression),
            "created_at": self.created_at,
        }

    def references(self) -> List[str]:
        return [entry.entry_id for entry in self.entries]

    def conflict_ids(self) -> List[str]:
        out: List[str] = []
        for entry in self.entries:
            out.extend(entry.conflict_ids)
        return sorted(set(out))
