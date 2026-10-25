"""用户记忆控制服务（ALN-24-D：记忆来源、纠错、删除与范围调整）。

复用 ALN-05 已接受的存储/冲突/墓碑切片（`memory/store.py`、`memory/conflicts.py`），
不重写检索与删除链，只补用户可见的控制面：

- **看来源**：`list_memories/get_memory` 返回公共字段（内容、来源、作用域、
  可信度、使用次数、是否被取代），不暴露证据快照与内部关系；
- **纠错**：`correct_memory` 调 `MemoryStore.correct`（来源置 user、状态 verified）
  并**回检检索**：后续检索必须召回纠正后内容（GLOBAL-02 后续使用纠正事实）；
- **删除**：`delete_memory` 调 `MemoryStore.delete_with_chain`（沿派生/缓存引用链
  写墓碑），并**明确列出保留项与失败项**，不宣称「全域擦除」——墓碑仅存内容摘要、
  审计日志与历史备份不追删；
- **范围调整**：`set_scope_policy` 持久化「允许/禁止参与分析与检索的作用域」，
  禁止优先；规则落在记忆库自身表 `memory_scope_policy`（幂等建表，不动共享 schema）。

边界：本服务只作用于本地记忆库与检索结果；跨端/备份/外部仓储的擦除不在本卡范围，
`fully_erased` 永远为 False，报告里逐层写清保留项。
"""

import json
import time
from typing import Any, Dict, List, Optional

from .store import Credibility, MemoryItem, MemoryKind, MemoryStore, Scope

POLICY_TABLE = """CREATE TABLE IF NOT EXISTS memory_scope_policy(
    id INTEGER PRIMARY KEY CHECK(id=1), allow_json TEXT NOT NULL,
    deny_json TEXT NOT NULL, actor TEXT, reason TEXT, updated_at REAL)"""

RETAINED_LAYERS = [
    {"layer": "tombstones",
     "note": "仅保留内容摘要与归属标识，不可还原正文"},
    {"layer": "审计日志", "note": "删除/纠错动作本身保留，用于追责"},
    {"layer": "历史备份",
     "note": "既有备份含旧内容，未追删；如需彻底擦除需单独清理备份"},
]


class MemoryControlError(ValueError):
    """记忆控制请求非法（空内容/空理由/非法作用域/未知记忆）。"""


class MemoryNotFound(MemoryControlError):
    """记忆不存在或已被删除。"""


def _scope_values() -> List[str]:
    return [item.value for item in Scope]


class MemoryControlService:
    """用户可见记忆控制（来源/纠错/删除/范围）。"""

    def __init__(self, store: MemoryStore, *, db: Any = None,
                 audit: Any = None, clock=None):
        self.store = store
        self.db = db
        self.audit = audit
        self.clock = clock or time.time
        self._ensure_policy()

    # ---- 内部 ----
    def _ensure_policy(self) -> None:
        with self.store._lock:
            self.store._conn.execute(POLICY_TABLE)
            self.store._conn.commit()

    def _audit(self, action: str, **detail: Any) -> Optional[Dict[str, Any]]:
        event = {"action": action, "at": self.clock(), **detail}
        if self.audit is None:
            return event
        try:
            self.audit.record(action, detail=detail)  # 既有审计对象
        except Exception:  # noqa: BLE001 —— 审计失败不阻断用户操作，但报告里可见
            event["audit_error"] = True
        return event

    @staticmethod
    def _text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise MemoryControlError(label + "不得为空")
        return value.strip()

    # ---- 看来源 ----
    def list_memories(self, *, scope: Optional[str] = None,
                      kind: Optional[str] = None,
                      limit: int = 100) -> List[Dict[str, Any]]:
        scope_enum = Scope(scope) if scope else None
        kind_enum = MemoryKind(kind) if kind else None
        items = self.store.list_items(kind=kind_enum, scope=scope_enum,
                                      limit=max(1, min(int(limit), 500)))
        return [self._public(item) for item in items]

    def get_memory(self, memory_id: str) -> Dict[str, Any]:
        item = self.store.get(self._text(memory_id, "memory_id"))
        if item is None:
            raise MemoryNotFound("记忆不存在或已删除: " + memory_id)
        return self._public(item)

    def _public(self, item: MemoryItem) -> Dict[str, Any]:
        public = item.to_public_dict()
        public["provenance"] = {"source": item.source,
                                "scope": item.scope.value,
                                "scope_key": item.scope_key,
                                "fact_key": item.fact_key,
                                "task_id": item.task_id}
        public["privacy"] = {
            "storage": "local-memory-db",
            "exportable": False,
            "note": "记忆正文仅本地存储；导出/上传不在本服务范围"}
        return public

    # ---- 纠错 ----
    def correct_memory(self, memory_id: str, content: str, *,
                       actor: str, reason: str) -> Dict[str, Any]:
        memory_id = self._text(memory_id, "memory_id")
        content = self._text(content, "content")
        actor = self._text(actor, "actor")
        reason = self._text(reason, "reason")
        item = self.store.correct(memory_id, content, reason=reason)
        if item is None:
            raise MemoryNotFound("记忆不存在或已删除，无法纠错: " + memory_id)
        # GLOBAL-02：后续检索必须使用纠正事实（不是只改库不回检）
        hits = self.store.search(content, scope=item.scope, limit=20)
        verified = any(hit.memory_id == memory_id and hit.content == content
                       for hit in hits)
        audit = self._audit("memory_corrected", memory_id=memory_id,
                            actor=actor, reason=reason)
        return {"ok": True, "memory_id": memory_id,
                "item": self._public(item),
                "verified_in_retrieval": bool(verified),
                "retrieval_hits": [hit.memory_id for hit in hits],
                "actor": actor, "reason": reason, "audit": audit,
                "note": "纠正后来源=user、状态=verified；检索回检通过后才算生效"}

    # ---- 删除 ----
    def delete_memory(self, memory_id: str, *, actor: str,
                      reason: str) -> Dict[str, Any]:
        memory_id = self._text(memory_id, "memory_id")
        actor = self._text(actor, "actor")
        reason = self._text(reason, "reason")
        item = self.store.get(memory_id)
        if item is None:
            raise MemoryNotFound("记忆不存在或已删除: " + memory_id)
        former_content = item.content
        result = self.store.delete_with_chain(memory_id, actor=actor,
                                              reason=reason)
        if not result.get("ok"):
            return {"ok": False, "memory_id": memory_id, "failures": [
                {"stage": "delete_chain", "error": "删除链未执行"}],
                "retained": RETAINED_LAYERS, "fully_erased": False,
                "scope_claim": "仅本地记忆库与检索缓存"}
        recall_after = self.store.search(former_content,
                                         scope=item.scope, limit=20)
        retrieval_excludes = all(hit.memory_id not in result["deleted_ids"]
                                 for hit in recall_after)
        audit = self._audit("memory_deleted", memory_id=memory_id,
                            actor=actor, reason=reason,
                            deleted_ids=result["deleted_ids"])
        return {"ok": True, "memory_id": memory_id,
                "deleted_ids": result["deleted_ids"],
                "deleted_count": result["deleted"],
                "tombstones": result["tombstones"],
                "invalidated_conflicts": result.get("invalidated_conflicts", []),
                "recall_check": {"get_after_delete": self.store.get(memory_id),
                                 "retrieval_excludes_deleted": bool(retrieval_excludes)},
                "retained": RETAINED_LAYERS,
                "failures": [],
                "fully_erased": False,
                "scope_claim": "仅本地记忆库与检索缓存；不宣称跨备份/跨端/外部仓储擦除",
                "actor": actor, "reason": reason, "audit": audit}

    # ---- 范围调整 ----
    def get_scope_policy(self) -> Dict[str, Any]:
        row = self.store._conn.execute(
            "SELECT allow_json, deny_json, actor, reason, updated_at"
            " FROM memory_scope_policy WHERE id=1").fetchone()
        if row is None:
            return {"allow": sorted(_scope_values()), "deny": [],
                    "all_layers": True, "updated_at": None,
                    "scope_values": sorted(_scope_values())}
        return {"allow": json.loads(row["allow_json"]),
                "deny": json.loads(row["deny_json"]),
                "actor": row["actor"], "reason": row["reason"],
                "updated_at": row["updated_at"], "all_layers": False,
                "scope_values": sorted(_scope_values())}

    def set_scope_policy(self, *, allow: Any, deny: Any, actor: str,
                         reason: str) -> Dict[str, Any]:
        actor = self._text(actor, "actor")
        reason = self._text(reason, "reason")
        if not isinstance(allow, list) or not isinstance(deny, list):
            raise MemoryControlError("allow/deny 必须是作用域数组")
        values = set(_scope_values())
        allow_set, deny_set = set(allow), set(deny)
        if not allow_set <= values or not deny_set <= values:
            raise MemoryControlError("包含未知作用域: "
                                     + str(sorted((allow_set | deny_set) - values)))
        if allow_set & deny_set:
            raise MemoryControlError("同一作用域不能同时在 allow 与 deny")
        if not allow_set and not deny_set:
            raise MemoryControlError("至少指定一个允许或禁止的作用域")
        now = self.clock()
        with self.store._lock:
            self.store._conn.execute(
                "INSERT INTO memory_scope_policy VALUES (1,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET allow_json=excluded.allow_json,"
                " deny_json=excluded.deny_json, actor=excluded.actor,"
                " reason=excluded.reason, updated_at=excluded.updated_at",
                (json.dumps(sorted(allow_set)), json.dumps(sorted(deny_set)),
                 actor, reason, now))
            self.store._conn.commit()
        audit = self._audit("memory_scope_policy_updated",
                            allow=sorted(allow_set), deny=sorted(deny_set),
                            actor=actor, reason=reason)
        return {"ok": True, "policy": self.get_scope_policy(), "audit": audit,
                "note": "禁止优先；策略用于控制面过滤，装配器接线见保留缺口"}

    def scope_allowed(self, scope: str) -> bool:
        policy = self.get_scope_policy()
        if scope in policy["deny"]:
            return False
        return scope in policy["allow"]
