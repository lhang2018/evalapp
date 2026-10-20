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

from .embeddings import (EmbeddingProvider, HashEmbeddingProvider, hash_vector,
                         normalize, words)


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
    TASK = "task"
    PROJECT = "project"
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
        self._lock = threading.Lock()
        self._embedder = embedder or HashEmbeddingProvider()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._migrate_if_needed()
            self._conn.execute(self._SCHEMA)
            self._conn.execute(self._VECTOR_SCHEMA)
            self._conn.commit()

    def _migrate_if_needed(self) -> None:
        """开发期自动重建不兼容旧表；生产版需换成正式迁移。"""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(memory_items)")]
        if cols and "superseded_by" not in cols:
            self._conn.execute("DROP TABLE memory_items")

    # ---- 写入（PRD 27：来源识别→权限检查→冲突检查→验证→范围→版本记录）----
    def write(self, item: MemoryItem, *, require_verification: bool = True) -> MemoryItem:
        """require_verification=True 时：只有带证据的写入才能直接 verified。"""
        if require_verification and item.status == Credibility.VERIFIED \
                and not item.evidence:
            item.status = Credibility.PENDING_REVIEW
        with self._lock:
            # 简单冲突处理：同 scope+kind 相同内容 → 跳过重复
            dup = self._conn.execute(
                "SELECT 1 FROM memory_items WHERE scope=? AND kind=? AND scope_key=?"
                " AND content=? AND status!='revoked'",
                (item.scope.value, item.kind.value, item.scope_key, item.content)).fetchone()
            if dup:
                return item
            self._conn.execute(
                "INSERT OR REPLACE INTO memory_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.memory_id, item.kind.value, item.scope.value, item.scope_key,
                 item.status.value, item.content, item.source,
                 1 if item.allowed else 0, item.created_at, item.updated_at,
                 item.superseded_by,
                 json.dumps(self._extra(item), ensure_ascii=False)))
            self._conn.commit()
        self._store_vector(item)
        return item

    @staticmethod
    def _extra(item: MemoryItem) -> Dict[str, Any]:
        return {"source_version": item.source_version,
                "applicable_domains": item.applicable_domains,
                "evidence": item.evidence,
                "relations": item.relations,
                "superseded_by": item.superseded_by,
                "revoked_reason": item.revoked_reason,
                "valid_until": item.valid_until, "use_count": item.use_count}

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
               limit: int = 5) -> List[MemoryItem]:
        now = time.time()
        rows = self._conn.execute(
            "SELECT memory_id, kind, scope, scope_key, status, content, source,"
            " allowed, created_at, updated_at, extra FROM memory_items").fetchall()
        # noqa: superseded_by 在 extra JSON 中，检索时以 status 过滤为主
        items = [self._row_to_item(r) for r in rows]
        # 1) 权限过滤
        items = [m for m in items if m.allowed and m.status != Credibility.REVOKED]
        # 2) 过期过滤
        items = [m for m in items if not m.is_expired(now)]
        # 3) 范围过滤（更窄 scope 匹配优先）
        if scope is not None:
            items = [m for m in items if m.scope == scope
                     and (not scope_key or not m.scope_key or m.scope_key == scope_key)]
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

    def get(self, memory_id: str) -> Optional[MemoryItem]:
        row = self._conn.execute(
            "SELECT * FROM memory_items WHERE memory_id=?",
            (memory_id,)).fetchone()
        return self._row_to_item(row) if row else None

    # ---- 用户端管理（PRD 89.4：查看 / 纠正 / 删除）----
    def list_items(self, *, kind: Optional[MemoryKind] = None,
                   scope: Optional[Scope] = None,
                   status: Optional[Credibility] = None,
                   allowed_only: bool = True,
                   limit: int = 200) -> List[MemoryItem]:
        rows = self._conn.execute(
            "SELECT * FROM memory_items ORDER BY created_at DESC").fetchall()
        items = [self._row_to_item(r) for r in rows]
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

    def delete(self, memory_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM memory_items WHERE memory_id=?", (memory_id,))
            self._conn.execute(
                "DELETE FROM memory_vectors WHERE memory_id=?", (memory_id,))
            self._conn.commit()
            return cur.rowcount > 0

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
            created_at=r["created_at"], updated_at=r["updated_at"])
        for k, v in extra.items():
            if hasattr(item, k):
                setattr(item, k, v)
        return item


def _vector(text: str) -> List[float]:
    """哈希向量（回退实现）；保留名称供旧调用点使用。"""
    return hash_vector(text)


def _cosine(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _words(text: str) -> List[str]:
    """混合分词：拉丁按词；中文用 bigram（朴素但够用于重叠记分）。"""
    return words(text)
