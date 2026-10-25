"""故障经验知识库（ALN-27-A）。

知识条目（故障经验）必须带**来源、可信度、适用域与版本**，错误经验可撤销。
写操作返回新条目、不原地改写历史，供审计追溯。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional


class KnowledgeError(ValueError):
    """知识条目的校验失败或版本冲突。"""


@dataclass(frozen=True)
class KnowledgeEntry:
    """一条不可变知识。同 ``key`` 的不同 ``version`` 是同一经验的不同版本。"""

    entry_id: str
    key: str
    content: str
    source: str
    confidence: float
    applicable_domains: tuple[str, ...]
    version: int
    created_at: float
    revoked_at: Optional[float] = None
    revoke_reason: Optional[str] = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def validate(self) -> None:
        if not str(self.entry_id).strip():
            raise KnowledgeError("entry_id 必填")
        if not str(self.key).strip():
            raise KnowledgeError("key 必填")
        if not str(self.content).strip():
            raise KnowledgeError("content 必填")
        if not str(self.source).strip():
            raise KnowledgeError("来源必填：知识必须可追溯到出处")
        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 防御
            raise KnowledgeError("confidence 必须是数值") from exc
        if not 0.0 <= confidence <= 1.0:
            raise KnowledgeError("confidence 必须在 [0, 1]")
        if not self.applicable_domains:
            raise KnowledgeError("适用域不能为空")
        if any(not str(domain).strip() for domain in self.applicable_domains):
            raise KnowledgeError("适用域不能包含空值")
        if int(self.version) < 1:
            raise KnowledgeError("version 必须 >= 1")


class KnowledgeStore:
    """进程内知识库。同一 ``key`` 的版本必须严格递增，不允许覆盖旧版本。"""

    def __init__(self) -> None:
        self._entries: dict[str, KnowledgeEntry] = {}

    def add(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        entry.validate()
        if entry.entry_id in self._entries:
            raise KnowledgeError(f"entry_id 已存在：{entry.entry_id}")
        versions = [e.version for e in self._entries.values() if e.key == entry.key]
        if versions and entry.version <= max(versions):
            raise KnowledgeError(
                f"版本必须递增：{entry.key} 已有版本 {max(versions)}"
            )
        self._entries[entry.entry_id] = entry
        return entry

    def get(self, entry_id: str) -> Optional[KnowledgeEntry]:
        return self._entries.get(entry_id)

    def all(self) -> tuple[KnowledgeEntry, ...]:
        return tuple(self._entries.values())

    def active(self) -> tuple[KnowledgeEntry, ...]:
        """未撤销的条目（含历史版本）。"""
        return tuple(e for e in self._entries.values() if not e.revoked)

    def versions_of(self, key: str) -> tuple[KnowledgeEntry, ...]:
        return tuple(
            sorted(
                (e for e in self._entries.values() if e.key == key),
                key=lambda e: e.version,
            )
        )

    def latest(self, key: str) -> Optional[KnowledgeEntry]:
        """同 key 未撤销的最高版本；全部撤销则为 None（不会回退到旧版本）。"""
        candidates = [e for e in self._entries.values() if e.key == key and not e.revoked]
        if not candidates:
            return None
        return max(candidates, key=lambda e: e.version)

    def revoke(self, entry_id: str, *, reason: str, at: float) -> KnowledgeEntry:
        """撤销一条错误经验；幂等，重复撤销返回同一条目。"""
        entry = self._entries.get(entry_id)
        if entry is None:
            raise KnowledgeError(f"条目不存在：{entry_id}")
        if entry.revoked:
            return entry
        revoked = replace(
            entry, revoked_at=float(at), revoke_reason=reason or "撤销"
        )
        self._entries[entry_id] = revoked
        return revoked
