"""Server-owned memory identity; task/request snapshots never grant access."""
import copy
import json
import math
import time
import uuid
from dataclasses import replace
from ..core.leases import guard_write
from .manifest import AccessGrant, MemoryAccessError, MemoryIdentity

_FIELDS = {"user_id", "project_id", "session_id"}
_DEFAULTS = {name: [{"user_id": name, "project_id": "default:"+name,
                     "session_id": "default:"+name}]
             for name in ("local:host", "api:operator", "api:admin")}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class MemoryBindingAuthority:
    def __init__(self, db, policies=None, clock=time.time):
        self.db, self.clock = db, clock
        self.policies = copy.deepcopy(_DEFAULTS if not policies else policies)
        if not isinstance(self.policies, dict):
            raise ValueError("memory_bindings must map server principals to identity lists")
        for principal, choices in self.policies.items():
            if not isinstance(principal, str) or not principal or not isinstance(choices, list) or not choices:
                raise ValueError("Invalid memory principal policy")
            for identity in choices:
                if not isinstance(identity, dict) or set(identity) != _FIELDS:
                    raise ValueError("Memory policy requires user/project/session")
                checked = MemoryIdentity(**identity)
                if not all(getattr(checked, name) for name in _FIELDS):
                    raise ValueError("Memory policy identity cannot be empty")

    def submission_identity(self, principal, requested=None):
        choices = self.policies.get(principal)
        if not choices:
            raise MemoryAccessError("服务主体未配置记忆身份")
        if requested is None:
            return MemoryIdentity(**choices[0])
        if not isinstance(requested, dict) or set(requested)-_FIELDS:
            raise MemoryAccessError("memory_context仅允许user/project/session，不接受task/expert/grant")
        MemoryIdentity(**requested)
        matches = [choice for choice in choices if all(choice[k] == v for k,v in requested.items())]
        if len(matches) != 1:
            raise MemoryAccessError("请求身份不在主体白名单内或不唯一")
        return MemoryIdentity(**matches[0])

    def _row(self, task_id):
        with self.db._lock:
            row = self.db._conn.execute("SELECT * FROM task_memory_bindings WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def _check_current_policy(self, row):
        data = json.loads(row["data"])
        if not data["legacy"]:
            self.submission_identity(row["principal_id"], {k:data["identity"][k] for k in _FIELDS})
        return data

    def _save(self, task, principal, identity, grant_ref=None, *, legacy=False):
        payload = dict(identity=identity.to_dict(), grant_ref=grant_ref, legacy=legacy)
        with self.db._lock:
            guard_write(self.db)
            if not self.db._conn.in_transaction:
                self.db._conn.execute("BEGIN IMMEDIATE")
            try:
                previous = self._row(task.task_id)
                if previous:
                    if previous["principal_id"] != principal or json.loads(previous["data"]) != payload:
                        raise MemoryAccessError("任务记忆身份不可重新绑定")
                    ref = previous["binding_ref"]
                else:
                    ref = "mb_" + uuid.uuid4().hex
                    self.db._conn.execute("INSERT INTO task_memory_bindings VALUES (?,?,?,?)",
                        (task.task_id, ref, principal, _json(payload)))
                stored = self.db._conn.execute("SELECT data FROM tasks WHERE task_id=?", (task.task_id,)).fetchone()
                if stored is not None:
                    snapshot = json.loads(stored["data"])
                    snapshot.update(memory_binding_ref=ref, memory_identity=identity.to_dict())
                    self.db._conn.execute("UPDATE tasks SET data=? WHERE task_id=?", (_json(snapshot), task.task_id))
                self.db._conn.commit()
            except BaseException:
                self.db._conn.rollback()
                raise
        task.memory_binding_ref = ref
        task.memory_identity = identity.to_dict()
        return ref

    def bind_new(self, task, principal="local:host", requested=None, grant_ref=None):
        identity = replace(self.submission_identity(principal, requested), task_id=task.task_id)
        if grant_ref is not None:
            self._grant(grant_ref, principal, identity)
        self._save(task, principal, identity, grant_ref)

    def resolve(self, task, expert_key=""):
        row = self._row(task.task_id)
        if row is None:
            if task.memory_binding_ref or task.memory_identity:
                raise MemoryAccessError("任务记忆绑定缺失，拒绝无身份回退")
            # No blanket ownership is inferred for historic tasks or memories.
            owner = "legacy:" + task.task_id
            identity = MemoryIdentity(user_id=owner, project_id=owner,
                                      session_id=owner, task_id=task.task_id)
            self._save(task, "legacy", identity, legacy=True)
            row = self._row(task.task_id)
        data = self._check_current_policy(row)
        # Another process may have privately migrated the same old task after
        # this caller loaded its pre-migration JSON. Adopt only the recorded
        # legacy owner; modern snapshots may never be reconstructed by omission.
        if (not task.memory_binding_ref and not task.memory_identity and data["legacy"]
                and row["principal_id"] == "legacy" and data["grant_ref"] is None):
            self._save(task, "legacy", MemoryIdentity(**data["identity"]), legacy=True)
        if task.memory_binding_ref != row["binding_ref"] or task.memory_identity != data["identity"]:
            raise MemoryAccessError("Task身份快照与权威绑定不一致")
        identity = MemoryIdentity(**data["identity"])
        grant = self._grant(data["grant_ref"], row["principal_id"], identity) if data["grant_ref"] else None
        return replace(identity, expert_id=expert_key), grant

    def inherit(self, parent, child):
        identity, _ = self.resolve(parent)
        row = self._row(parent.task_id)
        data = json.loads(row["data"])
        self._save(child, row["principal_id"], replace(identity, task_id=child.task_id),
                   data["grant_ref"], legacy=data["legacy"])

    def assert_principal(self, task, principal, *, admin=False):
        if admin:
            return
        row = self._row(task.task_id) if task is not None else None
        if row is None or row["principal_id"] != principal:
            raise MemoryAccessError("任务不属于当前服务主体")
        self._check_current_policy(row)

    def owns(self, task_id, principal):
        row = self._row(task_id)
        if row is None or row["principal_id"] != principal:
            return False
        try:
            self._check_current_policy(row)
        except MemoryAccessError:
            return False
        return True

    def check_replay(self, task, principal, requested=None, grant_ref=None):
        self.assert_principal(task, principal)
        identity, _ = self.resolve(task)
        expected = self.submission_identity(principal, requested)
        stored = json.loads(self._row(task.task_id)["data"])
        if stored["grant_ref"] != grant_ref or any(getattr(identity,k) != getattr(expected,k) for k in _FIELDS):
            raise MemoryAccessError("请求重放的身份或Grant不一致")

    def issue_grant(self, principal, grantee, target, scopes, *, actor="local:host", reason, expires_at):
        # Host-only service: no HTTP body can invoke this method or supply actor.
        if actor not in {"local:host", "api:admin"}:
            raise MemoryAccessError("跨域授权需受信管理员签发")
        if not isinstance(grantee, MemoryIdentity) or not isinstance(target, MemoryIdentity):
            raise ValueError("Grant requires complete identities")
        self.submission_identity(principal, {k:getattr(grantee,k) for k in _FIELDS})
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int,float)) or not math.isfinite(expires_at) or expires_at <= self.clock():
            raise ValueError("Grant expiry must be finite and in the future")
        grant = AccessGrant(tuple(scopes), target, actor, reason)
        grant.scope_map()
        ref = "mg_" + uuid.uuid4().hex
        data = dict(grantee={k:getattr(grantee,k) for k in _FIELDS}, grant=grant.to_dict())
        with self.db._lock:
            guard_write(self.db)
            self.db._conn.execute("INSERT INTO memory_access_grants VALUES (?,?,?,0,?)",
                                  (ref, principal, expires_at, _json(data)))
            self.db._conn.commit()
        return ref

    def revoke_grant(self, ref, *, actor="local:host"):
        if actor not in {"local:host", "api:admin"}:
            raise MemoryAccessError("跨域授权撤销需受信管理员")
        with self.db._lock:
            guard_write(self.db)
            self.db._conn.execute("UPDATE memory_access_grants SET revoked=1 WHERE grant_ref=?", (ref,))
            self.db._conn.commit()

    def _grant(self, ref, principal, identity):
        if not isinstance(ref, str):
            raise MemoryAccessError("Grant必须引用服务端签发记录")
        with self.db._lock:
            row = self.db._conn.execute("SELECT * FROM memory_access_grants WHERE grant_ref=?", (ref,)).fetchone()
        if row is None or row["revoked"] or row["expires_at"] <= self.clock() or row["principal_id"] != principal:
            raise MemoryAccessError("跨域Grant不存在、已撤销、过期或不属于当前主体")
        data = json.loads(row["data"])
        if data["grantee"] != {k:getattr(identity,k) for k in _FIELDS}:
            raise MemoryAccessError("跨域Grant不属于当前user/project/session")
        grant = data["grant"]
        return AccessGrant(tuple(grant["scopes"]), MemoryIdentity(**grant["identity"]), grant["actor"], grant["reason"])
