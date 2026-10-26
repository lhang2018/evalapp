"""Persisted scope policy shared by memory controls and runtime retrieval.

同时提供多主体分区策略与旧策略迁移/回退（ALN-24-F）：

- 既有 `read_scope_policy` 保持原语义（供 `store.py`/`user_control.py` 的
  单主体检索与上下文过滤使用）；缺失策略仍按“历史默认允许全部范围”处理，
  这是既有单用户行为，不在本层改变。
- 新增 `ScopePolicyStore`：**主体由服务端鉴权决定**（拒绝按请求体选策略）；
  未显式开启多主体时仍走旧单行策略；开启后按 `principal_id` 分区，互不影响；
  迁移支持 dry-run/幂等/冲突拒绝/回退核对；缺失或损坏的旧策略在迁移与
  strict 生效路径上**拒绝默认放宽**。
- 不重复 c6c9872 的检索过滤：本模块只产出策略与迁移结果，检索过滤仍由
  既有 `read_scope_policy` 消费方执行。
"""
import hashlib
import json
import threading
import time
import uuid
from functools import wraps
from typing import Any, Dict, List, Optional

from .manifest import MemoryAccessError


POLICY_TABLE = """CREATE TABLE IF NOT EXISTS memory_scope_policy(
    id INTEGER PRIMARY KEY CHECK(id=1), allow_json TEXT NOT NULL,
    deny_json TEXT NOT NULL, actor TEXT, reason TEXT, updated_at REAL)"""

PRINCIPAL_POLICY_TABLE = """CREATE TABLE IF NOT EXISTS memory_principal_scope_policy(
    principal_id TEXT PRIMARY KEY, multi_principal INTEGER NOT NULL DEFAULT 0,
    allow_json TEXT NOT NULL, deny_json TEXT NOT NULL, version INTEGER NOT NULL,
    actor TEXT, reason TEXT, updated_at REAL)"""

MIGRATION_TABLE = """CREATE TABLE IF NOT EXISTS memory_scope_policy_migrations(
    migration_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL,
    legacy_digest TEXT NOT NULL, new_digest TEXT NOT NULL, dry_run INTEGER NOT NULL,
    action TEXT NOT NULL, actor TEXT, reason TEXT, created_at REAL)"""

SCOPE_VALUES = ("user", "project", "session", "task", "expert", "model", "shared")


class ScopePolicyError(ValueError):
    """策略请求非法、旧策略缺失/损坏或迁移不可执行。"""


class PrincipalMismatch(ScopePolicyError):
    """请求试图使用非鉴权主体的策略（越权选择主体）。"""


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _validate_scopes(values: Any, label: str) -> List[str]:
    if not isinstance(values, list) or any(
            not isinstance(item, str) or item not in SCOPE_VALUES
            for item in values):
        raise ScopePolicyError(label + " 必须是合法作用域数组")
    return sorted(set(values))


def read_scope_policy(connection, scope_values):
    """Read one policy snapshot; missing means legacy defaults, corrupt means deny.

    The caller owns the store lock. No row is different from an explicitly empty
    allowlist: the latter allows no scopes. Identity grants cannot widen policy.
    """
    values = sorted(scope_values)
    row = connection.execute(
        "SELECT allow_json, deny_json, actor, reason, updated_at"
        " FROM memory_scope_policy WHERE id=1").fetchone()
    if row is None:
        return {"allow": values, "deny": [], "all_layers": True,
                "updated_at": None, "scope_values": values}
    try:
        allow, deny = json.loads(row["allow_json"]), json.loads(row["deny_json"])
        if any(not isinstance(group, list) or
               any(not isinstance(scope, str) or scope not in values for scope in group)
               for group in (allow, deny)):
            raise ValueError("invalid scope list")
    except (TypeError, ValueError):
        raise MemoryAccessError("记忆范围策略损坏，拒绝检索；请重新保存合法策略") from None
    return {"allow": sorted(set(allow)), "deny": sorted(set(deny)),
            "actor": row["actor"], "reason": row["reason"],
            "updated_at": row["updated_at"], "all_layers": False,
            "scope_values": values}


def _transactional(method):
    """Keep policy preconditions and writes in one SQLite writer transaction."""
    @wraps(method)
    def run(self, *args, **kwargs):
        with self._lock:
            started = not self._conn.in_transaction
            if started:
                self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = method(self, *args, **kwargs)
            except BaseException:
                if started and self._conn.in_transaction:
                    self._conn.rollback()
                raise
            else:
                if started and self._conn.in_transaction:
                    self._conn.commit()
                return result
    return run


class ScopePolicyStore:
    """主体分区策略 + 旧策略迁移/回退（同库，幂等建表，无版本化迁移）。"""

    def __init__(self, conn, *, lock=None, clock=None):
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()
        self._clock = clock or time.time
        self._ensure()

    def _ensure(self) -> None:
        with self._lock:
            self._conn.execute(POLICY_TABLE)
            self._conn.execute(PRINCIPAL_POLICY_TABLE)
            self._conn.execute(MIGRATION_TABLE)
            self._conn.commit()

    # ---- 主体解析：服务端鉴权为唯一来源 ----
    @staticmethod
    def resolve_principal(authenticated: str, requested: Any = None) -> str:
        if not isinstance(authenticated, str) or not authenticated.strip():
            raise ScopePolicyError("缺少已鉴权主体")
        principal = authenticated.strip()
        if requested is not None:
            if not isinstance(requested, str) or not requested.strip():
                raise ScopePolicyError("请求主体格式非法")
            if requested.strip() != principal:
                raise PrincipalMismatch(
                    "请求主体与鉴权主体不一致，拒绝按请求体选择策略")
        return principal

    # ---- 旧单行策略（ALN-24-D）----
    def legacy_policy(self) -> Dict[str, Any]:
        try:
            row = self._conn.execute(
                "SELECT allow_json, deny_json, actor, reason, updated_at"
                " FROM memory_scope_policy WHERE id=1").fetchone()
        except Exception:  # noqa: BLE001 —— 表不存在即视为缺失
            return {"present": False, "valid": False, "source": "legacy_missing"}
        if row is None:
            return {"present": False, "valid": False, "source": "legacy_missing"}
        try:
            allow = json.loads(row["allow_json"])
            deny = json.loads(row["deny_json"])
            if (not isinstance(allow, list) or not isinstance(deny, list)
                    or any(item not in SCOPE_VALUES for item in allow + deny)):
                raise ValueError("作用域非法")
        except (ValueError, TypeError):
            return {"present": True, "valid": False, "source": "legacy_corrupt",
                    "digest": _digest({"allow": row["allow_json"],
                                       "deny": row["deny_json"]})}
        payload = {"allow": sorted(set(allow)), "deny": sorted(set(deny))}
        return {"present": True, "valid": True, "source": "legacy",
                "allow": payload["allow"], "deny": payload["deny"],
                "digest": _digest(payload), "actor": row["actor"],
                "reason": row["reason"], "updated_at": row["updated_at"]}

    # ---- 主体分区策略 ----
    def principal_policy(self, principal_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT multi_principal, allow_json, deny_json, version, actor,"
            " reason, updated_at FROM memory_principal_scope_policy"
            " WHERE principal_id=?", (principal_id,)).fetchone()
        if row is None:
            return None
        try:
            allow = json.loads(row["allow_json"])
            deny = json.loads(row["deny_json"])
            if any(item not in SCOPE_VALUES for item in allow + deny):
                raise ValueError("作用域非法")
        except (ValueError, TypeError):
            raise ScopePolicyError("主体策略损坏，拒绝默认放宽: " + principal_id)
        payload = {"allow": sorted(set(allow)), "deny": sorted(set(deny))}
        return {"principal_id": principal_id,
                "multi_principal": bool(row["multi_principal"]),
                "allow": payload["allow"], "deny": payload["deny"],
                "version": row["version"], "actor": row["actor"],
                "reason": row["reason"], "updated_at": row["updated_at"],
                "digest": _digest(payload)}

    @_transactional
    def set_principal_policy(self, *, authenticated: str, requested: Any = None,
                             multi_principal: bool, allow: Any, deny: Any,
                             actor: str, reason: str) -> Dict[str, Any]:
        principal = self.resolve_principal(authenticated, requested)
        allow_list = _validate_scopes(allow, "allow")
        deny_list = _validate_scopes(deny, "deny")
        if set(allow_list) & set(deny_list):
            raise ScopePolicyError("同一作用域不能同时在 allow 与 deny")
        if not allow_list and not deny_list:
            raise ScopePolicyError("至少指定一个允许或禁止的作用域")
        if not isinstance(multi_principal, bool):
            raise ScopePolicyError("multi_principal 必须是布尔值")
        actor = (actor or "").strip()
        reason = (reason or "").strip()
        if not actor or not reason:
            raise ScopePolicyError("actor/reason 不得为空")
        now = self._clock()
        with self._lock:
            existing = self.principal_policy(principal)
            version = (existing["version"] + 1) if existing else 1
            self._conn.execute(
                "INSERT INTO memory_principal_scope_policy VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(principal_id) DO UPDATE SET"
                " multi_principal=excluded.multi_principal,"
                " allow_json=excluded.allow_json, deny_json=excluded.deny_json,"
                " version=excluded.version, actor=excluded.actor,"
                " reason=excluded.reason, updated_at=excluded.updated_at",
                (principal, 1 if multi_principal else 0,
                 json.dumps(allow_list), json.dumps(deny_list), version,
                 actor, reason, now))
        return {"ok": True, "policy": self.principal_policy(principal)}

    def effective_policy(self, principal_id: str, *,
                         strict: bool = True) -> Dict[str, Any]:
        """主体实际生效策略：分区优先；未开启多主体时沿用旧单行策略。"""
        principal = self.resolve_principal(principal_id)
        partitioned = self.principal_policy(principal)
        if partitioned is not None and partitioned["multi_principal"]:
            return {"principal_id": principal, "partitioned": True,
                    "source": "principal", "allow": partitioned["allow"],
                    "deny": partitioned["deny"],
                    "version": partitioned["version"]}
        legacy = self.legacy_policy()
        if legacy.get("valid"):
            return {"principal_id": principal, "partitioned": False,
                    "source": "legacy", "allow": legacy["allow"],
                    "deny": legacy["deny"],
                    "legacy_digest": legacy.get("digest")}
        if strict:
            raise ScopePolicyError(
                "旧单主体策略缺失或损坏，拒绝默认开放: "
                + str(legacy.get("source")))
        return {"principal_id": principal, "partitioned": False,
                "source": str(legacy.get("source")), "allow": [],
                "deny": list(SCOPE_VALUES),
                "warning": "策略缺失/损坏：按拒绝全部范围处理，需人工修复"}

    def scope_visible(self, principal_id: str, scope: str) -> bool:
        if scope not in SCOPE_VALUES:
            raise ScopePolicyError("未知作用域: " + str(scope))
        policy = self.effective_policy(principal_id)
        return scope not in policy["deny"] and scope in policy["allow"]

    # ---- 迁移与回退 ----
    @_transactional
    def migrate_legacy(self, *, authenticated: str, requested: Any = None,
                       dry_run: bool = True, actor: str = "api:admin",
                       reason: str) -> Dict[str, Any]:
        principal = self.resolve_principal(authenticated, requested)
        if not isinstance(dry_run, bool):
            raise ScopePolicyError("dry_run 必须是布尔值")
        reason = (reason or "").strip()
        if not reason:
            raise ScopePolicyError("迁移必须给出 reason")
        legacy = self.legacy_policy()
        if not legacy.get("valid"):
            raise ScopePolicyError(
                "旧单主体策略缺失或损坏，拒绝迁移（不默认放宽）: "
                + str(legacy.get("source")))
        payload = {"allow": legacy["allow"], "deny": legacy["deny"]}
        new_digest = _digest(payload)
        existing = self.principal_policy(principal)
        if existing is not None:
            if existing["digest"] == new_digest and existing["multi_principal"]:
                return {"ok": True, "status": "already_migrated",
                        "principal_id": principal, "digest": new_digest}
            raise ScopePolicyError(
                "目标主体已有不同分区策略，拒绝覆盖（需人工合并）: "
                + principal)
        migration_id = "mig_" + uuid.uuid4().hex[:12]
        now = self._clock()
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory_scope_policy_migrations VALUES"
                " (?,?,?,?,?,?,?,?,?)",
                (migration_id, principal, legacy.get("digest") or "",
                 new_digest, 1 if dry_run else 0,
                 "dry_run" if dry_run else "apply", actor, reason, now))
            if not dry_run:
                self._conn.execute(
                    "INSERT INTO memory_principal_scope_policy VALUES"
                    " (?,?,?,?,?,?,?,?)",
                    (principal, 1, json.dumps(legacy["allow"]),
                     json.dumps(legacy["deny"]), 1, actor, reason, now))
        return {"ok": True,
                "status": "dry_run" if dry_run else "applied",
                "migration_id": migration_id, "principal_id": principal,
                "legacy_digest": legacy.get("digest"),
                "new_digest": new_digest,
                "allow": legacy["allow"], "deny": legacy["deny"]}

    @_transactional
    def rollback_migration(self, *, authenticated: str, requested: Any = None,
                           actor: str = "api:admin",
                           reason: str) -> Dict[str, Any]:
        principal = self.resolve_principal(authenticated, requested)
        reason = (reason or "").strip()
        if not reason:
            raise ScopePolicyError("回退必须给出 reason")
        row = self._conn.execute(
            "SELECT migration_id, new_digest, legacy_digest FROM memory_scope_policy_migrations"
            " WHERE principal_id=? AND action='apply' ORDER BY created_at DESC, rowid DESC"
            " LIMIT 1", (principal,)).fetchone()
        if row is None:
            raise ScopePolicyError("该主体没有可回退的迁移记录")
        existing = self.principal_policy(principal)
        if existing is None:
            raise ScopePolicyError("分区策略已不存在，回退不可执行")
        if (existing["digest"] != row["new_digest"]
                or existing["version"] != 1 or not existing["multi_principal"]):
            raise ScopePolicyError(
                "分区策略在迁移后被修改，拒绝按旧迁移回退（需人工确认）")
        legacy = self.legacy_policy()
        if not legacy.get("valid") or legacy.get("digest") != row["legacy_digest"]:
            raise ScopePolicyError("旧策略在迁移后已变化或损坏，拒绝回退到未经核对的范围")
        now = self._clock()
        with self._lock:
            self._conn.execute(
                "DELETE FROM memory_principal_scope_policy WHERE principal_id=?",
                (principal,))
            self._conn.execute(
                "INSERT INTO memory_scope_policy_migrations VALUES"
                " (?,?,?,?,?,?,?,?,?)",
                ("rb_" + uuid.uuid4().hex[:12],
                 principal, row["new_digest"], row["new_digest"], 0,
                 "rollback", actor, reason, now))
        return {"ok": True, "principal_id": principal,
                "rolled_back_to": "legacy",
                "legacy_valid": bool(legacy.get("valid")),
                "verification": {
                    "partitioned": False,
                    "source": "legacy" if legacy.get("valid") else "legacy_missing",
                }}

    def migration_history(self, principal_id: str) -> Dict[str, Any]:
        rows = self._conn.execute(
            "SELECT migration_id, legacy_digest, new_digest, dry_run, action,"
            " actor, reason, created_at FROM memory_scope_policy_migrations"
            " WHERE principal_id=? ORDER BY created_at, rowid", (principal_id,)
        ).fetchall()
        return {"principal_id": principal_id,
                "migrations": [dict(row) for row in rows]}
