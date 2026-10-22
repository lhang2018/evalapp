"""Server-owned task scopes, intersected at the final action boundary."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import time
import uuid

from .leases import guard_write

RISK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
RESOURCE_KEYS = {"path": "path", "dir": "path", "source_path": "path",
                 "destination_path": "path", "target": "target", "target_id": "target",
                 "recipient": "target", "object_id": "target", "device_id": "device"}


class AuthorizationStore:
    def __init__(self, db):
        self.db = db

    def issue(self, task_id, policy, *, actor, ttl_seconds=86400):
        policy = deepcopy(policy)
        if not isinstance(policy, dict) or not task_id or not actor:
            raise ValueError("缺少任务、授权策略或授权人")
        if set(policy) - {"tools", "permissions", "path_roots", "targets", "device_ids", "max_risk"}:
            raise ValueError("未知授权范围字段")
        for key in ("tools", "permissions"):
            if not isinstance(policy.get(key), list) or any(not isinstance(v, str) or not v for v in policy[key]):
                raise ValueError("授权必须明确列出 " + key)
        for key in ("path_roots", "targets", "device_ids"):
            if key in policy and (not isinstance(policy[key], list) or
                    any(not isinstance(v, str) or not v for v in policy[key])):
                raise ValueError("授权范围必须是字符串数组: " + key)
        if policy.get("max_risk", "critical") not in RISK:
            raise ValueError("未知授权风险等级")
        if not isinstance(ttl_seconds, (int, float)) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("授权有效期必须为有限正数")
        if "path_roots" in policy:
            policy["path_roots"] = [str(Path(p).resolve()) for p in policy["path_roots"]]
        ref = "auth_" + uuid.uuid4().hex
        now = time.time()
        with self.db._lock:
            guard_write(self.db)
            self.db._conn.execute("INSERT INTO action_authorizations VALUES (?,?,?,?,?,?,NULL)",
                (ref, task_id, json.dumps(policy, ensure_ascii=False, allow_nan=False), actor, now, now + ttl_seconds))
            self.db._conn.commit()
        self.db.revoke_task_approvals(task_id)  # Old decisions cannot authorize a new revision.
        return ref

    def current(self, task_id):
        with self.db._lock:
            row = self.db._conn.execute("SELECT * FROM action_authorizations WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                                        (task_id,)).fetchone()
        return {**dict(row), "policy": json.loads(row["policy_json"])} if row else None

    def revoke(self, ref):
        with self.db._lock:
            guard_write(self.db)
            row = self.db._conn.execute("SELECT task_id FROM action_authorizations WHERE authorization_ref=?", (ref,)).fetchone()
            cur = self.db._conn.execute("UPDATE action_authorizations SET revoked_at=? WHERE authorization_ref=? AND revoked_at IS NULL",
                                        (time.time(), ref))
            self.db._conn.commit()
        if row:
            self.db.revoke_task_approvals(row["task_id"])
        return cur.rowcount == 1

    def check(self, task_id, contract, args, *, expected_ref=None):
        row = self.current(task_id) if task_id else None
        if row is None:
            task = self.db.get_task(task_id) if task_id else None
            if expected_ref or (task and task.authorization_ref):
                raise PermissionError("任务授权不存在")
            return None  # Trusted standalone/legacy callers have no task grant.
        ref = row["authorization_ref"]
        if (expected_ref and expected_ref != ref) or row["revoked_at"] is not None or row["expires_at"] <= time.time():
            raise PermissionError("任务授权已变更、撤销或过期")
        policy = row["policy"]
        if contract.tool_id not in policy["tools"]:
            raise PermissionError("工具超出任务授权")
        if set(contract.permission_requirements or []) - set(policy["permissions"]):
            raise PermissionError("工具所需权限未获任务授权")
        derived = {"none": "low", "local": "high", "external": "high", "destructive": "critical"}[contract.side_effect_level]
        risk = max(RISK[derived], RISK.get(str(contract.risk_class).lower(), 0))
        if risk > RISK[policy.get("max_risk", "critical")]:
            raise PermissionError("工具风险超出任务授权")
        resources = list(_resources(args))
        for name, kind in contract.resource_bindings.items():
            value = args
            for part in name.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if value is None:
                value = contract.resource_defaults.get(name)
            if value is None:
                raise PermissionError("无法核验动作对象: " + name)
            resources.append((kind, value))
        for kind, value in resources:
            key = {"path": "path_roots", "target": "targets", "device": "device_ids"}.get(kind)
            if key is None:
                raise PermissionError("未知动作对象类型")
            if key not in policy:
                continue
            if not isinstance(value, str) or not value:
                raise PermissionError("动作对象无法核验")
            if kind == "path":
                path = (Path(contract.resource_base) / value).resolve()
                if not any(_inside(path, Path(root)) for root in policy[key]):
                    raise PermissionError("文件路径超出授权范围")
            elif value not in policy[key]:
                raise PermissionError("操作对象或设备超出授权范围")
        return ref


def _inside(path, root):
    try:
        return os.path.normcase(os.path.commonpath([path, root])) == os.path.normcase(str(root))
    except ValueError:
        return False


def _resources(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in RESOURCE_KEYS:
                yield RESOURCE_KEYS[key], item
            yield from _resources(item)
    elif isinstance(value, list):
        for item in value:
            yield from _resources(item)
