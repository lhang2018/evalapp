"""记忆存储与检索治理（PRD 21-28 / Architecture 8.1-8.4）。

核心规则：
- 类型与归属是两个独立维度。
- 可信状态：candidate（候选）/ verified（已验证）/ restricted（受限）/ pending_review（待复核）/ revoked（已撤销）。
- 一次失败的模型推断不能直接写成 verified 长期经验。
- 检索流程：权限过滤 → 范围过滤 → 有效性 → 相关性召回 → 可信度排序 → 预算选择 → 使用记录。
"""

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .conflicts import ConflictLedger, TombstoneLedger
from .embeddings import (EmbeddingProvider, HashEmbeddingProvider, hash_vector,
                         normalize, words)
from .manifest import AccessGrant, MemoryAccessError, MemoryIdentity


class MemoryKind(str, Enum):
    USER_PREFERENCE = "user_preference"          # PRD 22.1
    PROJECT_STATE = "project_state"              # 22.2
    DOMAIN_KNOWLEDGE = "domain_knowledge"        # 22.3
    SKILL_WORKFLOW = "skill_workflow"            # 22.4
    RUNTIME_EXPERIENCE = "runtime_experience"    # 22.5
    MODEL_ENV_ADAPTATION = "model_env_adaptation"  # 22.6
    RAW_TRACE = "raw_trace"                      # 22.7（不自动转为经验）
    AUDIT = "audit"


class Credibility(str, Enum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    RESTRICTED = "restricted"
    PENDING_REVIEW = "pending_review"
    REVOKED = "revoked"


class Scope(str, Enum):
    USER = "user"
    PROJECT = "project"
    SESSION = "session"
    TASK = "task"
    EXPERT = "expert"
    MODEL = "model"
    SHARED = "shared"


@dataclass
class MemoryItem:
    content: str
    kind: MemoryKind = MemoryKind.RUNTIME_EXPERIENCE
    scope: Scope = Scope.TASK
    scope_key: str = ""                          # task_id / project / expert_id / model_id
    status: Credibility = Credibility.CANDIDATE
    source: str = "runtime"                      # 来源（user/model/tool/eval）
    source_version: str = ""
    applicable_domains: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)
    # 记忆间关系：{"relation": "supports|contradicts|refines|derived_from", "target": id}
    relations: List[Dict[str, str]] = field(default_factory=list)
    superseded_by: Optional[str] = None
    revoked_reason: Optional[str] = None
    allowed: bool = True                # 访问权限（策略层可再收紧）
    memory_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    valid_until: Optional[float] = None        # 过期标记（PRD 26）
    use_count: int = 0
    # ALN-05：事实键与五类身份作用域（身份字段为空表示未声明归属）
    fact_key: str = ""
    user_id: str = ""
    project_id: str = ""
    session_id: str = ""
    task_id: str = ""
    expert_id: str = ""
    conflict_ids: List[str] = field(default_factory=list)
    has_conflict: bool = False

    def is_expired(self, now: float) -> bool:
        return bool(self.valid_until and now > self.valid_until)

    def to_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["kind"] = self.kind.value
        d["scope"] = self.scope.value
        d["status"] = self.status.value
        return d

    def to_public_dict(self) -> Dict[str, Any]:
        """用户端展示字段：不含证据快照与内部关系（PRD 89.4）。"""
        return {
            "memory_id": self.memory_id, "kind": self.kind.value,
            "scope": self.scope.value, "scope_key": self.scope_key,
            "status": self.status.value, "content": self.content,
            "source": self.source,
            "applicable_domains": list(self.applicable_domains),
            "superseded_by": self.superseded_by,
            "valid_until": self.valid_until,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "use_count": self.use_count,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryItem":
        d = dict(d)
        d["kind"] = MemoryKind(d.get("kind", "runtime_experience"))
        d["scope"] = Scope(d.get("scope", "task"))
        d["status"] = Credibility(d.get("status", "candidate"))
        item = cls.__new__(cls)
        for k, v in d.items():
            if hasattr(cls, k) or k in cls.__dataclass_fields__:
                setattr(item, k, v)
        return item


class MemoryStore:
    """逻辑存储 + 检索治理（Arch 8.1）。"""

    _SCHEMA = """CREATE TABLE IF NOT EXISTS memory_items(
        memory_id TEXT PRIMARY KEY, kind TEXT, scope TEXT, scope_key TEXT,
        status TEXT, content TEXT, source TEXT, allowed INTEGER,
        created_at REAL, updated_at REAL, superseded_by TEXT, extra TEXT)"""

    _VECTOR_SCHEMA = """CREATE TABLE IF NOT EXISTS memory_vectors(
        memory_id TEXT PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL,
        vector TEXT NOT NULL, updated_at REAL)"""

    def __init__(self, path: str = ".evoagent/memory.db",
                 embedder: Optional[EmbeddingProvider] = None):
        import pathlib
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        # RLock：冲突/墓碑账本与存储共用同一临界区，可重入
        self._lock = threading.RLock()
        self._embedder = embedder or HashEmbeddingProvider()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._migrate_if_needed()
            self._conn.execute(self._SCHEMA)
            self._conn.execute(self._VECTOR_SCHEMA)
            self._conn.commit()
        self.conflicts = ConflictLedger(self._conn, self._lock)
        self.tombstones = TombstoneLedger(self._conn, self._lock)

    def _migrate_if_needed(self) -> None:
        """开发期自动重建不兼容旧表；生产版需换成正式迁移。"""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(memory_items)")]
        if cols and "superseded_by" not in cols:
            self._conn.execute("DROP TABLE memory_items")

    # ---- 写入（PRD 27：来源识别→权限检查→冲突检查→验证→范围→版本记录）----
    def write(self, item: MemoryItem, *, require_verification: bool = True,
              identity: Optional[MemoryIdentity] = None,
              fact_key: str = "") -> MemoryItem:
        """require_verification=True 时：只有带证据的写入才能直接 verified。

        identity 非空时把五类身份键钉在条目上；fact_key 相同且内容不同的
        新写入不会覆盖旧值，而是产生未裁决冲突（ALN-05）。
        """
        if identity is not None:
            if not isinstance(identity, MemoryIdentity):
                raise ValueError("identity 必须是 MemoryIdentity")
            _apply_identity(item, identity)
        if fact_key:
            item.fact_key = fact_key
        if require_verification and item.status == Credibility.VERIFIED \
                and not item.evidence:
            item.status = Credibility.PENDING_REVIEW
        existing = None
        with self._lock:
            references = [item.memory_id] + [relation.get("target") for relation in item.relations
                if relation.get("relation") in {"derived_from", "cached_from"}]
            if any(self._conn.execute("SELECT 1 FROM memory_tombstones WHERE memory_id=?", (ref,)).fetchone()
                   for ref in references if ref):
                raise MemoryAccessError("已删除记忆及其过期派生引用不可重新写入")
            previous = self.get(item.memory_id)
            if previous is not None and identity is not None and not _identity_visible(previous, _access_keys(identity, None)):
                raise MemoryAccessError("不可覆盖其他身份的记忆")
            # 简单冲突处理：同 scope+kind 相同内容 → 跳过重复
            duplicates = self._conn.execute(
                "SELECT * FROM memory_items WHERE scope=? AND kind=? AND scope_key=?"
                " AND content=? AND status!='revoked'",
                (item.scope.value, item.kind.value, item.scope_key, item.content)).fetchall()
            for row in duplicates:
                duplicate = self._row_to_item(row)
                if _owner_partition(duplicate) == _owner_partition(item):
                    return duplicate
            if item.fact_key:
                existing = self._find_fact(item)
            self._conn.execute(
                "INSERT OR REPLACE INTO memory_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.memory_id, item.kind.value, item.scope.value, item.scope_key,
                 item.status.value, item.content, item.source,
                 1 if item.allowed else 0, item.created_at, item.updated_at,
                 item.superseded_by,
                 json.dumps(self._extra(item), ensure_ascii=False)))
            self._conn.commit()
        self._store_vector(item)
        if existing is not None and existing.content != item.content:
            self._record_conflict(existing, item)
        return item

    def _find_fact(self, item: MemoryItem) -> Optional[MemoryItem]:
        rows = self._conn.execute(
            "SELECT * FROM memory_items WHERE scope=? AND scope_key=?"
            " AND status!='revoked' AND memory_id!=?",
            (item.scope.value, item.scope_key, item.memory_id)).fetchall()
        for row in rows:
            candidate = self._row_to_item(row)
            if (candidate.fact_key and candidate.fact_key == item.fact_key
                    and _owner_partition(candidate) == _owner_partition(item)):
                return candidate
        return None

    def _record_conflict(self, old_item: MemoryItem,
                         new_item: MemoryItem) -> Dict[str, Any]:
        conflict = self.conflicts.record(old_item, new_item)
        self.conflicts.attach(conflict["conflict_id"],
                              [old_item.memory_id, new_item.memory_id])
        new_item.has_conflict = True
        if conflict["conflict_id"] not in new_item.conflict_ids:
            new_item.conflict_ids.append(conflict["conflict_id"])
        old_item.has_conflict = True
        return conflict

    @staticmethod
    def _extra(item: MemoryItem) -> Dict[str, Any]:
        return {"source_version": item.source_version,
                "applicable_domains": item.applicable_domains,
                "evidence": item.evidence,
                "relations": item.relations,
                "revoked_reason": item.revoked_reason,
                "valid_until": item.valid_until, "use_count": item.use_count,
                "fact_key": item.fact_key,
                "user_id": item.user_id, "project_id": item.project_id,
                "session_id": item.session_id, "task_id": item.task_id,
                "expert_id": item.expert_id,
                "conflict_ids": list(item.conflict_ids),
                "has_conflict": bool(item.has_conflict)}

    # ---- 向量化（PRD 25）----
    def _embed_texts(self, texts: List[str]):
        """返回 (模型标识, 归一化向量)；外部嵌入失败时回退哈希向量。"""
        try:
            vectors = self._embedder.embed(list(texts))
            model = getattr(self._embedder, "model_id", "unknown")
            if len(vectors) == len(texts) and all(
                    len(v) > 0 for v in vectors):
                return model, [normalize(v) for v in vectors]
        except Exception:  # noqa: BLE001  嵌入服务不可用不能阻断检索
            pass
        return "hash-64", [hash_vector(t) for t in texts]

    def _store_vector(self, item: MemoryItem) -> None:
        model, vectors = self._embed_texts([item.content])
        self._write_vector(item.memory_id, model, vectors[0])

    def _write_vector(self, memory_id: str, model: str,
                      vector: List[float]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO memory_vectors VALUES (?,?,?,?,?)",
                (memory_id, model, len(vector), json.dumps(vector),
                 time.time()))
            self._conn.commit()

    def _vectors_for(self, items: List[MemoryItem],
                     model: str) -> Dict[str, List[float]]:
        """取与查询同模型的向量；缺失或换模型时批量重算并落盘。"""
        ids = [m.memory_id for m in items]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            "SELECT memory_id, vector FROM memory_vectors"
            f" WHERE model=? AND memory_id IN ({placeholders})",
            [model, *ids]).fetchall()
        out = {r["memory_id"]: json.loads(r["vector"]) for r in rows}
        missing = [m for m in items if m.memory_id not in out]
        if missing:
            fresh_model, vectors = self._embed_texts(
                [m.content for m in missing])
            if fresh_model != model:
                return out
            with self._lock:
                for m, vec in zip(missing, vectors):
                    self._conn.execute(
                        "INSERT OR REPLACE INTO memory_vectors"
                        " VALUES (?,?,?,?,?)",
                        (m.memory_id, model, len(vec), json.dumps(vec),
                         time.time()))
                    out[m.memory_id] = vec
                self._conn.commit()
        return out

    # ---- 检索（PRD 25）----
    def search(self, query: str, *, scope: Optional[Scope] = None,
               scope_key: str = "", domains: Optional[List[str]] = None,
               limit: int = 5,
               identity: Optional[MemoryIdentity] = None,
               grant: Optional[AccessGrant] = None) -> List[MemoryItem]:
        now = time.time()
        rows = self._conn.execute(
            "SELECT memory_id, kind, scope, scope_key, status, content, source,"
            " allowed, created_at, updated_at, extra FROM memory_items").fetchall()
        # noqa: superseded_by 在 extra JSON 中，检索时以 status 过滤为主
        items = [self._row_to_item(r) for r in rows]
        # 1) 权限过滤
        items = [m for m in items if m.allowed and m.status != Credibility.REVOKED]
        # 1b) 身份作用域隔离（ALN-05）：未显式授权的跨作用域请求直接拒绝
        if identity is not None:
            keys = _access_keys(identity, grant)
            if scope is not None:
                _require_scope_access(scope, scope_key, keys, identity)
            elif scope_key and scope_key not in _all_keys(keys):
                raise MemoryAccessError(f"越权访问作用域键: {scope_key}")
            items = [m for m in items if _identity_visible(m, keys)]
        # 2) 过期过滤
        items = [m for m in items if not m.is_expired(now)]
        # 3) 范围过滤（GAP 死字段修复③：scope_key 真正参与检索隔离）
        if scope is not None:
            items = [m for m in items if m.scope == scope
                     and _key_isolated(m, scope_key)]
        elif scope_key:
            # 只给了键未给 scope：同样执行键隔离（此前此路径被完全忽略，
            # memory_scope_key 因此是死字段——赋值存在、检索不生效）
            items = [m for m in items if _key_isolated(m, scope_key)]
        # 4) 领域过滤
        if domains:
            matching = [m for m in items
                        if any(d in m.applicable_domains for d in domains)
                        or m.kind == MemoryKind.USER_PREFERENCE]
            items = matching or [m for m in items if m.scope == Scope.SHARED]
        # 5) 相关性：词重叠（精确）+ 向量余弦（真实嵌入或哈希回退）混合
        q_words = set(_words(query))
        model, q_vectors = self._embed_texts([query])
        q_vec = q_vectors[0]
        vectors = self._vectors_for(items, model)
        cred_boost = {Credibility.VERIFIED: 2.0, Credibility.RESTRICTED: 0.5,
                      Credibility.CANDIDATE: 1.0, Credibility.PENDING_REVIEW: 0.8}
        scored = []
        for m in items:
            m_words = set(_words(m.content))
            overlap = len(q_words & m_words)
            m_vec = vectors.get(m.memory_id)
            cosine = _cosine(q_vec, m_vec) if m_vec else 0.0
            if overlap == 0 and cosine < 0.05:
                continue
            lexical = overlap / max(1, len(q_words))
            relevance = 0.5 * lexical + 0.5 * cosine
            score = relevance * cred_boost.get(m.status, 1.0)
            scored.append((round(score, 6), m))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [m for _, m in scored[:limit]]

    # ---- 状态治理 ----
    def set_status(self, memory_id: str, status: Credibility,
                   reason: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE memory_items SET status=?, updated_at=? WHERE memory_id=?",
                (status.value, time.time(), memory_id))
            self._conn.commit()

    def supersede(self, old_id: str, new_id: str, reason: str = "") -> None:
        """事实替代（PRD 26）：新事实不无条件覆盖旧事实，需显式调用。"""
        self.set_status(old_id, Credibility.REVOKED, reason)
        with self._lock:
            self._conn.execute(
                "UPDATE memory_items SET superseded_by=?, updated_at=? WHERE memory_id=?",
                (new_id, time.time(), old_id))
            self._conn.commit()

    def get(self, memory_id: str, *,
            identity: Optional[MemoryIdentity] = None,
            grant: Optional[AccessGrant] = None) -> Optional[MemoryItem]:
        row = self._conn.execute(
            "SELECT * FROM memory_items WHERE memory_id=?",
            (memory_id,)).fetchone()
        if row is None:
            return None
        item = self._row_to_item(row)
        if identity is not None and not _identity_visible(
                item, _access_keys(identity, grant)):
            raise MemoryAccessError(f"越权读取记忆: {memory_id}")
        return item

    # ---- 用户端管理（PRD 89.4：查看 / 纠正 / 删除）----
    def list_items(self, *, kind: Optional[MemoryKind] = None,
                   scope: Optional[Scope] = None,
                   status: Optional[Credibility] = None,
                   allowed_only: bool = True,
                   limit: int = 200,
                   identity: Optional[MemoryIdentity] = None,
                   grant: Optional[AccessGrant] = None) -> List[MemoryItem]:
        rows = self._conn.execute(
            "SELECT * FROM memory_items ORDER BY created_at DESC").fetchall()
        items = [self._row_to_item(r) for r in rows]
        if identity is not None:
            items = [m for m in items
                     if _identity_visible(m, _access_keys(identity, grant))]
        if allowed_only:
            items = [m for m in items if m.allowed]
        if kind is not None:
            items = [m for m in items if m.kind == kind]
        if scope is not None:
            items = [m for m in items if m.scope == scope]
        if status is not None:
            items = [m for m in items if m.status == status]
        else:
            items = [m for m in items if m.status != Credibility.REVOKED]
        return items[:limit]

    def correct(self, memory_id: str, content: str, *,
                reason: str = "") -> Optional[MemoryItem]:
        """用户纠正记忆内容：来源改为 user，状态置 verified（PRD 89.4）。"""
        item = self.get(memory_id)
        if item is None:
            return None
        item.content = content
        item.source = "user"
        item.status = Credibility.VERIFIED
        item.updated_at = time.time()
        item.evidence = dict(item.evidence or {})
        item.evidence["user_correction"] = {"reason": reason,
                                            "at": item.updated_at}
        with self._lock:
            self._conn.execute(
                "UPDATE memory_items SET content=?, source=?, status=?,"
                " updated_at=?, extra=? WHERE memory_id=?",
                (item.content, item.source, item.status.value,
                 item.updated_at,
                 json.dumps(self._extra(item), ensure_ascii=False),
                 memory_id))
            self._conn.commit()
        self._store_vector(item)
        return item

    def delete(self, memory_id: str, *, actor: str = "",
               reason: str = "") -> bool:
        """删除源记忆：写墓碑并沿派生/缓存引用链删除（ALN-05）。"""
        result = self.delete_with_chain(memory_id, actor=actor, reason=reason)
        return bool(result.get("ok"))

    def delete_with_chain(self, memory_id: str, *, actor: str = "",
                          reason: str = "") -> Dict[str, Any]:
        return self.tombstones.delete_with_chain(
            memory_id, actor=actor, reason=reason, conflicts=self.conflicts)

    # ---- 冲突治理入口（规则裁决与人工裁决分离，ALN-05）----
    def list_conflicts(self, *, status: Optional[str] = None,
                       limit: int = 200) -> List[Dict[str, Any]]:
        return self.conflicts.list(status=status, limit=limit)

    def adjudicate_conflict(self, conflict_id: str) -> Dict[str, Any]:
        return self.conflicts.adjudicate_rules(conflict_id)

    def resolve_conflict(self, conflict_id: str, winner_id: str, *,
                         actor: str, reason: str) -> Dict[str, Any]:
        return self.conflicts.resolve_manually(
            conflict_id, winner_id, actor=actor, reason=reason)

    def record_use(self, memory_id: str) -> None:
        """使用记录（PRD 25 检索最后一步）：use_count 存于 extra JSON。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT extra FROM memory_items WHERE memory_id=?",
                (memory_id,)).fetchone()
            if row is None:
                return
            try:
                extra = json.loads(row["extra"] or "{}")
            except Exception:  # noqa: BLE001
                extra = {}
            extra["use_count"] = int(extra.get("use_count", 0)) + 1
            self._conn.execute(
                "UPDATE memory_items SET extra=?, updated_at=? WHERE memory_id=?",
                (json.dumps(extra, ensure_ascii=False), time.time(), memory_id))
            self._conn.commit()

    def link(self, memory_id: str, relation: str, target_id: str) -> None:
        """建立记忆关系（supports/contradicts/refines/derived_from）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT extra FROM memory_items WHERE memory_id=?",
                (memory_id,)).fetchone()
            if row is None:
                raise KeyError(memory_id)
            try:
                extra = json.loads(row["extra"] or "{}")
            except Exception:  # noqa: BLE001
                extra = {}
            relations = list(extra.get("relations") or [])
            entry = {"relation": relation, "target": target_id}
            if entry not in relations:
                relations.append(entry)
            extra["relations"] = relations
            self._conn.execute(
                "UPDATE memory_items SET extra=?, updated_at=? WHERE memory_id=?",
                (json.dumps(extra, ensure_ascii=False), time.time(), memory_id))
            self._conn.commit()

    def related(self, memory_id: str) -> List[MemoryItem]:
        item = self.get(memory_id)
        if item is None:
            return []
        out = []
        for rel in item.relations:
            target = self.get(rel.get("target", ""))
            if target is not None:
                out.append(target)
        return out

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass

    def stats(self) -> Dict[str, Any]:
        rows = self._conn.execute(
            "SELECT kind, scope, status, COUNT(*) AS n FROM memory_items"
            " GROUP BY kind, scope, status").fetchall()
        out: Dict[str, Any] = {"total": 0, "by_kind": {}, "by_status": {},
                               "by_scope": {}}
        for r in rows:
            n = int(r["n"])
            out["total"] += n
            for dim, val in (("by_kind", r["kind"]), ("by_status", r["status"]),
                             ("by_scope", r["scope"])):
                out[dim][val] = out[dim].get(val, 0) + n
        return out

    def _row_to_item(self, r: sqlite3.Row) -> MemoryItem:
        extra = {}
        try:
            extra = json.loads(r["extra"])
        except Exception:
            pass
        item = MemoryItem(
            memory_id=r["memory_id"], kind=MemoryKind(r["kind"]),
            scope=Scope(r["scope"]), scope_key=r["scope_key"],
            status=Credibility(r["status"]), content=r["content"],
            source=r["source"], allowed=bool(r["allowed"]),
            created_at=r["created_at"], updated_at=r["updated_at"],
            superseded_by=(r["superseded_by"]
                           if "superseded_by" in r.keys() else None))
        for k, v in extra.items():
            if hasattr(item, k):
                setattr(item, k, v)
        return item


def _vector(text: str) -> List[float]:
    """哈希向量（回退实现）；保留名称供旧调用点使用。"""
    return hash_vector(text)


_SCOPE_IDENTITY_FIELDS = {
    "user": "user_id",
    "project": "project_id",
    "session": "session_id",
    "task": "task_id",
    "expert": "expert_id",
}


def _apply_identity(item: MemoryItem, identity: MemoryIdentity) -> None:
    """写入时把五类身份键钉在条目上，并让 scope_key 与作用域字段一致。"""
    for field_name in _SCOPE_IDENTITY_FIELDS.values():
        setattr(item, field_name, getattr(identity, field_name, "") or "")
    field_name = _SCOPE_IDENTITY_FIELDS.get(item.scope.value)
    if field_name and not item.scope_key:
        item.scope_key = getattr(identity, field_name, "") or ""


class _IdentityKeys(dict):
    """Keep coherent grant targets; never combine owners from unrelated grants."""
    pass


def _access_keys(identity: MemoryIdentity,
                 grant: Optional[AccessGrant]) -> Dict[str, set]:
    """身份自带作用域 + 显式授权作用域；授权只增加可见键，不扩大默认范围。"""
    keys = _IdentityKeys()
    keys.primary = identity
    keys.identities = {scope: [identity] for scope in _SCOPE_IDENTITY_FIELDS}
    for scope, field_name in _SCOPE_IDENTITY_FIELDS.items():
        value = getattr(identity, field_name, "") or ""
        if value:
            keys.setdefault(scope, set()).add(value)
    if grant is not None:
        for scope, value in grant.scope_map().items():
            keys.setdefault(scope, set()).add(value)
            keys.identities[scope].append(grant.identity)
    return keys


def _all_keys(keys: Dict[str, set]) -> set:
    out: set = set()
    for values in keys.values():
        out.update(values)
    return out


def _owner_fields(scope):
    return {
        "user": ("user_id",), "project": ("user_id", "project_id"),
        "session": ("user_id", "project_id", "session_id"),
        "task": ("user_id", "project_id", "session_id", "task_id"),
        "expert": ("user_id", "project_id", "session_id", "expert_id"),
        "shared": ("user_id",), "model": ("user_id", "project_id", "session_id"),
    }.get(scope, ())


def _owner_partition(item):
    return tuple(getattr(item, field, "") for field in _owner_fields(item.scope.value))


def _identity_visible(item: MemoryItem, keys: Dict[str, set]) -> bool:
    """作用域隔离：只读当前身份链上的键；shared 无 owner 时横切可见。"""
    scope = item.scope.value
    if scope == "shared":
        owner = item.user_id
        return not owner or owner in keys.get("user", set())
    field_name = _SCOPE_IDENTITY_FIELDS.get(scope)
    if field_name is None:
        return all(not getattr(item, field, "") or
                   getattr(item, field) == getattr(keys.primary, field)
                   for field in _owner_fields(scope))
    item_key = getattr(item, field_name, "") or item.scope_key
    if not item_key or item_key not in keys.get(scope, set()):
        return False
    for identity in keys.identities.get(scope, ()):
        if getattr(identity, field_name) != item_key:
            continue
        # An unowned private hive is historical data, not a public expert.
        if scope != "user" and identity.user_id and not item.user_id:
            continue
        if all(not getattr(item, field, "") or
               getattr(item, field) == getattr(identity, field)
               for field in _owner_fields(scope)):
            return True
    return False


def _require_scope_access(scope: Scope, scope_key: str,
                          keys: Dict[str, set],
                          identity: MemoryIdentity) -> None:
    scope_name = scope.value
    if scope_name in ("shared", "model"):
        return
    field_name = _SCOPE_IDENTITY_FIELDS.get(scope_name)
    if field_name is None:
        return
    allowed = keys.get(scope_name, set())
    wanted = scope_key or getattr(identity, field_name, "")
    if wanted:
        if wanted not in allowed:
            raise MemoryAccessError(f"越权访问作用域 {scope_name}:{wanted}")
    elif not allowed:
        raise MemoryAccessError(f"越权访问作用域 {scope_name}")


def _key_isolated(m: MemoryItem, want: str) -> bool:
    """scope_key 隔离判定（GAP 死字段修复③）。

    - 条目带键：必须与请求键一致（键不匹配绝不跨界可见）
    - 条目空键：仅 shared（横切通用层，如用户偏好）放行；
      task/expert/project/model 空键视为归属未声明，按隔离处置——
      取消旧口径的 `or not m.scope_key` 全量放行
    - 请求键为空：不参与隔离（予以全量）
    """
    if not want:
        return True
    if m.scope_key:
        return m.scope_key == want
    return m.scope == Scope.SHARED


def _cosine(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _words(text: str) -> List[str]:
    """混合分词：拉丁按词；中文用 bigram（朴素但够用于重叠记分）。"""
    return words(text)
