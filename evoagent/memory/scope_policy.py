"""Persisted scope policy shared by memory controls and runtime retrieval."""
import json

from .manifest import MemoryAccessError


POLICY_TABLE = """CREATE TABLE IF NOT EXISTS memory_scope_policy(
    id INTEGER PRIMARY KEY CHECK(id=1), allow_json TEXT NOT NULL,
    deny_json TEXT NOT NULL, actor TEXT, reason TEXT, updated_at REAL)"""


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
