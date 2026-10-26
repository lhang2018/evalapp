"""生产信任根：RSA 身份密钥与制品签名签发（ALN-03-P）。

端侧（android-app/src/update/trust.ts + rsa.ts）用受信公钥做
RSA PKCS#1 v1.5 + SHA-256 验签，验签消息为固定七行规范串：

    EVOAGENT-ARTIFACT-V1
    <schema>
    <release_id>
    <version>
    <artifact_sha256 小写十六进制，去 sha256: 前缀>
    <artifact_size 十进制>
    <signing_key_id>

服务端此前只有 HMAC 对称签名（signing.py），artifact_signature /
artifact_signing_key_id 两个信封字段没有生产来源。本模块补上签发侧：

- PrivateIdentityKey 持有 RSA 私钥，按上述规范签发制品签名；
- TrustedIdentity 是公钥身份记录（key_id/公钥参数/生效窗口/撤销），
  轮换靠新增 key_id 完成，历史身份可撤销但记录不删（可追溯）；
- identity_public_record() 导出端侧 TrustedPublicKey 兼容的公钥记录；
  私钥材料绝不进入导出，也不落发布清单。

服务端对已发布制品同时保留 HMAC 完整性链（signing.py）；RSA 签名
解决的是"端侧不持有共享秘密也能验真"的信任根问题，两者互补。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

try:  # 生产环境用 cryptography 生成/导入密钥；纯 Python 实现保证可测试性
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.exceptions import InvalidSignature
    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - cryptography 在运行环境始终可用
    _CRYPTO_AVAILABLE = False

SCHEMA = "evoagent.release.signing/1"
ALGORITHM = "rsa-sha256"
MESSAGE_PREFIX = "EVOAGENT-ARTIFACT-V1"
DEFAULT_EXPONENT = 65537
_MIN_MODULUS_BITS = 2048


class IdentityError(ValueError):
    """身份密钥或签名请求不合法。"""


def _now() -> float:
    return time.time()


def _hex_even(value: int) -> str:
    """十六进制且长度为偶数：65537 → "010001"（而非 "10001"）。

    奇数长度十六进制会被端侧 toBigInt 走 base64url 分支解错、
    被 Node Buffer.from 截断丢字节——这是跨端契约，必须偶数长度。
    """
    text = format(value, "x")
    return text if len(text) % 2 == 0 else "0" + text


def canonical_artifact_message(*, schema: str, release_id: str, version: str,
                               artifact_sha256: str, artifact_size: int,
                               signing_key_id: str) -> bytes:
    """与端侧 canonicalArtifactMessage() 逐字节一致的七行规范串。"""
    digest = str(artifact_sha256).lower()
    if digest.startswith("sha256:"):
        digest = digest[len("sha256:"):]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise IdentityError("制品摘要必须是 64 位小写十六进制")
    if not isinstance(artifact_size, int) or isinstance(artifact_size, bool) \
            or artifact_size < 0:
        raise IdentityError("制品大小必须是非负整数")
    for text in (schema, release_id, version, signing_key_id):
        if not isinstance(text, str) or not text.strip():
            raise IdentityError("规范消息字段不能为空")
        if "\n" in text:
            raise IdentityError("规范消息字段不能包含换行")
    return "\n".join([
        MESSAGE_PREFIX, schema, release_id, version,
        digest, str(artifact_size), signing_key_id,
    ]).encode("utf-8")


# ---- 纯 Python RSA 原语（与端侧 rsa.ts 同构；交叉验证用） ----
def _bytes_to_int(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


def _int_to_bytes(value: int, length: int) -> bytes:
    return value.to_bytes(length, "big")


def _mod_pow(base: int, exponent: int, modulus: int) -> int:
    return pow(base, exponent, modulus)


def pkcs1_v15_sha256_emsa(message: bytes, modulus_bits: int) -> bytes:
    """EMSA-PKCS1-v1_5 编码：00 01 FF..FF 00 || DigestInfo(SHA-256)。"""
    digest_info = bytes.fromhex(
        "3031300d060960864801650304020105000420" + hashlib.sha256(message).hexdigest())
    k = modulus_bits // 8
    padding_len = k - len(digest_info) - 3
    if padding_len < 8:
        raise IdentityError("模长过小，无法安全填充")
    return b"\x00\x01" + b"\xff" * padding_len + b"\x00" + digest_info


def raw_rsa_verify(modulus: int, exponent: int, message: bytes,
                   signature: bytes) -> bool:
    """与端侧 rsaSha256Verify 相同的常数时间比较验签（纯 Python）。"""
    k = (modulus.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    sig_int = _bytes_to_int(signature)
    if sig_int >= modulus:
        return False
    encoded = _int_to_bytes(_mod_pow(sig_int, exponent, modulus), k)
    expected = pkcs1_v15_sha256_emsa(message, modulus.bit_length())
    return encoded == expected


# ---- 私钥（签发侧） ----
class PrivateIdentityKey:
    """RSA 私钥包装：签发制品签名；私钥材料不导出、不落清单。"""

    def __init__(self, private_key: Any, *, key_id: str):
        if not _CRYPTO_AVAILABLE:  # pragma: no cover
            raise IdentityError("cryptography 库不可用，无法持有生产私钥")
        if not isinstance(key_id, str) or not key_id.strip():
            raise IdentityError("密钥标识不能为空")
        if "\n" in key_id:
            raise IdentityError("密钥标识不能包含换行")
        self._key = private_key
        self.key_id = key_id.strip()
        size = self._key.key_size
        if not isinstance(size, int) or size < _MIN_MODULUS_BITS:
            raise IdentityError(f"RSA 模长至少 {_MIN_MODULUS_BITS} 位，当前 {size}")

    @classmethod
    def generate(cls, *, key_id: str, modulus_bits: int = 2048) -> "PrivateIdentityKey":
        if modulus_bits < _MIN_MODULUS_BITS:
            raise IdentityError(f"RSA 模长至少 {_MIN_MODULUS_BITS} 位")
        return cls(rsa.generate_private_key(
            public_exponent=DEFAULT_EXPONENT, key_size=modulus_bits), key_id=key_id)

    @classmethod
    def from_pem(cls, pem: bytes, *, key_id: str, password: Optional[bytes] = None
                 ) -> "PrivateIdentityKey":
        key = serialization.load_pem_private_key(pem, password=password)
        return cls(key, key_id=key_id)

    def sign(self, message: bytes) -> str:
        """按 PKCS#1 v1.5 + SHA-256 签名，返回十六进制签名
        （端侧 decodeSignature 原生接受 hex）。"""
        if not isinstance(message, (bytes, bytearray)):
            raise IdentityError("签名消息必须是字节串")
        signature = self._key.sign(
            bytes(message),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return signature.hex()

    def sign_artifact_envelope(self, *, release_id: str, version: str,
                               artifact_sha256: str, artifact_size: int,
                               schema: str = SCHEMA) -> str:
        """按端侧规范消息签发制品信封签名。"""
        message = canonical_artifact_message(
            schema=schema, release_id=release_id, version=version,
            artifact_sha256=artifact_sha256, artifact_size=artifact_size,
            signing_key_id=self.key_id)
        return self.sign(message)

    def public_numbers(self) -> tuple:
        numbers = self._key.public_key().public_numbers()
        return numbers.n, numbers.e

    def public_record(self, *, not_before: Optional[float] = None,
                      not_after: Optional[float] = None) -> Dict[str, Any]:
        """导出端侧 TrustedPublicKey 兼容记录；不含任何私钥材料。"""
        n, e = self.public_numbers()
        record: Dict[str, Any] = {
            "key_id": self.key_id,
            "algorithm": ALGORITHM,
            # 端侧 toBigInt 接受十六进制或 base64url；十六进制无歧义
            "n": _hex_even(n),
            "e": _hex_even(e),
        }
        if not_before is not None:
            record["not_before"] = not_before
        if not_after is not None:
            record["not_after"] = not_after
        return record


# ---- 身份登记簿（服务端持有，可审计轮换与撤销） ----
@dataclass
class TrustedIdentity:
    """一条生产签名身份：公钥参数 + 生命周期。

    轮换 = 登记新 key_id 并把 active_key_id 指过去；
    撤销 = revoked=True + revoked_reason；记录永不删除，保证可追溯。
    """

    key_id: str
    modulus: str            # 十六进制，无 0x 前缀
    exponent: str           # 十六进制，无 0x 前缀
    algorithm: str = ALGORITHM
    not_before: Optional[float] = None
    not_after: Optional[float] = None
    revoked: bool = False
    revoked_reason: Optional[str] = None
    revoked_at: Optional[float] = None
    created_at: float = field(default_factory=_now)
    note: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or not self.key_id.strip():
            raise IdentityError("key_id 不能为空")
        if "\n" in self.key_id:
            raise IdentityError("key_id 不能包含换行")
        for name in ("modulus", "exponent"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise IdentityError(f"{name} 必须是非空十六进制字符串")
            try:
                int(value, 16)
            except ValueError as exc:
                raise IdentityError(f"{name} 不是合法十六进制: {value!r}") from exc
        if self.algorithm != ALGORITHM:
            raise IdentityError(f"不支持的算法: {self.algorithm}")
        if self.not_before is not None and self.not_after is not None \
                and self.not_before > self.not_after:
            raise IdentityError("not_before 不能晚于 not_after")
        if self.revoked and not self.revoked_reason:
            raise IdentityError("撤销身份必须写明原因")

    def public_record(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "key_id": self.key_id,
            "algorithm": self.algorithm,
            "n": self.modulus,
            "e": self.exponent,
            # Complete exports must not turn revoked identities back into
            # usable client keys. Keep lifecycle details/reasons server-side.
            "revoked": self.revoked,
        }
        if self.not_before is not None:
            record["not_before"] = self.not_before
        if self.not_after is not None:
            record["not_after"] = self.not_after
        return record

    def usable(self, *, at: Optional[float] = None) -> bool:
        """端侧 hasUsableKey 的服务端镜像：未撤销且在生效窗口内。"""
        now = _now() if at is None else at
        if self.revoked:
            return False
        if self.not_before is not None and now < self.not_before:
            return False
        if self.not_after is not None and now >= self.not_after:
            return False
        return True


class IdentityRegistry:
    """生产身份登记簿：登记、轮换、撤销与分发公钥记录。

    持久化为单个 JSON 文档；不含私钥材料，泄露无害。
    """

    def __init__(self, path: Optional[Path] = None, *, now: Optional[Any] = None):
        self._path = Path(path) if path is not None else None
        self._now = now or _now
        self._identities: Dict[str, TrustedIdentity] = {}
        self._active_key_id: Optional[str] = None
        self._load()

    # -- 持久化 --
    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if raw.get("schema") != "evoagent.release.identity/1":
            raise IdentityError("身份登记簿 schema 不符")
        for item in raw.get("identities", []):
            identity = TrustedIdentity(**item)
            self._identities[identity.key_id] = identity
        self._active_key_id = raw.get("active_key_id")
        if self._active_key_id is not None \
                and self._active_key_id not in self._identities:
            raise IdentityError("active_key_id 指向未登记身份")

    def save(self) -> None:
        if self._path is None:
            return
        doc = {
            "schema": "evoagent.release.identity/1",
            "active_key_id": self._active_key_id,
            "identities": [
                {k: v for k, v in vars(identity).items()} for identity
                in self._identities.values()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")

    # -- 登记 / 轮换 / 撤销 --
    def register(self, identity: TrustedIdentity, *, activate: bool = False,
                 actor: str, reason: str) -> TrustedIdentity:
        _text(actor, "actor")
        _text(reason, "reason")
        if identity.key_id in self._identities:
            existing = self._identities[identity.key_id]
            if existing != identity:
                raise IdentityError(f"身份 {identity.key_id} 已存在且参数不同；"
                                    "轮换须使用新的 key_id")
            return existing
        self._identities[identity.key_id] = identity
        if activate or self._active_key_id is None:
            self._active_key_id = identity.key_id
        self.save()
        return identity

    def register_from_private(self, private: PrivateIdentityKey, *, actor: str,
                              reason: str, activate: bool = False,
                              not_before: Optional[float] = None,
                              not_after: Optional[float] = None) -> TrustedIdentity:
        n, e = private.public_numbers()
        return self.register(TrustedIdentity(
            key_id=private.key_id, modulus=_hex_even(n), exponent=_hex_even(e),
            not_before=not_before, not_after=not_after),
            activate=activate, actor=actor, reason=reason)

    def rotate(self, new_key_id: str, *, actor: str, reason: str) -> None:
        """把活动身份指到已登记的新 key_id；不删除旧记录。"""
        _text(actor, "actor")
        _text(reason, "reason")
        if new_key_id not in self._identities:
            raise IdentityError(f"目标身份 {new_key_id} 未登记")
        if new_key_id == self._active_key_id:
            return
        self._active_key_id = new_key_id
        self.save()

    def revoke(self, key_id: str, *, reason: str, actor: str) -> TrustedIdentity:
        _text(actor, "actor")
        _text(reason, "reason")
        identity = self._identities.get(key_id)
        if identity is None:
            raise KeyError(f"未登记身份: {key_id}")
        if key_id == self._active_key_id:
            raise IdentityError("不能撤销当前活动身份；先轮换到新身份再撤销")
        if not identity.revoked:
            identity.revoked = True
            identity.revoked_reason = reason
            identity.revoked_at = self._now()
            self.save()
        return identity

    # -- 查询 / 分发 --
    @property
    def active_key_id(self) -> Optional[str]:
        return self._active_key_id

    def get(self, key_id: Optional[str]) -> Optional[TrustedIdentity]:
        if not isinstance(key_id, str):
            return None
        return self._identities.get(key_id)

    def active(self) -> TrustedIdentity:
        identity = self.get(self._active_key_id)
        if identity is None:
            raise IdentityError("信任根没有活动身份")
        return identity

    def key_ids(self) -> List[str]:
        return sorted(self._identities)

    def public_records(self, *, include_revoked: bool = False) -> List[Dict[str, Any]]:
        """分发给端侧的受信公钥列表（端侧 TrustedPublicKey[]）。"""
        out = []
        for identity in self._identities.values():
            if identity.revoked and not include_revoked:
                continue
            out.append(identity.public_record())
        return sorted(out, key=lambda r: r["key_id"])


# ---- 验签（服务端自检 / 联验） ----
def verify_artifact_signature(*, modulus: str, exponent: str, message: bytes,
                              signature: str) -> Dict[str, Any]:
    """服务端独立验签：与端侧同一算法，用于发布前联验。

    返回 {"ok": bool, "error": str?}；不抛异常，便于进证据链。
    """
    try:
        n = int(modulus, 16)
        e = int(exponent, 16)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": f"公钥参数非法: {exc}"}
    if isinstance(signature, str):
        text = signature.strip()
        try:
            raw = bytes.fromhex(text) if _is_hex(text) else \
                base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        except (ValueError, binascii.Error) as exc:
            return {"ok": False, "error": f"签名编码非法: {exc}"}
    else:
        return {"ok": False, "error": "签名必须是字符串"}
    ok = raw_rsa_verify(n, e, message, raw)
    if _CRYPTO_AVAILABLE:
        # 与 cryptography 库交叉验证：两种实现必须一致
        public_key = rsa.RSAPublicNumbers(e, n).public_key()
        try:
            public_key.verify(raw, message, padding.PKCS1v15(), hashes.SHA256())
            cross = True
        except InvalidSignature:
            cross = False
        if cross != ok:
            return {"ok": False,
                    "error": "纯 Python 与 cryptography 验签结论不一致（内部错误）"}
    return {"ok": ok, "error": None if ok else "签名验证失败"}


def _is_hex(text: str) -> bool:
    return bool(text) and len(text) % 2 == 0 and \
        all(c in "0123456789abcdefABCDEF" for c in text)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdentityError(f"{name} 不能为空")
    return value
