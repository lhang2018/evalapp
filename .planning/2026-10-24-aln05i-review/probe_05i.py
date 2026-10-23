"""ALN-05-I 独立复核探针（claude-main）。

先读 MemoryBindingAuthority 签名再写，验证交付声明最硬的三条：

I1 未知/歧义身份拒绝（不在主体白名单 → 拒绝；同主体多条配置时部分选择须唯一命中）；
I2 memory_context 不接受 task/expert/grant/principal_id 等身份字段
   （直接填身份式对象不能创建权限）；
I3 配置中移除原三元组后，旧绑定的执行也拒绝（不把旧任务改绑给新用户）；
I4 省略 memory_context 时采用该主体第一条配置（护栏：正常可用）。

判别力：功能前基点（04de43f）失败 / 05a7cf4 通过。
"""
import sys
import os

results = []


def probe(name, fn):
    try:
        fn()
        results.append((name, "PASS"))
    except (AssertionError, ValueError, TypeError, KeyError, RuntimeError,
            PermissionError) as exc:
        results.append((name, f"FAIL: {type(exc).__name__}: {str(exc)[:90]}"))


def _authority(policies):
    from evoagent.memory.binding import MemoryBindingAuthority
    from evoagent.core.persistence import Database
    import tempfile
    db = Database(os.path.join(tempfile.mkdtemp(prefix="bind-"), "b.db"))
    return MemoryBindingAuthority(db, policies), db


POLICY = {
    "local:host": [
        {"user_id": "alice", "project_id": "p1", "session_id": "s1"},
        {"user_id": "bob", "project_id": "p2", "session_id": "s2"},
    ],
}


# ---- I1 未知/歧义身份拒绝 ----
def i1_unknown_identity_rejected():
    from evoagent.memory.binding import MemoryAccessError
    auth, db = _authority(POLICY)
    try:
        auth.submission_identity(
            "local:host", {"user_id": "carol", "project_id": "p1",
                           "session_id": "s1"})
    except MemoryAccessError:
        db.close()
        return
    db.close()
    raise AssertionError("不在白名单内的身份被接受")


def i1b_ambiguous_identity_rejected():
    """部分选择必须唯一命中：只给 session_id=s1，若两条配置共享该值则歧义拒绝。"""
    from evoagent.memory.binding import MemoryAccessError
    ambiguous = {
        "local:host": [
            {"user_id": "alice", "project_id": "p1", "session_id": "same"},
            {"user_id": "bob", "project_id": "p2", "session_id": "same"},
        ],
    }
    auth, db = _authority(ambiguous)
    try:
        auth.submission_identity("local:host", {"session_id": "same"})
    except MemoryAccessError:
        db.close()
        return
    db.close()
    raise AssertionError("歧义的部分身份选择被接受（未要求唯一命中）")


def i1c_unconfigured_principal_rejected():
    from evoagent.memory.binding import MemoryAccessError
    auth, db = _authority(POLICY)
    try:
        auth.submission_identity("api:admin", None)
    except MemoryAccessError:
        db.close()
        return
    db.close()
    raise AssertionError("未配置主体的请求被接受")


# ---- I2 memory_context 不接受身份字段 ----
def i2_identity_fields_rejected():
    from evoagent.memory.binding import MemoryAccessError
    auth, db = _authority(POLICY)
    bad = [
        {"user_id": "alice", "project_id": "p1", "session_id": "s1",
         "task_id": "forged-task"},
        {"user_id": "alice", "project_id": "p1", "session_id": "s1",
         "expert_id": "forged-expert"},
        {"user_id": "alice", "project_id": "p1", "session_id": "s1",
         "principal_id": "api:admin"},
        {"user_id": "alice", "project_id": "p1", "session_id": "s1",
         "grant": {"scope": "user"}},
        {"memory_identity": {"user_id": "alice"}},
    ]
    for payload in bad:
        try:
            auth.submission_identity("local:host", payload)
        except MemoryAccessError:
            continue
        except (ValueError, TypeError):
            continue
        db.close()
        raise AssertionError(f"含身份字段的请求被接受: {payload}")
    db.close()


# ---- I3 配置移除后旧绑定拒绝 ----
def i3_removed_policy_rejects_old_binding():
    from evoagent.memory.binding import MemoryAccessError
    auth, db = _authority(POLICY)
    row = {
        "task_id": "t1", "principal_id": "local:host", "binding_ref": "ref1",
        "data": '{"legacy": false, "identity": {"user_id": "alice",'
                ' "project_id": "p1", "session_id": "s1"}}',
    }
    with db._lock:
        db._conn.execute(
            "INSERT INTO task_memory_bindings VALUES (?,?,?,?)",
            (row["task_id"], row["principal_id"], row["binding_ref"],
             row["data"]))
        db._conn.commit()
    # 主体仍在，但该三元组已从策略中移除
    stripped = {"local:host": [{"user_id": "bob", "project_id": "p2",
                                "session_id": "s2"}]}
    auth.policies = stripped
    try:
        auth._check_current_policy(row)
    except MemoryAccessError:
        db.close()
        return
    db.close()
    raise AssertionError("策略移除原三元组后旧绑定仍被允许（会把旧任务改绑）")


# ---- I4 省略时采用第一条配置（护栏） ----
def i4_omitted_uses_first_choice():
    auth, db = _authority(POLICY)
    got = auth.submission_identity("local:host", None)
    assert got.user_id == "alice", (
        f"省略 memory_context 未采用主体第一条配置: {got}")
    db.close()


probe("I1 unknown-identity-rejected", i1_unknown_identity_rejected)
probe("I1b ambiguous-rejected", i1b_ambiguous_identity_rejected)
probe("I1c unconfigured-principal-rejected", i1c_unconfigured_principal_rejected)
probe("I2 identity-fields-rejected", i2_identity_fields_rejected)
probe("I3 removed-policy-rejects-old-binding", i3_removed_policy_rejects_old_binding)
probe("I4 omitted-uses-first[guard]", i4_omitted_uses_first_choice)

print("=" * 56)
for name, status in results:
    print(f"{status:8} {name}")
failed = [n for n, s in results if s.startswith("FAIL")]
print("=" * 56)
print(f"total={len(results)} pass={len(results)-len(failed)} fail={len(failed)}")
sys.exit(1 if failed else 0)
