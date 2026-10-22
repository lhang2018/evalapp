# -*- coding: utf-8 -*-
"""ALN-05：身份作用域隔离与不可变 ContextManifest。"""

import json

import pytest

from evoagent.memory.context import ContextAssembler, ContextBudget
from evoagent.memory.manifest import (AccessGrant, ContextManifest,
                                      MemoryAccessError, MemoryIdentity,
                                      make_entry)
from evoagent.memory.store import (Credibility, MemoryItem, MemoryKind,
                                   MemoryStore, Scope)


def _ident(**kwargs) -> MemoryIdentity:
    return MemoryIdentity(**kwargs)


@pytest.fixture()
def store(tmp_path):
    value = MemoryStore(str(tmp_path / "memory.db"))
    yield value
    value.close()


def _write(store, content, *, identity=None, scope=Scope.TASK, key="",
           kind=MemoryKind.DOMAIN_KNOWLEDGE,
           status=Credibility.VERIFIED, domains=None, fact_key="",
           source="runtime"):
    item = MemoryItem(
        content=content, kind=kind, scope=scope, scope_key=key,
        status=status, source=source,
        applicable_domains=list(domains or []),
        evidence={"verified": True})
    return store.write(item, identity=identity, fact_key=fact_key)


def test_identity_scopes_isolate_and_inherit_broader(store):
    alice = _ident(user_id="u1", project_id="p1", session_id="s1",
                   task_id="t1")
    bob = _ident(user_id="u2", project_id="p2", session_id="s2",
                 task_id="t2")
    for scope in (Scope.USER, Scope.PROJECT, Scope.SESSION, Scope.TASK):
        _write(store, f"alpha deploy {scope.value} 经验", identity=alice,
               scope=scope)
    _write(store, "beta deploy 私有经验", identity=bob, scope=Scope.TASK)
    _write(store, "alpha shared 通用偏好", scope=Scope.SHARED,
           kind=MemoryKind.USER_PREFERENCE)
    _write(store, "alpha owned shared", identity=alice, scope=Scope.SHARED)
    # 未声明身份归属的条目不得泄漏给身份读
    _write(store, "alpha unowned task", scope=Scope.TASK, key="t9")

    got = {m.content for m in store.search("alpha deploy 经验",
                                           identity=alice, limit=10)}
    assert "alpha deploy user 经验" in got
    assert "alpha deploy project 经验" in got
    assert "alpha deploy session 经验" in got
    assert "alpha deploy task 经验" in got
    assert "alpha shared 通用偏好" in got
    assert "alpha owned shared" in got
    assert "beta deploy 私有经验" not in got
    assert "alpha unowned task" not in got

    bob_got = {m.content for m in store.search("alpha deploy 经验",
                                               identity=bob, limit=10)}
    assert "alpha deploy task 经验" not in bob_got
    assert "alpha owned shared" not in bob_got
    assert "alpha shared 通用偏好" in bob_got


def test_expert_hive_requires_explicit_expert_id(store):
    expert = _ident(user_id="u1", expert_id="e1")
    plain = _ident(user_id="u1")
    _write(store, "alpha expert hive 经验", identity=expert,
           scope=Scope.EXPERT)
    assert store.search("alpha expert hive 经验", identity=expert)
    assert store.search("alpha expert hive 经验", identity=plain) == []
    with pytest.raises(MemoryAccessError):
        store.search("alpha expert hive 经验", scope=Scope.EXPERT,
                     scope_key="e1", identity=plain)


def test_cross_scope_read_requires_audited_grant(store):
    owner = _ident(user_id="u2", task_id="t2")
    item = _write(store, "other tenant alpha", identity=owner)
    viewer = _ident(user_id="u1", task_id="t1")

    assert store.search("other tenant alpha", identity=viewer) == []
    with pytest.raises(MemoryAccessError):
        store.get(item.memory_id, identity=viewer)

    grant = AccessGrant(scopes=("task",), identity=owner,
                        actor="incident-admin", reason="incident review")
    loaded = store.get(item.memory_id, identity=viewer, grant=grant)
    assert loaded is not None and loaded.content == "other tenant alpha"
    assert store.search("other tenant alpha", identity=viewer, grant=grant)

    with pytest.raises(ValueError):
        AccessGrant(scopes=("task",), identity=owner, actor="",
                    reason="incident review")
    with pytest.raises(ValueError):
        AccessGrant(scopes=("tenant",), identity=owner, actor="a",
                    reason="r")


def test_list_items_respects_identity_scope(store):
    owner = _ident(user_id="u2", task_id="t2")
    mine = _ident(user_id="u1", task_id="t1")
    _write(store, "mine alpha", identity=mine)
    _write(store, "theirs alpha", identity=owner)
    contents = {m.content for m in store.list_items(identity=mine)}
    assert contents == {"mine alpha"}


def test_manifest_digest_is_reproducible_and_independent_of_time():
    entries = [make_entry("goal", "same input", reason="任务目标")]
    first = ContextManifest.build(assembly={"intent": "x"}, budget_tokens=10,
                                  entries=entries, compression={},
                                  created_at=1.0)
    second = ContextManifest.build(assembly={"intent": "x"}, budget_tokens=10,
                                   entries=entries, compression={},
                                   created_at=2.0)
    assert first.digest == second.digest
    assert first.manifest_id == second.manifest_id


def test_assemble_manifest_is_reproducible_and_body_free(store):
    ident = _ident(user_id="u1", task_id="t1")
    memory = _write(store, "mobile 部署经验 回滚", identity=ident,
                    domains=["mobile"])
    assembler = ContextAssembler(store, ContextBudget(max_chars=6000))
    options = dict(intent="operate", domain="mobile",
                   constraints=["不要删除数据"], status="running",
                   acceptance=[{"id": "a1", "desc": "完成"}],
                   recent_results=["step done"], tool_docs=["open_app"],
                   identity=ident)

    first = assembler.assemble("mobile 部署经验 回滚", **options)
    second = assembler.assemble("mobile 部署经验 回滚", **options)
    assert first.manifest is not None
    assert first.manifest.digest == second.manifest.digest
    assert first.manifest.manifest_id == second.manifest.manifest_id
    assert first.stats()["manifest_digest"] == first.manifest.digest
    assert first.manifest.references()
    memory_entry = [e for e in first.manifest.entries
                    if e.kind == "memory"][0]
    assert memory_entry.source_id == memory.memory_id
    assert memory_entry.source_version == memory.source_version
    assert memory_entry.reason == "relevance_retrieval"

    payload = json.dumps(first.manifest.to_dict(), ensure_ascii=False)
    assert "mobile 部署经验 回滚" not in payload
    assert "step done" not in payload
    assert "open_app" not in payload

    changed = assembler.assemble("另一个目标", intent="operate",
                                 domain="mobile", identity=ident)
    assert changed.manifest.digest != first.manifest.digest


def test_manifest_marks_conflicts_without_storing_body(store):
    ident = _ident(user_id="u1", task_id="t1")
    _write(store, "部署端口 8080", identity=ident, fact_key="deploy.port",
           domains=["mobile"])
    _write(store, "部署端口 9090", identity=ident, fact_key="deploy.port",
           domains=["mobile"], source="user")
    ctx = ContextAssembler(store).assemble(
        "部署端口 8080 9090", domain="mobile", identity=ident)

    conflicts = ctx.manifest.conflict_ids()
    assert conflicts
    memory_entries = [e for e in ctx.manifest.entries if e.kind == "memory"]
    assert any(e.conflict_ids for e in memory_entries)
    assert "部署端口 8080" not in json.dumps(ctx.manifest.to_dict(),
                                             ensure_ascii=False)


def test_compression_keeps_constraints_and_marks_dropped_refs(store):
    ident = _ident(user_id="u1", task_id="t1")
    for index in range(3):
        _write(store, f"mobile deploy 步骤{index} " + "x" * 60,
               identity=ident, domains=["mobile"])
    assembler = ContextAssembler(store, ContextBudget(max_chars=260))
    ctx = assembler.assemble(
        "mobile deploy 步骤", domain="mobile", identity=ident,
        constraints=["硬约束A"], acceptance=[{"id": "a1"}],
        recent_results=["结果" + "y" * 80 for _ in range(3)])

    assert ctx.hard_constraints == ["硬约束A"]
    assert ctx.acceptance_criteria == [{"id": "a1"}]
    entries = list(ctx.manifest.entries)
    assert all(e.included for e in entries if e.kind == "constraint")
    assert all(e.included for e in entries if e.kind == "acceptance")
    dropped = [e for e in entries if not e.included]
    assert dropped
    pre_refs = set(ctx.manifest.compression["pre_compress_refs"])
    assert pre_refs == {e.entry_id for e in dropped}
    assert ctx.manifest.compression["applied"] is True


def test_manifest_keeps_reference_after_source_is_deleted(store):
    ident = _ident(user_id="u1", task_id="t1")
    memory = _write(store, "mobile 待删除经验", identity=ident,
                    domains=["mobile"])
    ctx = ContextAssembler(store).assemble(
        "mobile 待删除经验", domain="mobile", identity=ident)
    entry = [e for e in ctx.manifest.entries if e.kind == "memory"][0]
    assert entry.source_id == memory.memory_id

    result = store.delete_with_chain(memory.memory_id, actor="admin",
                                     reason="user erase request")
    assert result["ok"] is True
    assert store.get(memory.memory_id) is None
    assert store.search("mobile 待删除经验", identity=ident) == []

    # 清单保留装配当时的引用与摘要，但不含正文
    assert entry.entry_id in ctx.manifest.references()
    assert entry.source_id == memory.memory_id
    assert "mobile 待删除经验" not in json.dumps(
        ctx.manifest.to_dict(), ensure_ascii=False)
