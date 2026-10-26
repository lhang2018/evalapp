"""注入前适用性门禁（ALN-27-A）。

规则：**不适用的条目不注入**。撤销、适用域不符、可信度低于阈值的条目一律
跳过，并保留跳过原因，便于审计与解释。
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

from .store import KnowledgeEntry, KnowledgeStore


@dataclass(frozen=True)
class SkippedEntry:
    entry_id: str
    reason: str


@dataclass(frozen=True)
class InjectionPlan:
    domain: str
    injected: tuple[KnowledgeEntry, ...]
    skipped: tuple[SkippedEntry, ...]


def _validate_threshold(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= 1 or not math.isfinite(value)):
        raise ValueError("min_confidence必须是[0,1]中的有限数字")


def inapplicability_reason(
    entry: KnowledgeEntry, *, domain: str, min_confidence: float = 0.0
) -> Optional[str]:
    """返回不可注入的原因；``None`` 表示可注入。"""
    _validate_threshold(min_confidence)
    entry.validate()
    if entry.revoked:
        return "已撤销"
    if domain not in entry.applicable_domains:
        return f"适用域不含 {domain}"
    if float(entry.confidence) < float(min_confidence):
        return f"可信度 {entry.confidence} 低于阈值 {min_confidence}"
    return None


def plan_injection(
    store: KnowledgeStore,
    *,
    domain: str,
    min_confidence: float = 0.0,
) -> InjectionPlan:
    """为 ``domain`` 生成注入计划：可注入条目按可信度/版本降序，其余记录原因。"""
    _validate_threshold(min_confidence)
    injected: list[KnowledgeEntry] = []
    skipped: list[SkippedEntry] = []
    ordered = sorted(
        store.all(), key=lambda e: (-float(e.confidence), -e.version, e.entry_id)
    )
    for entry in ordered:
        reason = inapplicability_reason(
            entry, domain=domain, min_confidence=min_confidence
        )
        if reason is None:
            injected.append(entry)
        else:
            skipped.append(SkippedEntry(entry.entry_id, reason))
    return InjectionPlan(domain, tuple(injected), tuple(skipped))
