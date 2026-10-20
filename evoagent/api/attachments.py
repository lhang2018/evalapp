"""诊断附件元数据校验（补充 C §6）。

附件属于高敏感材料：上传前必须显式授权；服务端只保存分块与摘要，
完整内容校验通过后才标记 complete。事件可以先引用尚未完成的附件。
"""

import math
import re
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Optional

MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MIN_CHUNK_BYTES = 16 * 1024
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_CHUNKS = 4096

ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class AttachmentError(ValueError):
    """带 HTTP 状态码的附件校验错误。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class AttachmentInit:
    attachment_id: str
    file_name: str
    content_type: str
    size_bytes: int
    sha256: str
    chunk_size: int
    chunk_count: int
    consent: bool


def _text(value: Any, name: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttachmentError(f"{name} 必须为非空字符串")
    cleaned = CONTROL_CHARS_RE.sub("", value).strip()
    if not cleaned:
        raise AttachmentError(f"{name} 不能为空")
    if len(cleaned) > limit:
        raise AttachmentError(f"{name} 超过 {limit} 字符")
    return cleaned


def sanitize_file_name(raw: Any) -> str:
    # 附件名只作展示，不允许路径成分
    value = str(raw or "").replace("\\", "/").split("/")[-1]
    value = CONTROL_CHARS_RE.sub("", value).strip()
    return value[:128] or "attachment.bin"


def validate_init(body: Any) -> AttachmentInit:
    if not isinstance(body, dict):
        raise AttachmentError("请求体必须是对象")
    attachment_id = body.get("attachment_id")
    if not isinstance(attachment_id, str) or \
            not ATTACHMENT_ID_RE.match(attachment_id):
        raise AttachmentError(
            "attachment_id 非法（16-64 位字母数字/下划线/短横线）")
    if body.get("consent") is not True:
        raise AttachmentError("附件属于高敏感材料，需要显式授权", status=403)

    size = body.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise AttachmentError("size_bytes 必须为正整数")
    if size > MAX_ATTACHMENT_BYTES:
        raise AttachmentError("附件超过 50 MiB", status=413)

    sha256 = body.get("sha256")
    if not isinstance(sha256, str) or not SHA256_RE.match(sha256):
        raise AttachmentError("sha256 必须为 64 位小写十六进制")

    chunk_size = body.get("chunk_size", 256 * 1024)
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or \
            chunk_size < MIN_CHUNK_BYTES or chunk_size > MAX_CHUNK_BYTES:
        raise AttachmentError(
            f"chunk_size 必须在 {MIN_CHUNK_BYTES}–{MAX_CHUNK_BYTES} 字节之间")
    chunk_count = math.ceil(size / chunk_size)
    if chunk_count > MAX_CHUNKS:
        raise AttachmentError("分块数量超过上限")

    content_type = body.get("content_type", "application/octet-stream")
    if not isinstance(content_type, str) or not content_type.strip():
        content_type = "application/octet-stream"
    content_type = content_type.strip()[:128]
    if CONTROL_CHARS_RE.search(content_type):
        raise AttachmentError("content_type 含非法字符")

    return AttachmentInit(
        attachment_id=attachment_id,
        file_name=sanitize_file_name(body.get("file_name")),
        content_type=content_type,
        size_bytes=size,
        sha256=sha256,
        chunk_size=chunk_size,
        chunk_count=chunk_count,
        consent=True,
    )


def validate_chunk(meta: Optional[Dict[str, Any]], index: int,
                   data: bytes) -> None:
    if meta is None:
        raise AttachmentError("附件不存在", status=404)
    if meta.get("status") == "complete":
        raise AttachmentError("附件已上传完成", status=409)
    chunk_count = int(meta.get("chunk_count") or 0)
    if not 0 <= index < chunk_count:
        raise AttachmentError(f"分块序号越界: {index}")
    if not data:
        raise AttachmentError("分块为空")
    expected = int(meta.get("chunk_size") or 0)
    last = index == chunk_count - 1
    if expected and len(data) > expected:
        raise AttachmentError("分块超过声明的 chunk_size", status=413)
    if last:
        remaining = int(meta.get("size_bytes") or 0) - expected * (chunk_count - 1)
        if len(data) != remaining:
            raise AttachmentError("最后一块大小与声明不符")
    elif expected and len(data) != expected:
        raise AttachmentError("分块大小与声明不符")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
