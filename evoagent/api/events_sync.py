"""客户端事件批次校验（PRD 73/75，Arch 9.1）。

服务端只接收 standard 隐私级别的事件；校验失败按事件粒度返回，
批次级错误（结构、条数）直接拒绝整批。
"""

import json
import re
import time
from typing import Any, Dict, List, Tuple

from ..core.events import RunEvent
from ..core.privacy import PRIVACY_LEVELS, is_uploadable

MAX_BATCH = 200
MAX_PAYLOAD_BYTES = 16 * 1024
MAX_CLOCK_SKEW_SECONDS = 7 * 24 * 3600
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_ATTACHMENT_REFS = 8
MANIFEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
SEVERITIES = {"info", "warn", "error", "critical"}
CLOCK_SOURCES = {"device", "server"}
# 回执协议版本：客户端按版本解释 confirmed/unconfirmed 语义
SYNC_PROTOCOL_VERSION = "1.0"


class BatchError(ValueError):
    """批次结构错误（整批 400）。"""


def _optional_str(raw: Dict[str, Any], key: str, limit: int = 128):
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    if len(value) > limit:
        raise ValueError(f"{key} 超过 {limit} 字符")
    return value


def _one(raw: Any, now: float) -> RunEvent:
    if not isinstance(raw, dict):
        raise ValueError("事件必须是对象")
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not EVENT_ID_RE.match(event_id):
        raise ValueError("event_id 非法（16-64 位字母数字/下划线/短横线）")
    event_type = raw.get("event_type")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("event_type 不能为空")
    if len(event_type) > 64:
        raise ValueError("event_type 超过 64 字符")

    ts = raw.get("timestamp")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        raise ValueError("timestamp 必须是数字")
    if abs(now - float(ts)) > MAX_CLOCK_SKEW_SECONDS:
        raise ValueError("timestamp 超出 ±7 天时钟窗口")

    privacy = raw.get("privacy_classification", "standard")
    if privacy not in PRIVACY_LEVELS:
        raise ValueError(f"未知隐私级别: {privacy}")
    if not is_uploadable(privacy):
        raise ValueError(f"服务端不接收 {privacy} 事件")

    payload = raw.get("payload", {})
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")
    try:
        encoded = json.dumps(payload, ensure_ascii=False,
                             default=str).encode("utf-8")
    except Exception as exc:
        raise ValueError(f"payload 无法序列化: {exc}")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload 超过 16 KiB")

    severity = raw.get("severity", "info")
    if severity not in SEVERITIES:
        raise ValueError(f"未知 severity: {severity}")

    sequence_no = raw.get("sequence_no")
    if sequence_no is not None:
        if (not isinstance(sequence_no, int) or isinstance(sequence_no, bool)
                or sequence_no < 0 or sequence_no > 2 ** 31 - 1):
            raise ValueError("sequence_no 必须是非负整数")

    runtime_manifest_id = _optional_str(raw, "runtime_manifest_id")
    if runtime_manifest_id is not None and \
            not MANIFEST_ID_RE.match(runtime_manifest_id):
        raise ValueError("runtime_manifest_id 非法（8-128 位标识符）")

    run_id = _optional_str(raw, "run_id")

    clock_source = raw.get("clock_source", "device")
    if clock_source not in CLOCK_SOURCES:
        raise ValueError(f"未知 clock_source: {clock_source}")

    refs_raw = raw.get("attachment_refs", [])
    if refs_raw is None:
        refs_raw = []
    if not isinstance(refs_raw, list):
        raise ValueError("attachment_refs 必须是数组")
    if len(refs_raw) > MAX_ATTACHMENT_REFS:
        raise ValueError(f"attachment_refs 最多 {MAX_ATTACHMENT_REFS} 项")
    refs: List[str] = []
    for ref in refs_raw:
        if not isinstance(ref, str) or not ATTACHMENT_ID_RE.match(ref):
            raise ValueError("attachment_refs 含非法 ID")
        if ref not in refs:
            refs.append(ref)

    return RunEvent(
        event_id=event_id,
        event_type=event_type.strip(),
        task_id=_optional_str(raw, "task_id"),
        run_id=_optional_str(raw, "run_id"),
        component_id=_optional_str(raw, "component_id") or "client",
        component_version=_optional_str(raw, "component_version") or "0.1.0",
        timestamp=float(ts),
        severity=severity,
        correlation_id=_optional_str(raw, "correlation_id"),
        causation_id=_optional_str(raw, "causation_id"),
        payload=payload,
        privacy_classification="standard",
        sync_status="synced",
        sequence_no=sequence_no,
        runtime_manifest_id=runtime_manifest_id,
        clock_source=clock_source,
        attachment_refs=refs,
    )


def validate_batch(body: Any,
                   now: float = None) -> Tuple[List[RunEvent], List[Dict[str, str]]]:
    """返回 (合法事件, 逐事件错误)。结构错误抛 BatchError。"""
    now = time.time() if now is None else now
    if not isinstance(body, dict):
        raise BatchError("请求体必须是 JSON 对象")
    events = body.get("events")
    if not isinstance(events, list):
        raise BatchError("events 必须是数组")
    if not events:
        raise BatchError("events 不能为空")
    if len(events) > MAX_BATCH:
        raise BatchError(f"单批最多 {MAX_BATCH} 条事件")
    ok: List[RunEvent] = []
    errors: List[Dict[str, str]] = []
    for index, raw in enumerate(events):
        try:
            ok.append(_one(raw, now))
        except ValueError as exc:
            eid = ""
            if isinstance(raw, dict) and isinstance(raw.get("event_id"), str):
                eid = raw["event_id"][:64]
            errors.append({"event_id": eid or f"index:{index}",
                           "error": str(exc)})
    return ok, errors
