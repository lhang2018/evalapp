"""发布正文签名（ALN-03）。

规范化签名载荷使用自定界编码：字符串/列表/字典都带长度前缀，字典键排序，
因此同一语义载荷在任何进程、平台与 JSON 往返后都得到相同字节。签名覆盖
发布清单身份、完整兼容声明、逐文件制品摘要、通道、发布者密钥标识与时间。

服务端只持有对称签名密钥；下发给端侧的只有 `signed_by`、`key_id` 和签名
载荷，公钥信任与端侧验签由 Android 线另行实现。
"""

import hashlib
import hmac
import math
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional

SCHEMA = "evoagent.release.signing/1"
ALGORITHM = "hmac-sha256"
SIGNATURE_VERSION = "hmac-sha256-v2"
_HEX_RE = None


class SigningError(ValueError):
    """签名载荷或制品声明不合法。"""


def _encode(value: Any, out: List[bytes]) -> None:
    if value is None:
        out.append(b"n")
    elif isinstance(value, bool):
        out.append(b"t" if value else b"f")
    elif isinstance(value, int):
        out.append(b"i" + str(value).encode("ascii") + b";")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise SigningError("签名载荷不能包含 NaN/Infinity")
        out.append(b"d" + repr(value).encode("ascii") + b";")
    elif isinstance(value, str):
        raw = value.encode("utf-8")
        out.append(b"s" + str(len(raw)).encode("ascii") + b":" + raw)
    elif isinstance(value, (list, tuple)):
        out.append(b"l" + str(len(value)).encode("ascii") + b":")
        for item in value:
            _encode(item, out)
    elif isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda item: item[0])
        out.append(b"m" + str(len(items)).encode("ascii") + b":")
        for key, item in items:
            if not isinstance(key, str):
                raise SigningError("签名载荷的字典键必须是字符串")
            _encode(key, out)
            _encode(item, out)
    else:
        raise SigningError(f"签名载荷不支持类型: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    """自定界规范编码；任何歧义都会被长度前缀消除。"""
    out: List[bytes] = []
    _encode(value, out)
    return b"".join(out)


def payload_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---- 密钥 ----
@dataclass(frozen=True)
class SigningKey:
    key_id: str
    secret: str
    algorithm: str = ALGORITHM


class KeyRing:
    """可注入的密钥环；轮换后旧密钥保留，历史发布仍可验证。"""

    def __init__(self, keys: Optional[Iterable[SigningKey]] = None,
                 active_key_id: Optional[str] = None):
        self._keys: Dict[str, SigningKey] = {}
        self.active_key_id: Optional[str] = None
        for key in keys or ():
            self.add(key.key_id, key.secret, key.algorithm)
        if active_key_id is not None:
            self.set_active(active_key_id)

    def add(self, key_id: str, secret: str,
            algorithm: str = ALGORITHM) -> SigningKey:
        if not isinstance(key_id, str) or not key_id.strip():
            raise SigningError("密钥标识不能为空")
        if not isinstance(secret, str) or not secret:
            raise SigningError("签名密钥不能为空")
        if algorithm != ALGORITHM:
            raise SigningError(f"不支持的签名算法: {algorithm}")
        key = SigningKey(key_id.strip(), secret, algorithm)
        self._keys[key.key_id] = key
        if self.active_key_id is None:
            self.active_key_id = key.key_id
        return key

    def set_active(self, key_id: str) -> None:
        if key_id not in self._keys:
            raise KeyError(f"未知签名密钥标识: {key_id}")
        self.active_key_id = key_id

    def remove(self, key_id: str) -> None:
        if key_id == self.active_key_id:
            raise ValueError("不能移除当前启用密钥")
        self._keys.pop(key_id, None)

    def get(self, key_id: Optional[str]) -> Optional[SigningKey]:
        if not isinstance(key_id, str):
            return None
        return self._keys.get(key_id)

    @property
    def active(self) -> SigningKey:
        key = self.get(self.active_key_id)
        if key is None:
            raise SigningError("密钥环没有启用密钥")
        return key

    def key_ids(self) -> List[str]:
        return sorted(self._keys)


def build_payload(metadata: Mapping[str, Any], *,
                  key_id: str) -> Dict[str, Any]:
    """把清单元数据（含逐文件摘要）冻结成待签名载荷。"""
    if not isinstance(metadata, Mapping):
        raise SigningError("签名元数据必须是对象")
    return {
        "schema": SCHEMA,
        "algorithm": ALGORITHM,
        "key_id": key_id,
        "manifest_digest": hashlib.sha256(
            canonical_bytes(dict(metadata))).hexdigest(),
        "manifest": dict(metadata),
    }


def sign_payload(payload: Mapping[str, Any], secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical_bytes(payload),
                    hashlib.sha256).hexdigest()


def verify_signature(payload: Mapping[str, Any], signature: Any,
                     keyring: KeyRing) -> Dict[str, Any]:
    key_id = payload.get("key_id") if isinstance(payload, Mapping) else None
    key = keyring.get(key_id)
    if key is None:
        return {"ok": False, "key_id": key_id,
                "error": f"未知签名密钥标识: {key_id!r}"}
    if payload.get("algorithm") != key.algorithm:
        return {"ok": False, "key_id": key_id,
                "error": f"签名算法不匹配: {payload.get('algorithm')!r}"}
    if not isinstance(signature, str) or not signature:
        return {"ok": False, "key_id": key_id, "error": "签名缺失"}
    expected = sign_payload(payload, key.secret)
    if not hmac.compare_digest(expected, signature):
        return {"ok": False, "key_id": key_id, "error": "签名不匹配"}
    return {"ok": True, "key_id": key_id}


# ---- 制品文件 ----
def _safe_relative(path: str) -> PurePosixPath:
    if not isinstance(path, str) or not path.strip():
        raise SigningError("制品路径不能为空")
    if "\\" in path:
        raise SigningError(f"制品路径必须使用 POSIX 分隔符: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise SigningError(f"非法制品路径: {path!r}")
    return pure


def _resolve_under(root: Path, raw: str) -> Path:
    pure = _safe_relative(raw)
    root = root.resolve()
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SigningError(f"制品路径越界: {raw!r}") from exc
    return candidate


def collect_artifact_files(root: Path, paths: Iterable[str]
                           ) -> List[Dict[str, Any]]:
    """按真实文件字节生成冻结清单；路径相对制品包根目录。"""
    root = Path(root)
    collected: Dict[str, Dict[str, Any]] = {}
    for raw in paths:
        pure = _safe_relative(raw)
        path = _resolve_under(root, raw)
        key = pure.as_posix()
        if key in collected:
            raise SigningError(f"重复声明制品文件: {key}")
        if path.is_symlink() or not path.is_file():
            raise SigningError(f"制品文件不存在或不是普通文件: {key}")
        collected[key] = {"path": key, "size": path.stat().st_size,
                          "sha256": sha256_file(path)}
    return [collected[key] for key in sorted(collected)]


def verify_artifact_files(root: Path, declared: Any, *,
                          check_extra: bool = True) -> Dict[str, Any]:
    """按实际文件重算逐文件摘要；缺文件、多文件、摘要/大小不符都报错。"""
    errors: List[Dict[str, str]] = []
    verified: List[Dict[str, Any]] = []
    if not isinstance(declared, (list, tuple)):
        return {"ok": False, "verified": [],
                "errors": [{"path": "-", "error": "制品清单必须是数组"}]}
    root = Path(root)
    seen: Dict[str, Dict[str, Any]] = {}
    for entry in declared:
        if not isinstance(entry, Mapping):
            errors.append({"path": repr(entry), "error": "制品条目必须是对象"})
            continue
        raw = entry.get("path")
        try:
            pure = _safe_relative(raw)
            key = pure.as_posix()
        except SigningError as exc:
            errors.append({"path": str(raw), "error": str(exc)})
            continue
        size = entry.get("size")
        digest = entry.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            errors.append({"path": key, "error": "文件大小必须是非负整数"})
            continue
        if (not isinstance(digest, str) or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)):
            errors.append({"path": key, "error": "文件摘要必须是 64 位小写十六进制"})
            continue
        if key in seen:
            errors.append({"path": key, "error": "重复声明制品文件"})
            continue
        seen[key] = entry
        path = _resolve_under(root, key)
        if path.is_symlink() or not path.is_file():
            errors.append({"path": key, "error": "制品文件缺失"})
            continue
        actual_size = path.stat().st_size
        if actual_size != size:
            errors.append({"path": key,
                           "error": f"文件大小不符: 声明 {size}，实际 {actual_size}"})
            continue
        actual_digest = sha256_file(path)
        if not hmac.compare_digest(actual_digest, digest):
            errors.append({"path": key, "error": "文件摘要不符"})
            continue
        verified.append({"path": key, "size": size, "sha256": actual_digest})

    if check_extra and root.is_dir():
        declared_paths = set(seen)
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            key = path.relative_to(root).as_posix()
            if key not in declared_paths:
                errors.append({"path": key, "error": "未声明的多余文件"})
    return {"ok": not errors, "verified": verified, "errors": errors}
