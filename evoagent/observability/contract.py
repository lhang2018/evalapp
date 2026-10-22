"""跨端观测事件契约（ALN-16）。

统一设备/服务端事件的来源、信任、隐私与故障口径，并保留
task/run/node_run/effect/approval/diagnostic/candidate/release 引用。
原始日志不经校验不能升级为结论或可信证据。
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .credentials import CredentialLeakError, assert_context_safe, scrub_value

SCHEMA_VERSION = "evoagent.observability.event/1"
PRIVACY_LEVELS = ("public", "internal", "sensitive", "restricted")
FAULT_LEVELS = ("none", "transient", "degraded", "fatal")
ORIGINS = ("device", "server")
TRUST_LEVELS = ("trusted", "untrusted")

_LEGACY_PRIVACY_MAP = {
    "standard": "internal",
    "sensitive": "sensitive",
    "secret": "restricted",
    "private": "restricted",
}


class EvidenceContractError(ValueError):
    pass


def to_four_level(classification: str) -> str:
    if classification in PRIVACY_LEVELS:
        return classification
    return _LEGACY_PRIVACY_MAP.get(classification, "restricted")


def infer_fault_level(event_type: str, severity: str,
                      payload: Optional[Dict[str, Any]] = None) -> str:
    payload = payload or {}
    name = str(event_type or "").lower()
    if severity == "critical" or "fatal" in name or "panic" in name:
        return "fatal"
    if severity == "error":
        return "degraded"
    if severity in ("warn", "warning"):
        return "transient"
    if payload.get("state") == "unknown" or "unknown" in name:
        return "degraded"
    if payload.get("retryable") is True:
        return "transient"
    return "none"


def canonical_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EventRefs:
    task_id: str = ""
    run_id: str = ""
    node_run_id: str = ""
    effect_id: str = ""
    approval_id: str = ""
    diagnostic_id: str = ""
    candidate_id: str = ""
    release_id: str = ""
    trace_id: str = ""
    audit_id: str = ""

    FIELDS = ("task_id", "run_id", "node_run_id", "effect_id", "approval_id",
              "diagnostic_id", "candidate_id", "release_id", "trace_id",
              "audit_id")

    def to_dict(self) -> Dict[str, str]:
        return {name: getattr(self, name) for name in self.FIELDS
                if getattr(self, name)}

    def merge(self, other: "EventRefs") -> "EventRefs":
        values = {}
        for name in self.FIELDS:
            values[name] = getattr(self, name) or getattr(other, name)
        return EventRefs(**values)


@dataclass
class EvidenceEvent:
    event_id: str = field(default_factory=lambda: "obs_" + uuid.uuid4().hex)
    event_type: str = "info"
    timestamp: float = 0.0
    schema_version: str = SCHEMA_VERSION
    origin: str = "server"
    trust: str = "trusted"
    privacy_level: str = "internal"
    fault_level: str = "none"
    severity: str = "info"
    component_id: str = "system"
    component_version: str = "0.1.0"
    refs: EventRefs = field(default_factory=EventRefs)
    payload: Dict[str, Any] = field(default_factory=dict)
    payload_ref: Optional[str] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    sequence_no: Optional[int] = None
    untrusted_origin: str = ""
    redacted: bool = False

    def validate(self) -> None:
        if self.privacy_level not in PRIVACY_LEVELS:
            raise EvidenceContractError(f"非法隐私等级: {self.privacy_level}")
        if self.fault_level not in FAULT_LEVELS:
            raise EvidenceContractError(f"非法故障等级: {self.fault_level}")
        if self.origin not in ORIGINS:
            raise EvidenceContractError(f"非法来源: {self.origin}")
        if self.trust not in TRUST_LEVELS:
            raise EvidenceContractError(f"非法信任等级: {self.trust}")
        if self.trust == "untrusted" and not self.untrusted_origin:
            raise EvidenceContractError("不可信内容必须保留原始来源")
        if self.privacy_level == "restricted":
            if self.payload and not self.redacted:
                raise EvidenceContractError(
                    "restricted 事件只能携带引用或已脱敏内容")
        try:
            assert_context_safe(self.payload)
        except CredentialLeakError as error:
            raise EvidenceContractError(str(error)) from error

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "origin": self.origin,
            "trust": self.trust,
            "privacy_level": self.privacy_level,
            "fault_level": self.fault_level,
            "severity": self.severity,
            "component_id": self.component_id,
            "component_version": self.component_version,
            "refs": self.refs.to_dict(),
            "payload": scrub_value(self.payload),
            "payload_ref": self.payload_ref,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "sequence_no": self.sequence_no,
            "untrusted_origin": self.untrusted_origin,
            "redacted": self.redacted,
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EvidenceEvent":
        refs_raw = raw.get("refs") if isinstance(raw.get("refs"), dict) else {}
        refs = EventRefs(**{name: str(refs_raw.get(name) or raw.get(name) or "")
                            for name in EventRefs.FIELDS})
        event = cls(
            event_id=str(raw.get("event_id") or ("obs_" + uuid.uuid4().hex)),
            event_type=str(raw.get("event_type") or "info"),
            timestamp=float(raw.get("timestamp") or 0.0),
            schema_version=str(raw.get("schema_version") or SCHEMA_VERSION),
            origin=str(raw.get("origin") or "server"),
            trust=str(raw.get("trust") or "trusted"),
            privacy_level=to_four_level(
                str(raw.get("privacy_level") or raw.get("privacy_classification")
                    or "standard")),
            fault_level=str(raw.get("fault_level")
                            or infer_fault_level(
                                raw.get("event_type", ""),
                                raw.get("severity", "info"),
                                raw.get("payload") if isinstance(
                                    raw.get("payload"), dict) else {})),
            severity=str(raw.get("severity") or "info"),
            component_id=str(raw.get("component_id") or "system"),
            component_version=str(raw.get("component_version") or "0.1.0"),
            refs=refs,
            payload=raw.get("payload") if isinstance(raw.get("payload"), dict)
            else {},
            payload_ref=raw.get("payload_ref"),
            correlation_id=raw.get("correlation_id"),
            causation_id=raw.get("causation_id"),
            sequence_no=raw.get("sequence_no"),
            untrusted_origin=str(raw.get("untrusted_origin") or ""),
            redacted=bool(raw.get("redacted")),
        )
        event.validate()
        return event

    @classmethod
    def from_run_event(cls, event: Any,
                       refs: Optional[EventRefs] = None) -> "EvidenceEvent":
        payload = event.payload if isinstance(event.payload, dict) else {}
        raw_refs = getattr(event, "refs", None)
        merged = EventRefs(
            task_id=str(getattr(event, "task_id", "") or ""),
            run_id=str(getattr(event, "run_id", "") or ""),
            node_run_id=str(getattr(event, "node_run_id", "") or ""),
            effect_id=str(getattr(event, "effect_id", "") or ""),
            approval_id=str(getattr(event, "approval_id", "") or ""),
            candidate_id=str(getattr(event, "candidate_id", "") or ""),
            release_id=str(getattr(event, "release_id", "") or ""),
        )
        if isinstance(raw_refs, dict):
            merged = merged.merge(EventRefs(**{
                name: str(raw_refs.get(name) or "")
                for name in EventRefs.FIELDS
                if name in raw_refs}))
        if refs is not None:
            merged = merged.merge(refs)
        privacy = str(getattr(event, "privacy_level", "") or
                      to_four_level(str(getattr(
                          event, "privacy_classification", "standard"))))
        fault = getattr(event, "fault_level", None) or infer_fault_level(
            str(getattr(event, "event_type", "")),
            str(getattr(event, "severity", "info")), payload)
        return cls(
            event_id=str(event.event_id),
            event_type=str(getattr(event, "event_type", "info")),
            timestamp=float(getattr(event, "timestamp", 0.0) or 0.0),
            schema_version=str(getattr(event, "schema_version", "1.0")),
            origin=str(getattr(event, "origin", "server") or "server"),
            trust=str(getattr(event, "trust", "trusted") or "trusted"),
            privacy_level=privacy,
            fault_level=str(fault),
            severity=str(getattr(event, "severity", "info")),
            component_id=str(getattr(event, "component_id", "system")),
            component_version=str(getattr(event, "component_version", "0.1.0")),
            refs=merged,
            payload=payload,
            payload_ref=getattr(event, "payload_ref", None),
            correlation_id=getattr(event, "correlation_id", None),
            causation_id=getattr(event, "causation_id", None),
            sequence_no=getattr(event, "sequence_no", None),
            untrusted_origin=str(getattr(event, "untrusted_origin", "") or ""),
            redacted=bool(getattr(event, "redacted", False)),
        )


def as_evidence_event(raw: Any) -> EvidenceEvent:
    if isinstance(raw, EvidenceEvent):
        raw.validate()
        return raw
    if isinstance(raw, dict):
        return EvidenceEvent.from_dict(raw)
    return EvidenceEvent.from_run_event(raw)
