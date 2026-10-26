"""故障经验知识库（ALN-27-A / 27-C）。

知识条目（故障经验）必须带**来源、可信度、适用域与版本**，错误经验可撤销。
写操作返回新条目、不原地改写历史，供审计追源。

ALN-27-C 在保持原内存 API 的前提下增加 SQLite 持久化：
- ``KnowledgeStore(conn)`` 重启后保留版本链、撤销状态与来源；
- 写穿表 ``knowledge_entries``（同一 entry_id 唯一，版本只增不改）；
- ``migrate_knowledge`` 把内存库受控迁移到持久库，迁移前后摘要核验、
  撤销状态保真、幂等且冲突拒绝；内存用法（``KnowledgeStore()``）保持兼容。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, Optional


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id, "key": self.key,
            "content": self.content, "source": self.source,
            "confidence": float(self.confidence),
            "applicable_domains": list(self.applicable_domains),
            "version": int(self.version), "created_at": float(self.created_at),
            "revoked_at": self.revoked_at, "revoke_reason": self.revoke_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeEntry":
        return cls(entry_id=str(data["entry_id"]), key=str(data["key"]),
                   content=str(data["content"]), source=str(data["source"]),
                   confidence=float(data["confidence"]),
                   applicable_domains=tuple(data.get("applicable_domains") or ()),
                   version=int(data["version"]),
                   created_at=float(data["created_at"]),
                   revoked_at=data.get("revoked_at"),
                   revoke_reason=data.get("revoke_reason"))


_TABLE = """CREATE TABLE IF NOT EXISTS knowledge_entries(
    entry_id TEXT PRIMARY KEY, key TEXT NOT NULL, content TEXT NOT NULL,
    source TEXT NOT NULL, confidence REAL NOT NULL,
    applicable_domains TEXT NOT NULL, version INTEGER NOT NULL,
    created_at REAL NOT NULL, revoked_at REAL, revoke_reason TEXT)"""


def _rows(conn) -> Iterable[Dict[str, Any]]:
    cursor = conn.execute("SELECT * FROM knowledge_entries")
    columns = [d[0] for d in cursor.description]
    for row in cursor.fetchall():
        yield dict(zip(columns, row))


class KnowledgeStore:
    """知识库：可选 SQLite 持久化；同 ``key`` 版本必须严格递增。"""

    def __init__(self, conn: Any = None) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self._entries: dict[str, KnowledgeEntry] = {}
        if conn is not None:
            with self._lock:
                conn.execute(_TABLE)
                conn.commit()
            self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        with self._lock:
            for row in _rows(self._conn):
                domains = json.loads(row["applicable_domains"] or "[]")
                entry = KnowledgeEntry(
                    entry_id=row["entry_id"], key=row["key"],
                    content=row["content"], source=row["source"],
                    confidence=float(row["confidence"]),
                    applicable_domains=tuple(domains),
                    version=int(row["version"]),
                    created_at=float(row["created_at"]),
                    revoked_at=row["revoked_at"],
                    revoke_reason=row["revoke_reason"])
                self._entries[entry.entry_id] = entry

    def _save(self, entry: KnowledgeEntry) -> None:
        if self._conn is None:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO knowledge_entries VALUES"
                " (?,?,?,?,?,?,?,?,?,?)",
                (entry.entry_id, entry.key, entry.content, entry.source,
                 float(entry.confidence),
                 json.dumps(list(entry.applicable_domains), ensure_ascii=False),
                 int(entry.version), float(entry.created_at),
                 entry.revoked_at, entry.revoke_reason))
            self._conn.commit()

    # ---- 读 ----
    def get(self, entry_id: str) -> Optional[KnowledgeEntry]:
        return self._entries.get(entry_id)

    def all(self) -> tuple[KnowledgeEntry, ...]:
        return tuple(self._entries.values())

    def active(self) -> tuple[KnowledgeEntry, ...]:
        return tuple(e for e in self._entries.values() if not e.revoked)

    def versions_of(self, key: str) -> tuple[KnowledgeEntry, ...]:
        return tuple(sorted(
            (e for e in self._entries.values() if e.key == key),
            key=lambda e: e.version))

    def latest(self, key: str) -> Optional[KnowledgeEntry]:
        candidates = [e for e in self._entries.values()
                      if e.key == key and not e.revoked]
        if not candidates:
            return None
        return max(candidates, key=lambda e: e.version)

    def digest(self) -> str:
        payload = sorted((e.to_dict() for e in self._entries.values()),
                         key=lambda item: item["entry_id"])
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                         sort_keys=True).encode("utf-8")).hexdigest()

    # ---- 写（只增版本、不原地改历史）----
    def add(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        entry.validate()
        with self._lock:
            if entry.entry_id in self._entries:
                raise KnowledgeError(f"entry_id 已存在：{entry.entry_id}")
            versions = [e.version for e in self._entries.values()
                        if e.key == entry.key]
            if versions and entry.version <= max(versions):
                raise KnowledgeError(
                    f"版本必须递增：{entry.key} 已有版本 {max(versions)}")
            self._entries[entry.entry_id] = entry
            self._save(entry)
        return entry

    def revoke(self, entry_id: str, *, reason: str, at: float) -> KnowledgeEntry:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                raise KnowledgeError(f"条目不存在：{entry_id}")
            if entry.revoked:
                return entry
            revoked = replace(entry, revoked_at=float(at),
                              revoke_reason=reason or "撤销")
            self._entries[entry_id] = revoked
            self._save(revoked)
            return revoked


def migrate_knowledge(source: KnowledgeStore, target: KnowledgeStore, *,
                      actor: str, reason: str) -> Dict[str, Any]:
    """把内存知识库受控迁移到持久库（摘要核验、撤销保真、幂等/冲突拒绝）。"""
    if not isinstance(actor, str) or not actor.strip():
        raise KnowledgeError("迁移必须记录 actor")
    if not isinstance(reason, str) or not reason.strip():
        raise KnowledgeError("迁移必须记录 reason")
    if source is target:
        raise KnowledgeError("源与目标是同一个知识库，无需迁移")
    source_digest = source.digest()
    target_digest = target.digest()
    if source_digest == target_digest:
        return {"ok": True, "status": "already_migrated", "count": len(source.all()),
                "digest": source_digest, "actor": actor, "reason": reason}
    if target.all():
        raise KnowledgeError("目标知识库非空且内容不同，拒绝覆盖（需人工合并）")
    entries = sorted(source.all(), key=lambda e: (e.key, e.version, e.entry_id))
    for entry in entries:
        target.add(KnowledgeEntry.from_dict(entry.to_dict()))
        if entry.revoked:
            target.revoke(entry.entry_id, reason=entry.revoke_reason or "迁移保留撤销",
                          at=float(entry.revoked_at))
    if target.digest() != source_digest:
        raise KnowledgeError("迁移后摘要不一致，拒绝视为完成")
    return {"ok": True, "status": "migrated", "count": len(entries),
            "revoked": sum(1 for e in entries if e.revoked),
            "digest": source_digest, "actor": actor, "reason": reason,
            "note": "迁移只增版本、保留撤销；不删除源库"}
