"""凭证分级、轮换与泄漏防线（ALN-16）。

凭证只保存摘要或公钥，不保存明文秘密。轮换保留旧密钥的验证窗口，
撤销立即生效；导出与模型上下文使用同一套秘密检测。
"""

import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class CredentialError(ValueError):
    pass


class CredentialLeakError(CredentialError):
    pass


TIERS = (
    "service_signing",
    "device_signing",
    "client_trust",
    "api_token",
    "model_provider",
)
DISTRIBUTABLE_TIERS = frozenset({"client_trust"})
_SECRET_KEYS = frozenset(
    {
        "token",
        "api_token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "private_key",
        "signing_secret",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "access_token",
        "refresh_token",
        "client_secret",
        "session_token",
        "aws_secret_access_key",
    }
)
_NORMALIZED_SECRET_KEYS = frozenset(
    re.sub(r"[^a-z0-9]", "", name.casefold()) for name in _SECRET_KEYS
)


def _sensitive_key(key: Any) -> bool:
    return re.sub(r"[^a-z0-9]", "", str(key).casefold()) in _NORMALIZED_SECRET_KEYS


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


# Include quoted/escaped JSON assignments and redact the complete quoted value,
# including spaces. A partial pasted value still must not leak its prefix.
_ASSIGNMENT = re.compile(r"""(?ix)\b
    (?:api[_-]?key|api[_-]?token|access[_-]?token|refresh[_-]?token|session[_-]?token|
       client[_-]?secret|signing[_-]?secret|aws_secret_access_key|private[_-]?key|
       token|secret|password|authorization|bearer|credentials?)
    (?:\\?["'])?\s*[:=]\s*
    (?:
       \\"(?:(?!\\").)+(?:\\"|$)
      |\\'(?:(?!\\').)+(?:\\'|$)
      |"(?:\\.|[^"\\])+(?:"|$)
      |'(?:\\.|[^'\\])+(?:'|$)
      |[^\s\\"',;{}\[\]]+
    )
""")

_PATTERNS = (
    (
        "authorization",
        re.compile(
            r"""(?i)\bauthorization(?:\\?["'])?\s*[:=]\s*(?:\\?["'])?"""
            r"""(?:basic|bearer)\s+[^\s\\"',;}]+"""
        ),
    ),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}")),
    ("api_key", _ASSIGNMENT),
    ("openai", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")),
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{6,}\b")),
    (
        "github",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "pem",
        re.compile(
            r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[\s\S]*?"
            r"(?:-----END (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|$)",
            re.IGNORECASE,
        ),
    ),
)


def _json_value(text: str):
    if text.lstrip()[:1] not in ("{", "[", '"'):
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def contains_secret(value: Any) -> bool:
    if isinstance(value, str):
        structured = _json_value(value)
        if structured is not None:
            return contains_secret(structured)
        return any(pattern.search(value) for _, pattern in _PATTERNS)
    if isinstance(value, dict):
        return any(
            (_sensitive_key(k) and _has_value(v))
            or contains_secret(k)
            or contains_secret(v)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(contains_secret(v) for v in value)
    return False


def scrub_text(text: str) -> str:
    cleaned = str(text)
    structured = _json_value(cleaned)
    if structured is not None:
        return (
            "<redacted:structured_credentials>"
            if contains_secret(structured)
            else cleaned
        )
    for name, pattern in _PATTERNS:
        cleaned = pattern.sub(f"<redacted:{name}>", cleaned)
    return cleaned


def scrub_value(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "<depth-limit>"
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            scrub_text(str(k)): (
                "<redacted:sensitive_field>"
                if _sensitive_key(k) and _has_value(v)
                else scrub_value(v, depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [scrub_value(v, depth + 1) for v in value]
    return value


def assert_context_safe(value: Any, path: str = "$") -> None:
    """模型上下文/导出前的硬门禁：秘密值或敏感键名直接拒绝。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if contains_secret(str(key)):
                raise CredentialLeakError(f"敏感字段名不得进入上下文: {path}")
            if _sensitive_key(key) and _has_value(item):
                raise CredentialLeakError(f"敏感字段不得进入上下文: {path}.{key}")
            assert_context_safe(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            assert_context_safe(item, f"{path}[{index}]")
        return
    if isinstance(value, str) and contains_secret(value):
        raise CredentialLeakError(f"疑似凭证不得进入上下文: {path}")


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass
class CredentialEntry:
    key_id: str
    tier: str
    fingerprint: str
    status: str = "active"
    not_before: float = 0.0
    not_after: Optional[float] = None
    rotated_from: str = ""
    public_key: str = ""
    revoke_reason: str = ""
    revoked_by: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_public: bool = True) -> Dict[str, Any]:
        out = {
            "key_id": self.key_id,
            "tier": self.tier,
            "status": self.status,
            "not_before": self.not_before,
            "not_after": self.not_after,
            "rotated_from": self.rotated_from,
            "revoke_reason": self.revoke_reason,
            "revoked_by": self.revoked_by,
        }
        if include_public and self.public_key:
            out["public_key"] = self.public_key
        return out


class CredentialRegistry:
    """按用途分层的凭证环；秘密只以摘要形式驻留。"""

    def __init__(self) -> None:
        self._entries: Dict[str, CredentialEntry] = {}

    def register(
        self,
        key_id: str,
        tier: str,
        *,
        secret: Optional[str] = None,
        public_key: str = "",
        not_before: Optional[float] = None,
        not_after: Optional[float] = None,
        rotated_from: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CredentialEntry:
        if not key_id:
            raise CredentialError("key_id 不能为空")
        if tier not in TIERS:
            raise CredentialError(f"未知凭证层级: {tier}")
        if key_id in self._entries:
            raise CredentialError(f"凭证已存在: {key_id}")
        if not secret and not public_key:
            raise CredentialError("凭证必须提供 secret 或 public_key")
        if tier == "client_trust" and not public_key:
            raise CredentialError("客户端信任层需要公钥")
        entry = CredentialEntry(
            key_id=key_id,
            tier=tier,
            fingerprint=_digest(secret) if secret else _digest(public_key),
            not_before=time.time() if not_before is None else not_before,
            not_after=not_after,
            rotated_from=rotated_from,
            public_key=public_key,
            metadata=dict(metadata or {}),
        )
        self._entries[key_id] = entry
        return entry

    def rotate(
        self,
        old_key_id: str,
        new_key_id: str,
        *,
        secret: Optional[str] = None,
        public_key: str = "",
        grace_seconds: float = 86400,
        at: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CredentialEntry:
        now = time.time() if at is None else at
        old = self._entries.get(old_key_id)
        if old is None:
            raise CredentialError(f"旧凭证不存在: {old_key_id}")
        if old.status == "revoked":
            raise CredentialError("已撤销凭证不能轮换")
        if new_key_id in self._entries:
            raise CredentialError(f"凭证已存在: {new_key_id}")
        new = self.register(
            new_key_id,
            old.tier,
            secret=secret,
            public_key=public_key,
            not_before=now,
            rotated_from=old_key_id,
            metadata=metadata,
        )
        # Validate/register first: a failed rotation must not retire a live key.
        old.status = "retired"
        deadline = now + max(0.0, grace_seconds)
        old.not_after = (
            min(old.not_after, deadline) if old.not_after is not None else deadline
        )
        return new

    def revoke(
        self, key_id: str, *, actor: str, reason: str, at: Optional[float] = None
    ) -> CredentialEntry:
        if not actor or not reason:
            raise CredentialError("撤销凭证必须记录 actor 与 reason")
        entry = self._entries.get(key_id)
        if entry is None:
            raise CredentialError(f"凭证不存在: {key_id}")
        entry.status = "revoked"
        entry.revoked_by = actor
        entry.revoke_reason = reason
        entry.not_after = time.time() if at is None else at
        return entry

    def verify_secret(
        self, key_id: str, secret: str, at: Optional[float] = None
    ) -> bool:
        entry = self._entries.get(key_id)
        if entry is None or entry.status == "revoked":
            return False
        now = time.time() if at is None else at
        if now < entry.not_before:
            return False
        if entry.not_after is not None and now >= entry.not_after:
            return False
        return hmac.compare_digest(entry.fingerprint, _digest(secret))

    def get(self, key_id: str) -> Optional[CredentialEntry]:
        return self._entries.get(key_id)

    def status(self, key_id: str) -> Dict[str, Any]:
        entry = self._entries.get(key_id)
        if entry is None:
            raise CredentialError(f"凭证不存在: {key_id}")
        out = entry.to_dict(include_public=False)
        out["distributable"] = entry.tier in DISTRIBUTABLE_TIERS
        out["explanation"] = self._explain(entry)
        return out

    def export_public_keys(self) -> List[Dict[str, Any]]:
        """只导出客户端可信任公钥；服务端/模型秘密永不进入导出。"""
        return [
            entry.to_dict(include_public=True)
            for entry in self._entries.values()
            if entry.tier in DISTRIBUTABLE_TIERS
            and entry.public_key
            and entry.status != "revoked"
        ]

    @staticmethod
    def _explain(entry: CredentialEntry) -> str:
        if entry.status == "revoked":
            return f"已撤销：{entry.revoke_reason or '未备注'}；新旧验证均拒绝"
        if entry.status == "retired":
            until = entry.not_after
            return f"已轮换退役，保留验证窗口至 {until}；窗口结束后拒绝"
        return "有效：按层级使用，秘密材料不进导出/日志/模型上下文"


@dataclass
class TokenEntry:
    key_id: str
    role: str
    fingerprint: str
    status: str = "active"
    not_after: Optional[float] = None
    revoke_reason: str = ""


class TokenRing:
    """API 令牌轮换环；不保存明文，比较使用常量时间摘要。"""

    ROLES = ("operator", "admin")

    def __init__(self) -> None:
        self._entries: Dict[str, TokenEntry] = {}

    def add(
        self,
        secret: str,
        role: str,
        *,
        key_id: str = "",
        not_after: Optional[float] = None,
    ) -> str:
        if role not in self.ROLES:
            raise CredentialError(f"未知角色: {role}")
        if not secret:
            raise CredentialError("令牌不能为空")
        identity = key_id or "tok_" + uuid.uuid4().hex[:12]
        if identity in self._entries:
            raise CredentialError(f"令牌已存在: {identity}")
        if self.knows_secret(secret):
            raise CredentialError("令牌秘密不得重复使用")
        self._entries[identity] = TokenEntry(
            key_id=identity, role=role, fingerprint=_digest(secret), not_after=not_after
        )
        return identity

    def rotate(
        self,
        old_key_id: str,
        new_secret: str,
        *,
        grace_seconds: float = 86400,
        at: Optional[float] = None,
    ) -> str:
        now = time.time() if at is None else at
        old = self._entries.get(old_key_id)
        if old is None:
            raise CredentialError(f"令牌不存在: {old_key_id}")
        if old.status == "revoked":
            raise CredentialError("已撤销令牌不能轮换")
        new_id = self.add(new_secret, old.role, not_after=None)
        old.status = "retired"
        deadline = now + max(0.0, grace_seconds)
        old.not_after = (
            min(old.not_after, deadline) if old.not_after is not None else deadline
        )
        return new_id

    def knows_secret(self, secret: str) -> bool:
        """Include revoked/expired entries so legacy auth cannot resurrect them."""
        candidate = _digest(secret)
        matched = False
        for entry in self._entries.values():
            matched |= hmac.compare_digest(entry.fingerprint, candidate)
        return matched

    def revoke(self, key_id: str, *, reason: str) -> None:
        if not reason:
            raise CredentialError("撤销令牌必须记录 reason")
        entry = self._entries.get(key_id)
        if entry is None:
            raise CredentialError(f"令牌不存在: {key_id}")
        entry.status = "revoked"
        entry.revoke_reason = reason
        entry.not_after = 0.0

    def role_for(self, secret: str, at: Optional[float] = None) -> Optional[str]:
        if not secret:
            return None
        now = time.time() if at is None else at
        candidate = _digest(secret)
        matched: Optional[str] = None
        for entry in self._entries.values():
            if entry.status == "revoked":
                continue
            if entry.not_after is not None and now >= entry.not_after:
                continue
            # 不提前返回，避免通过耗时猜测匹配位置
            if hmac.compare_digest(entry.fingerprint, candidate):
                matched = entry.role
        return matched

    def status(self, key_id: str) -> Dict[str, Any]:
        entry = self._entries.get(key_id)
        if entry is None:
            raise CredentialError(f"令牌不存在: {key_id}")
        return {
            "key_id": key_id,
            "role": entry.role,
            "status": entry.status,
            "not_after": entry.not_after,
            "revoke_reason": entry.revoke_reason,
        }
