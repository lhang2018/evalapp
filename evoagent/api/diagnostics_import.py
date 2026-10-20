"""离线诊断包受控导入（补充 C §8）。

导入只作为证据：不执行包内任何命令、脚本或工具请求。校验包大小、
路径、格式版本、摘要与关联关系后写入隔离区，供人工审查。
"""

import hashlib
import io
import json
import re
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from .events_sync import EVENT_ID_RE, MANIFEST_ID_RE

MAX_PACKAGE_BYTES = 20 * 1024 * 1024
MAX_ENTRIES = 200
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ENTRY_BYTES = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
RATIO_CHECK_MIN_BYTES = 1024 * 1024
MAX_EVENTS = 5000
MAX_EVENT_PAYLOAD_BYTES = 256 * 1024
FORMAT_VERSION = "1.0"

ALLOWED_FILES = {
    "manifest.json",
    "device-info.json",
    "runtime-manifests.jsonl",
    "task-runs.jsonl",
    "events.jsonl",
    "sync-status.json",
    "evaluation-results.jsonl",
    "errors/error-details.jsonl",
    "checksums.sha256",
}
ATTACHMENT_EXTS = {".png", ".jpg", ".jpeg", ".txt", ".log", ".json", ".jsonl"}
CHECKSUM_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")


class PackageError(ValueError):
    """整包校验失败（400/413）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _safe_path(name: str) -> str:
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        raise PackageError(f"非法路径: {name}")
    trimmed = name.rstrip("/")
    parts = trimmed.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PackageError(f"非法路径: {name}")
    if "/".join(parts) != trimmed:
        raise PackageError(f"非法路径: {name}")
    return trimmed


def _allowed(name: str) -> bool:
    if name in ALLOWED_FILES:
        return True
    if not name.startswith("attachments/"):
        return False
    if name.count("/") != 1:
        return False
    dot = name.rfind(".")
    return dot > len("attachments/") and name[dot:].lower() in ATTACHMENT_EXTS


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    with zf.open(info) as stream:
        data = stream.read(MAX_ENTRY_BYTES + 1)
    if len(data) > MAX_ENTRY_BYTES:
        raise PackageError(f"文件超过 20 MiB: {info.filename}", 413)
    return data


def _parse_checksums(raw: bytes, files: Dict[str, bytes]) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PackageError("checksums.sha256 不是 UTF-8 文本")
    listed = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = CHECKSUM_RE.match(line)
        if not match:
            raise PackageError(f"摘要行格式非法: {line[:80]}")
        digest, path = match.group(1).lower(), _safe_path(match.group(2))
        if path in listed:
            raise PackageError(f"摘要重复: {path}")
        listed.add(path)
        if path not in files:
            raise PackageError(f"摘要引用了不存在的文件: {path}")
        actual = hashlib.sha256(files[path]).hexdigest()
        if actual != digest:
            raise PackageError(f"SHA-256 不匹配: {path}")
    missing = set(files) - {"checksums.sha256"} - listed
    if missing:
        raise PackageError(f"缺少摘要: {', '.join(sorted(missing))}")


def _parse_manifest(raw: bytes) -> Tuple[Dict[str, Any], List[str]]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise PackageError("manifest.json 不是合法 JSON")
    if not isinstance(manifest, dict):
        raise PackageError("manifest.json 必须是对象")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise PackageError(
            f"不支持的包格式版本: {manifest.get('format_version')}")
    exported_at = manifest.get("exported_at")
    if not isinstance(exported_at, (int, float)) or isinstance(exported_at, bool):
        raise PackageError("manifest.json 缺少 exported_at")
    if "filters" in manifest and not isinstance(manifest["filters"], dict):
        raise PackageError("manifest.filters 必须是对象")
    warnings: List[str] = []
    listed = manifest.get("files")
    if isinstance(listed, list):
        for name in listed:
            if not isinstance(name, str) or not _allowed(_safe_path(name)):
                warnings.append(f"清单包含非法文件名: {name}")
    elif listed is not None:
        raise PackageError("manifest.files 必须是数组")
    return manifest, warnings


def _parse_events(raw: bytes, db: Any) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PackageError("events.jsonl 不是 UTF-8 文本")
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if len(events) >= MAX_EVENTS:
            raise PackageError(f"事件数超过 {MAX_EVENTS} 条", 413)
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            raise PackageError(f"events.jsonl 第 {number} 行不是合法 JSON")
        if not isinstance(item, dict):
            raise PackageError(f"events.jsonl 第 {number} 行必须是对象")
        event_id = item.get("event_id")
        if not isinstance(event_id, str) or not EVENT_ID_RE.match(event_id):
            raise PackageError(f"events.jsonl 第 {number} 行 event_id 非法")
        event_type = item.get("event_type")
        if (not isinstance(event_type, str) or not event_type.strip()
                or len(event_type) > 64):
            raise PackageError(f"events.jsonl 第 {number} 行 event_type 非法")
        timestamp = item.get("timestamp")
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            raise PackageError(f"events.jsonl 第 {number} 行 timestamp 非法")
        sequence_no = item.get("sequence_no")
        if sequence_no is not None and (
                not isinstance(sequence_no, int) or isinstance(sequence_no, bool)
                or sequence_no < 0):
            raise PackageError(
                f"events.jsonl 第 {number} 行 sequence_no 非法")
        manifest_id = item.get("runtime_manifest_id")
        if manifest_id is not None and (
                not isinstance(manifest_id, str)
                or not MANIFEST_ID_RE.match(manifest_id)):
            raise PackageError(
                f"events.jsonl 第 {number} 行 runtime_manifest_id 非法")
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            raise PackageError(f"events.jsonl 第 {number} 行 payload 必须是对象")
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode()
        if len(encoded) > MAX_EVENT_PAYLOAD_BYTES:
            raise PackageError(f"events.jsonl 第 {number} 行 payload 过大", 413)

        task_id = item.get("task_id")
        run_id = item.get("run_id")
        review_status, reason = _review_links(
            task_id if isinstance(task_id, str) else None,
            run_id if isinstance(run_id, str) else None,
            manifest_id if isinstance(manifest_id, str) else None,
            db)
        events.append({
            "event_id": event_id,
            "event_type": event_type.strip(),
            "task_id": task_id if isinstance(task_id, str) else None,
            "run_id": run_id if isinstance(run_id, str) else None,
            "runtime_manifest_id": manifest_id,
            "timestamp": float(timestamp),
            "sequence_no": sequence_no,
            "severity": item.get("severity") if item.get("severity") in
            ("info", "warn", "error", "critical") else "info",
            "payload": payload,
            "review_status": review_status,
            "conflict_reason": reason,
        })
    return events


def _review_links(task_id: Optional[str], run_id: Optional[str],
                  manifest_id: Optional[str],
                  db: Any) -> Tuple[str, str]:
    if task_id is not None:
        if db is None or db.get_task(task_id) is None:
            return "needs_review", "unknown_task"
    if run_id is not None and manifest_id is None:
        return "needs_review", "missing_manifest"
    if manifest_id is not None:
        manifest = db.get_runtime_manifest(manifest_id) if db is not None else None
        if manifest is None:
            return "needs_review", "unknown_manifest"
        if task_id and manifest.get("task_id") != task_id:
            return "needs_review", "manifest_task_mismatch"
        if run_id and manifest.get("run_id") not in (None, run_id):
            return "needs_review", "manifest_run_mismatch"
    return "accepted", ""


def inspect_package(data: bytes, db: Any = None) -> Dict[str, Any]:
    """校验诊断包并解析为待入隔离区的记录；失败抛 PackageError。"""
    if not data:
        raise PackageError("导入包为空")
    if len(data) > MAX_PACKAGE_BYTES:
        raise PackageError("导入包超过 20 MiB", 413)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PackageError("不是合法的 ZIP 包")

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise PackageError(f"文件数超过 {MAX_ENTRIES}", 413)
        total = 0
        files: Dict[str, bytes] = {}
        for info in infos:
            name = _safe_path(info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise PackageError(f"不允许符号链接: {name}")
            if info.is_dir():
                continue
            if not _allowed(name):
                raise PackageError(f"不允许的文件: {name}")
            if name in files:
                raise PackageError(f"重复文件: {name}")
            size = info.file_size
            if size > MAX_ENTRY_BYTES:
                raise PackageError(f"文件超过 20 MiB: {name}", 413)
            if (size > RATIO_CHECK_MIN_BYTES
                    and size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO):
                raise PackageError(f"压缩比异常（疑似解压炸弹）: {name}", 413)
            content = _read_entry(zf, info)
            total += len(content)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise PackageError("解压后总大小超过 100 MiB", 413)
            files[name] = content

    for required in ("manifest.json", "events.jsonl", "checksums.sha256"):
        if required not in files:
            raise PackageError(f"缺少必需文件: {required}")

    manifest, warnings = _parse_manifest(files["manifest.json"])
    _parse_checksums(files["checksums.sha256"], files)
    events = _parse_events(files["events.jsonl"], db)
    declared = manifest.get("files")
    if isinstance(declared, list):
        actual = set(files) - {"manifest.json", "checksums.sha256"}
        declared_set = {name for name in declared if isinstance(name, str)}
        if declared_set != actual:
            warnings.append("manifest.files 与实际文件不一致")

    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "files": sorted(files),
        "manifest": manifest,
        "format_version": manifest["format_version"],
        "exported_at": float(manifest["exported_at"]),
        "warnings": warnings,
        "events": events,
    }
