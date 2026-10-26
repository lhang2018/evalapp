"""ALN-24-F：多主体记忆范围策略与迁移隔离测试。

覆盖：主体由服务端鉴权决定（拒绝按 body 选主体）、默认单用户不变、
缺失/损坏旧策略拒绝默认放宽、同 scope 不同主体互不影响、迁移 dry-run/
幂等/冲突拒绝/回退核对、重启持久化、HTTP 端点角色与主体校验。
"""

import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.memory.scope_policy import (POLICY_TABLE,
                                          PrincipalMismatch, ScopePolicyError,
                                          ScopePolicyStore)


def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    return connection


def seed_legacy(connection, allow=("user", "project", "task"), deny=()):
    connection.execute(POLICY_TABLE)
    connection.execute(
        "INSERT OR REPLACE INTO memory_scope_policy VALUES (1,?,?,?,?,?)",
        (json.dumps(sorted(allow)), json.dumps(sorted(deny)),
         "seed", "test", 1.0))
    connection.commit()


def corrupt_legacy(connection):
    connection.execute(POLICY_TABLE)
    connection.execute(
        "INSERT OR REPLACE INTO memory_scope_policy VALUES (1,?,?,?,?,?)",
        ("not-json", "[]", "seed", "test", 1.0))
    connection.commit()


def test_missing_legacy_policy_refuses_default_open():
    store = ScopePolicyStore(conn())
    with pytest.raises(ScopePolicyError):
        store.effective_policy("user-a")
    lax = store.effective_policy("user-a", strict=False)
    assert lax["source"] == "legacy_missing"
    assert lax["deny"] == list(("user", "project", "session", "task",
                                "expert", "model", "shared"))


def test_corrupt_legacy_policy_is_reported_not_open():
    connection = conn()
    corrupt_legacy(connection)
    store = ScopePolicyStore(connection)
    legacy = store.legacy_policy()
    assert legacy["present"] is True and legacy["valid"] is False
    assert legacy["source"] == "legacy_corrupt"
    with pytest.raises(ScopePolicyError):
        store.migrate_legacy(authenticated="admin", reason="迁移")


def test_single_user_default_uses_legacy_policy():
    connection = conn()
    seed_legacy(connection, allow=("user", "task"), deny=("shared",))
    store = ScopePolicyStore(connection)
    policy = store.effective_policy("local:host")
    assert policy["partitioned"] is False
    assert policy["source"] == "legacy"
    assert policy["allow"] == ["task", "user"]
    assert policy["deny"] == ["shared"]


def test_two_principals_same_scope_are_isolated():
    connection = conn()
    seed_legacy(connection)
    store = ScopePolicyStore(connection)
    store.set_principal_policy(authenticated="user-a", multi_principal=True,
                               allow=["user"], deny=["shared"], actor="admin",
                               reason="A 策略")
    store.set_principal_policy(authenticated="user-b", multi_principal=True,
                               allow=["task"], deny=["user"], actor="admin",
                               reason="B 策略")
    a = store.effective_policy("user-a")
    b = store.effective_policy("user-b")
    assert a["partitioned"] is True and b["partitioned"] is True
    assert store.scope_visible("user-a", "user") is True
    assert store.scope_visible("user-a", "shared") is False
    assert store.scope_visible("user-b", "user") is False
    assert store.scope_visible("user-b", "task") is True
    assert store.scope_visible("user-b", "shared") is False
    assert store.principal_policy("user-a")["digest"] != \
        store.principal_policy("user-b")["digest"]


def test_request_body_cannot_select_other_principal():
    connection = conn()
    connection.execute(POLICY_TABLE)
    connection.commit()
    store = ScopePolicyStore(connection)
    with pytest.raises(PrincipalMismatch):
        store.set_principal_policy(authenticated="user-a", requested="user-b",
                                   multi_principal=True, allow=["user"],
                                   deny=[], actor="admin", reason="x")
    with pytest.raises(PrincipalMismatch):
        store.resolve_principal("user-a", requested="user-b")
    assert store.resolve_principal("user-a", requested="user-a") == "user-a"


def test_policy_validation_rejects_bad_input():
    connection = conn()
    seed_legacy(connection)
    store = ScopePolicyStore(connection)
    with pytest.raises(ScopePolicyError):
        store.set_principal_policy(authenticated="user-a", multi_principal=True,
                                   allow=["user"], deny=["user"], actor="a",
                                   reason="r")
    with pytest.raises(ScopePolicyError):
        store.set_principal_policy(authenticated="user-a", multi_principal=True,
                                   allow=[], deny=[], actor="a", reason="r")
    with pytest.raises(ScopePolicyError):
        store.set_principal_policy(authenticated="user-a", multi_principal=True,
                                   allow=["nope"], deny=[], actor="a",
                                   reason="r")
    with pytest.raises(ScopePolicyError):
        store.set_principal_policy(authenticated="user-a",
                                   multi_principal="yes", allow=["user"],
                                   deny=[], actor="a", reason="r")


def test_migration_dry_run_apply_idempotent_and_conflict():
    connection = conn()
    seed_legacy(connection, allow=("user", "project"), deny=("shared",))
    store = ScopePolicyStore(connection)
    dry = store.migrate_legacy(authenticated="admin", dry_run=True,
                               reason="预演")
    assert dry["status"] == "dry_run"
    assert store.principal_policy("admin") is None
    applied = store.migrate_legacy(authenticated="admin", dry_run=False,
                                   reason="迁移")
    assert applied["status"] == "applied"
    policy = store.principal_policy("admin")
    assert policy["multi_principal"] is True
    assert policy["allow"] == ["project", "user"]
    assert policy["deny"] == ["shared"]
    again = store.migrate_legacy(authenticated="admin", dry_run=False,
                                 reason="重复")
    assert again["status"] == "already_migrated"
    seed_legacy(connection, allow=("task",), deny=())
    with pytest.raises(ScopePolicyError):
        store.migrate_legacy(authenticated="admin", dry_run=False,
                             reason="冲突")


def test_rollback_restores_legacy_and_refuses_tamper():
    connection = conn()
    seed_legacy(connection, allow=("user",), deny=())
    store = ScopePolicyStore(connection)
    store.migrate_legacy(authenticated="admin", dry_run=False, reason="迁移")
    rolled = store.rollback_migration(authenticated="admin", reason="回退")
    assert rolled["ok"] is True
    assert rolled["verification"] == {"partitioned": False, "source": "legacy"}
    assert store.principal_policy("admin") is None
    assert store.effective_policy("admin")["source"] == "legacy"
    with pytest.raises(ScopePolicyError):
        store.rollback_migration(authenticated="admin", reason="再次回退")

    store.migrate_legacy(authenticated="admin", dry_run=False, reason="再迁移")
    store.set_principal_policy(authenticated="admin", multi_principal=True,
                               allow=["task"], deny=[], actor="admin",
                               reason="迁移后修改")
    with pytest.raises(ScopePolicyError):
        store.rollback_migration(authenticated="admin", reason="篡改后回退")


def test_migration_history_records_actions():
    connection = conn()
    seed_legacy(connection)
    store = ScopePolicyStore(connection)
    store.migrate_legacy(authenticated="admin", dry_run=True, reason="预演")
    store.migrate_legacy(authenticated="admin", dry_run=False, reason="迁移")
    store.rollback_migration(authenticated="admin", reason="回退")
    actions = [row["action"] for row in
               store.migration_history("admin")["migrations"]]
    assert actions == ["dry_run", "apply", "rollback"]


def test_policies_survive_reopen(tmp_path):
    path = str(tmp_path / "memory.db")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    seed_legacy(connection)
    ScopePolicyStore(connection).set_principal_policy(
        authenticated="user-a", multi_principal=True, allow=["user"],
        deny=["shared"], actor="admin", reason="持久化")
    connection.close()
    reopened = sqlite3.connect(path)
    reopened.row_factory = sqlite3.Row
    store = ScopePolicyStore(reopened)
    policy = store.effective_policy("user-a")
    assert policy["partitioned"] is True
    assert policy["deny"] == ["shared"]


# ---------------- HTTP 端点 ----------------
@pytest.fixture
def api(tmp_path):
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "op"
    app.config.api_admin_token = "root"
    conn_ = app.memory._conn
    seed_legacy(conn_)
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, f"http://127.0.0.1:{server.port}"
    server.shutdown()
    app.db.close()


def call(base, method, path, payload=None, token="root"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_http_principal_policy_roles_and_mismatch(api):
    app, base = api
    status, payload = call(base, "GET", "/memory/principal-policy")
    assert status == 200 and payload["source"] == "legacy"
    status, _ = call(base, "GET",
                     "/memory/principal-policy?principal=other-user")
    assert status == 403
    status, payload = call(base, "PUT", "/memory/principal-policy", {
        "multi_principal": True, "allow": ["user"], "deny": ["shared"],
        "reason": "多主体"}, token="root")
    assert status == 200 and payload["policy"]["multi_principal"] is True
    status, _ = call(base, "PUT", "/memory/principal-policy", {
        "multi_principal": True, "allow": ["user"], "deny": [],
        "reason": "operator 不应允许"}, token="op")
    assert status == 403
    status, payload = call(base, "GET", "/memory/principal-policy")
    assert payload["partitioned"] is True


def test_http_migration_and_rollback(api):
    app, base = api
    # 新实例无分区策略：先 dry-run 再 apply
    status, payload = call(base, "POST", "/memory/principal-policy/migrate",
                           {"dry_run": True, "reason": "预演"})
    assert status == 200 and payload["status"] == "dry_run"
    status, payload = call(base, "POST", "/memory/principal-policy/migrate",
                           {"dry_run": False, "reason": "迁移"})
    assert status == 200 and payload["status"] == "applied"
    status, history = call(base, "GET",
                           "/memory/principal-policy/migrations")
    assert status == 200 and len(history["migrations"]) == 2
    status, payload = call(base, "POST", "/memory/principal-policy/rollback",
                           {"reason": "回退"})
    assert status == 200 and payload["verification"]["source"] == "legacy"
    status, _ = call(base, "POST", "/memory/principal-policy/migrate",
                     {"dry_run": False, "reason": "x"}, token="op")
    assert status == 403
