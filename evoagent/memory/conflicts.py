"""冲突裁决与删除失效链（ALN-05）。

- 同一 fact_key 的新旧值不会互相覆盖；写入即产生未裁决冲突记录。
- 规则裁决（显式用户偏好优先、高可信来源优先）与人工裁决是两个入口，
  人工裁决必须记录 actor 与 reason。
- 删除源记忆写墓碑，并沿 derived_from / cached_from 引用链删除派生摘要；
  相关未裁决冲突标记 invalidated，幸存条目的冲突标记同步清除。
"""

import hashlib
import json
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

CONFLICT_SCHEMA = """CREATE TABLE IF NOT EXISTS memory_conflicts(
    conflict_id TEXT PRIMARY KEY, fact_key TEXT NOT NULL,
    scope TEXT, scope_key TEXT,
    old_memory_id TEXT, new_memory_id TEXT,
    old_digest TEXT, new_digest TEXT,
    old_source TEXT, new_source TEXT,
    old_status TEXT, new_status TEXT,
    old_updated_at REAL, new_updated_at REAL,
    status TEXT NOT NULL, resolution TEXT, resolved_by TEXT, reason TEXT,
    created_at REAL, updated_at REAL)"""

TOMBSTONE_SCHEMA = """CREATE TABLE IF NOT EXISTS memory_tombstones(
    tombstone_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL,
    content_digest TEXT, scope TEXT, scope_key TEXT, identity_json TEXT,
    root_memory_id TEXT, deleted_at REAL, actor TEXT, reason TEXT)"""

_CREDIBILITY_RANK = {"verified": 3, "restricted": 2, "candidate": 1,
                     "pending_review": 1, "revoked": 0}
_DERIVED_RELATIONS = ("derived_from", "cached_from")


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _extra(row: Any) -> Dict[str, Any]:
    try:
        value = json.loads(row["extra"] or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


class ConflictLedger:
    def __init__(self, conn: Any, lock: Any):
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.execute(CONFLICT_SCHEMA)
            self._conn.commit()

    # ---- 写入 ----
    def record(self, old_item: Any, new_item: Any,
               *, at: Optional[float] = None) -> Dict[str, Any]:
        fact_key = getattr(new_item, "fact_key", "") or ""
        if not fact_key:
            raise ValueError("冲突记录需要 fact_key")
        scope = getattr(new_item.scope, "value", new_item.scope)
        scope_key = getattr(new_item, "scope_key", "")
        old_digest = _digest(getattr(old_item, "content", ""))
        new_digest = _digest(getattr(new_item, "content", ""))
        now = time.time() if at is None else float(at)
        with self._lock:
            existing = self._conn.execute(
                "SELECT conflict_id FROM memory_conflicts"
                " WHERE fact_key=? AND scope=? AND scope_key=?"
                " AND old_memory_id=? AND new_digest=?"
                " AND status='unresolved' LIMIT 1",
                (fact_key, scope, scope_key,
                 getattr(old_item, "memory_id", ""), new_digest)).fetchone()
            if existing:
                return self.get(existing["conflict_id"])
            conflict_id = f"cf_{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                "INSERT INTO memory_conflicts VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (conflict_id, fact_key, scope, scope_key,
                 getattr(old_item, "memory_id", ""),
                 getattr(new_item, "memory_id", ""),
                 old_digest, new_digest,
                 getattr(old_item, "source", ""),
                 getattr(new_item, "source", ""),
                 getattr(getattr(old_item, "status", ""), "value",
                         getattr(old_item, "status", "")),
                 getattr(getattr(new_item, "status", ""), "value",
                         getattr(new_item, "status", "")),
                 float(getattr(old_item, "updated_at", 0) or 0),
                 float(getattr(new_item, "updated_at", 0) or 0),
                 "unresolved", None, None, None, now, now))
            self._conn.commit()
        return self.get(conflict_id)

    def attach(self, conflict_id: str, memory_ids: Iterable[str]) -> None:
        with self._lock:
            for memory_id in memory_ids:
                row = self._conn.execute(
                    "SELECT extra FROM memory_items WHERE memory_id=?",
                    (memory_id,)).fetchone()
                if row is None:
                    continue
                extra = _extra(row)
                ids = list(extra.get("conflict_ids") or [])
                if conflict_id not in ids:
                    ids.append(conflict_id)
                extra["conflict_ids"] = ids
                extra["has_conflict"] = True
                self._conn.execute(
                    "UPDATE memory_items SET extra=? WHERE memory_id=?",
                    (json.dumps(extra, ensure_ascii=False), memory_id))
            self._conn.commit()

    # ---- 查询 ----
    def get(self, conflict_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM memory_conflicts WHERE conflict_id=?",
            (conflict_id,)).fetchone()
        return dict(row) if row is not None else None

    def list(self, *, status: Optional[str] = None,
             scope: Optional[str] = None, scope_key: Optional[str] = None,
             limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM memory_conflicts WHERE 1=1"
        args: List[Any] = []
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        if scope is not None:
            sql += " AND scope=?"
            args.append(scope)
        if scope_key is not None:
            sql += " AND scope_key=?"
            args.append(scope_key)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        return [dict(row) for row in self._conn.execute(sql, args).fetchall()]

    # ---- 规则裁决（与人工裁决分离）----
    def adjudicate_rules(self, conflict_id: str) -> Dict[str, Any]:
        conflict = self.get(conflict_id)
        if conflict is None:
            raise KeyError(conflict_id)
        if conflict["status"] != "unresolved":
            return conflict
        old = self._item(conflict["old_memory_id"])
        new = self._item(conflict["new_memory_id"])
        if old is None or new is None:
            return self._mark_invalidated(conflict, "裁决前条目已删除")
        old_user = old["source"] == "user"
        new_user = new["source"] == "user"
        rule = ""
        winner: Optional[Dict[str, Any]] = None
        if old_user != new_user:
            winner = old if old_user else new
            rule = "explicit_user_preference"
        else:
            old_rank = _CREDIBILITY_RANK.get(old["status"], 0)
            new_rank = _CREDIBILITY_RANK.get(new["status"], 0)
            if old_rank != new_rank:
                winner = old if old_rank > new_rank else new
                rule = "higher_credibility"
        if winner is None:
            with self._lock:
                self._conn.execute(
                    "UPDATE memory_conflicts SET reason=?, updated_at=?"
                    " WHERE conflict_id=?",
                    ("规则无法裁决：可信度相同且无显式偏好，需人工裁决",
                     time.time(), conflict_id))
                self._conn.commit()
            return self.get(conflict_id)
        return self._resolve(conflict, winner["memory_id"],
                             resolution=f"rule:{rule}",
                             resolved_by="rules",
                             reason=f"规则裁决: {rule}")

    # ---- 人工裁决（独立入口，强制审计字段）----
    def resolve_manually(self, conflict_id: str, winner_id: str, *,
                         actor: str, reason: str) -> Dict[str, Any]:
        actor = str(actor or "").strip()
        reason = str(reason or "").strip()
        if not actor or not reason:
            raise ValueError("人工裁决必须记录 actor 与 reason")
        conflict = self.get(conflict_id)
        if conflict is None:
            raise KeyError(conflict_id)
        if conflict["status"] != "unresolved":
            return conflict
        if winner_id not in (conflict["old_memory_id"],
                             conflict["new_memory_id"]):
            raise ValueError("裁决胜者不属于该冲突")
        return self._resolve(conflict, winner_id, resolution="manual",
                             resolved_by=actor, reason=reason)

    # ---- 内部 ----
    def _item(self, memory_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT memory_id, source, status, superseded_by, extra"
            " FROM memory_items WHERE memory_id=?", (memory_id,)).fetchone()
        if row is None:
            return None
        return {"memory_id": row["memory_id"], "source": row["source"],
                "status": row["status"], "extra": _extra(row)}

    def _resolve(self, conflict: Dict[str, Any], winner_id: str, *,
                 resolution: str, resolved_by: str,
                 reason: str) -> Dict[str, Any]:
        loser_id = (conflict["new_memory_id"]
                    if winner_id == conflict["old_memory_id"]
                    else conflict["old_memory_id"])
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE memory_items SET status='revoked', superseded_by=?,"
                " updated_at=? WHERE memory_id=?",
                (winner_id, now, loser_id))
            self._remove_refs(conflict["conflict_id"],
                              (conflict["old_memory_id"],
                               conflict["new_memory_id"]))
            self._conn.execute(
                "UPDATE memory_conflicts SET status='resolved', resolution=?,"
                " resolved_by=?, reason=?, updated_at=? WHERE conflict_id=?",
                (resolution, resolved_by, reason, now,
                 conflict["conflict_id"]))
            self._conn.commit()
        return self.get(conflict["conflict_id"])

    def _mark_invalidated(self, conflict: Dict[str, Any],
                          reason: str) -> Dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "UPDATE memory_conflicts SET status='invalidated', reason=?,"
                " updated_at=? WHERE conflict_id=?",
                (reason, time.time(), conflict["conflict_id"]))
            self._remove_refs(conflict["conflict_id"],
                              (conflict["old_memory_id"],
                               conflict["new_memory_id"]))
            self._conn.commit()
        return self.get(conflict["conflict_id"])

    def _remove_refs(self, conflict_id: str,
                     memory_ids: Iterable[str]) -> None:
        for memory_id in memory_ids:
            row = self._conn.execute(
                "SELECT extra FROM memory_items WHERE memory_id=?",
                (memory_id,)).fetchone()
            if row is None:
                continue
            extra = _extra(row)
            ids = [cid for cid in extra.get("conflict_ids") or []
                   if cid != conflict_id]
            extra["conflict_ids"] = ids
            extra["has_conflict"] = bool(ids)
            self._conn.execute(
                "UPDATE memory_items SET extra=? WHERE memory_id=?",
                (json.dumps(extra, ensure_ascii=False), memory_id))

    def invalidate_for_memories(self, memory_ids: Iterable[str],
                                reason: str = "source_deleted") -> List[str]:
        ids = list(dict.fromkeys(memory_ids))
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT conflict_id, old_memory_id, new_memory_id"
            f" FROM memory_conflicts WHERE status='unresolved'"
            f" AND (old_memory_id IN ({placeholders})"
            f" OR new_memory_id IN ({placeholders}))",
            [*ids, *ids]).fetchall()
        invalidated: List[str] = []
        with self._lock:
            for row in rows:
                survivors = [mid for mid in (row["old_memory_id"],
                                             row["new_memory_id"])
                             if mid not in ids]
                self._conn.execute(
                    "UPDATE memory_conflicts SET status='invalidated',"
                    " reason=?, updated_at=? WHERE conflict_id=?",
                    (reason, time.time(), row["conflict_id"]))
                self._remove_refs(row["conflict_id"], survivors)
                invalidated.append(row["conflict_id"])
            self._conn.commit()
        return invalidated


class TombstoneLedger:
    def __init__(self, conn: Any, lock: Any):
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.execute(TOMBSTONE_SCHEMA)
            self._conn.commit()

    def delete_with_chain(self, memory_id: str, *, actor: str = "",
                          reason: str = "",
                          conflicts: Optional[ConflictLedger] = None
                          ) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT memory_id, content, scope, scope_key, extra"
                " FROM memory_items").fetchall()
            items = {row["memory_id"]: row for row in rows}
            if memory_id not in items:
                return {"ok": False, "deleted": 0, "deleted_ids": [],
                        "tombstones": [], "root": memory_id}
            chain: List[str] = []
            seen = {memory_id}
            queue = [memory_id]
            while queue:
                current = queue.pop(0)
                chain.append(current)
                for other_id, row in items.items():
                    if other_id in seen:
                        continue
                    extra = _extra(row)
                    for relation in extra.get("relations") or []:
                        if (relation.get("relation") in _DERIVED_RELATIONS
                                and relation.get("target") == current):
                            seen.add(other_id)
                            queue.append(other_id)
                            break
            now = time.time()
            tombstones: List[str] = []
            for target in chain:
                row = items[target]
                extra = _extra(row)
                identifier = {name: extra.get(name, "") for name in
                              ("user_id", "project_id", "session_id",
                               "task_id", "expert_id")}
                tombstone_id = f"tom_{uuid.uuid4().hex[:12]}"
                self._conn.execute(
                    "INSERT INTO memory_tombstones VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tombstone_id, target, _digest(row["content"]),
                     row["scope"], row["scope_key"],
                     json.dumps(identifier, ensure_ascii=False),
                     memory_id, now, str(actor or ""), str(reason or "")))
                self._conn.execute(
                    "DELETE FROM memory_vectors WHERE memory_id=?", (target,))
                self._conn.execute(
                    "DELETE FROM memory_items WHERE memory_id=?", (target,))
                tombstones.append(tombstone_id)
            invalidated: List[str] = []
            if conflicts is not None:
                invalidated = conflicts.invalidate_for_memories(
                    chain, "source_deleted")
            self._conn.commit()
        return {"ok": True, "deleted": len(chain),
                "deleted_ids": chain, "tombstones": tombstones,
                "invalidated_conflicts": invalidated, "root": memory_id}

    def list(self, *, memory_id: Optional[str] = None,
             limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM memory_tombstones"
        args: List[Any] = []
        if memory_id is not None:
            sql += " WHERE memory_id=? OR root_memory_id=?"
            args.extend([memory_id, memory_id])
        sql += " ORDER BY deleted_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        return [dict(row) for row in self._conn.execute(sql, args).fetchall()]
