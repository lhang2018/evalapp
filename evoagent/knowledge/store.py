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
import math
import threading
import uuid
from contextlib import contextmanager
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
        for name in ("entry_id", "key", "content", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KnowledgeError(name + "必须是非空字符串")
        if (isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float))
                or not 0.0 <= self.confidence <= 1.0):
            raise KnowledgeError("confidence 必须在 [0, 1]")
        if not isinstance(self.applicable_domains, (tuple, list)) or not self.applicable_domains:
            raise KnowledgeError("适用域不能为空")
        if any(not isinstance(domain, str) or not domain.strip() for domain in self.applicable_domains):
            raise KnowledgeError("适用域不能包含空值")
        if type(self.version) is not int or self.version < 1:
            raise KnowledgeError("version 必须是 >= 1 的整数")
        for value in (self.created_at, self.revoked_at):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or value < 0 or not math.isfinite(value)):
                raise KnowledgeError("知识时间戳必须是有限非负数字")
        if self.created_at is None:
            raise KnowledgeError("created_at必填")
        if self.revoke_reason is not None and (not isinstance(self.revoke_reason, str)
                                               or not self.revoke_reason.strip()):
            raise KnowledgeError("revoke_reason必须是非空字符串或None")

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
        domains = data.get("applicable_domains")
        if not isinstance(domains, (tuple, list)):
            raise KnowledgeError("适用域必须是数组")
        entry = cls(entry_id=data["entry_id"], key=data["key"],
                   content=data["content"], source=data["source"],
                   confidence=data["confidence"], applicable_domains=tuple(domains),
                   version=data["version"], created_at=data["created_at"],
                   revoked_at=data.get("revoked_at"),
                   revoke_reason=data.get("revoke_reason"))
        entry.validate()
        return entry


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

    def __init__(self, conn: Any = None, *, lock=None) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()
        self._entries: dict[str, KnowledgeEntry] = {}
        if conn is not None:
            with self._transaction():
                conn.execute(_TABLE)
                self._snapshot()  # Corrupt persisted rows must fail closed.

    # ---- 持久化 ----
    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._conn is None:
                before = dict(self._entries)
                try:
                    yield
                except BaseException:
                    self._entries = before
                    raise
                return
            conn = self._conn
            own = not conn.in_transaction
            savepoint = "knowledge_" + uuid.uuid4().hex
            conn.execute("BEGIN IMMEDIATE" if own else "SAVEPOINT " + savepoint)
            try:
                yield
                if own:
                    conn.commit()
                else:
                    conn.execute("RELEASE " + savepoint)
            except BaseException:
                if conn.in_transaction:
                    if own:
                        conn.rollback()
                    else:
                        conn.execute("ROLLBACK TO " + savepoint)
                        conn.execute("RELEASE " + savepoint)
                raise

    def _snapshot(self) -> Dict[str, KnowledgeEntry]:
        if self._conn is None:
            return dict(self._entries)
        entries = {}
        versions = set()
        for row in _rows(self._conn):
            try:
                row["applicable_domains"] = json.loads(row["applicable_domains"])
                entry = KnowledgeEntry.from_dict(row)
            except (TypeError, ValueError, KeyError) as exc:
                raise KnowledgeError("持久知识条目损坏，拒绝使用") from exc
            version = (entry.key, entry.version)
            if version in versions:
                raise KnowledgeError("持久知识存在重复key/version，拒绝使用")
            versions.add(version)
            entries[entry.entry_id] = entry
        return entries

    # ---- 读 ----
    def get(self, entry_id: str) -> Optional[KnowledgeEntry]:
        with self._lock:
            return self._snapshot().get(entry_id)

    def all(self) -> tuple[KnowledgeEntry, ...]:
        with self._lock:
            return tuple(self._snapshot().values())

    def active(self) -> tuple[KnowledgeEntry, ...]:
        return tuple(e for e in self.all() if not e.revoked)

    def versions_of(self, key: str) -> tuple[KnowledgeEntry, ...]:
        return tuple(sorted(
            (e for e in self.all() if e.key == key),
            key=lambda e: e.version))

    def latest(self, key: str) -> Optional[KnowledgeEntry]:
        candidates = [e for e in self.all()
                      if e.key == key and not e.revoked]
        if not candidates:
            return None
        return max(candidates, key=lambda e: e.version)

    def digest(self) -> str:
        return _digest(self.all())

    # ---- 写（只增版本、不原地改历史）----
    def add(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        entry.validate()
        entry = KnowledgeEntry.from_dict(entry.to_dict())
        with self._transaction():
            current = self._snapshot()
            if entry.entry_id in current:
                raise KnowledgeError(f"entry_id 已存在：{entry.entry_id}")
            versions = [e.version for e in current.values() if e.key == entry.key]
            if versions and entry.version <= max(versions):
                raise KnowledgeError(f"版本必须递增：{entry.key} 已有版本 {max(versions)}")
            if self._conn is None:
                self._entries[entry.entry_id] = entry
            else:
                self._conn.execute("INSERT INTO knowledge_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (entry.entry_id, entry.key, entry.content, entry.source, entry.confidence,
                     json.dumps(list(entry.applicable_domains), ensure_ascii=False), entry.version,
                     entry.created_at, entry.revoked_at, entry.revoke_reason))
        return entry

    def revoke(self, entry_id: str, *, reason: str, at: float) -> KnowledgeEntry:
        if not isinstance(reason, str) or not reason.strip():
            raise KnowledgeError("撤销必须记录reason")
        with self._transaction():
            entry = self._snapshot().get(entry_id)
            if entry is None:
                raise KnowledgeError(f"条目不存在：{entry_id}")
            if entry.revoked:
                return entry
            revoked = replace(entry, revoked_at=at, revoke_reason=reason)
            revoked.validate()
            if self._conn is None:
                self._entries[entry_id] = revoked
            else:
                self._conn.execute("UPDATE knowledge_entries SET revoked_at=?, revoke_reason=? WHERE entry_id=?",
                                   (at, reason, entry_id))
            return revoked


def _digest(entries) -> str:
    payload = sorted((e.to_dict() for e in entries), key=lambda item: item["entry_id"])
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                    sort_keys=True).encode("utf-8")).hexdigest()


def migrate_knowledge(source: KnowledgeStore, target: KnowledgeStore, *,
                      actor: str, reason: str) -> Dict[str, Any]:
    """把内存知识库受控迁移到持久库（摘要核验、撤销保真、幂等/冲突拒绝）。"""
    if not isinstance(actor, str) or not actor.strip():
        raise KnowledgeError("迁移必须记录 actor")
    if not isinstance(reason, str) or not reason.strip():
        raise KnowledgeError("迁移必须记录 reason")
    if source is target:
        raise KnowledgeError("源与目标是同一个知识库，无需迁移")
    entries = sorted(source.all(), key=lambda e: (e.key, e.version, e.entry_id))
    source_digest = _digest(entries)
    with target._transaction():
        if source_digest == target.digest():
            return {"ok": True, "status": "already_migrated", "count": len(entries),
                    "digest": source_digest, "actor": actor, "reason": reason}
        if target.all():
            raise KnowledgeError("目标知识库非空且内容不同，拒绝覆盖（需人工合并）")
        for entry in entries:
            target.add(entry)  # Includes revocation metadata; no intermediate activation.
        if target.digest() != source_digest:
            raise KnowledgeError("迁移后摘要不一致，拒绝视为完成")
    return {"ok": True, "status": "migrated", "count": len(entries),
            "revoked": sum(1 for e in entries if e.revoked),
            "digest": source_digest, "actor": actor, "reason": reason,
            "note": "迁移只增版本、保留撤销；不删除源库"}
