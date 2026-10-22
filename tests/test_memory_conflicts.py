# -*- coding: utf-8 -*-
"""ALN-05：冲突裁决与删除失效链。"""

import json

import pytest

from evoagent.memory.manifest import MemoryIdentity
from evoagent.memory.store import (Credibility, MemoryItem, MemoryKind,
                                   MemoryStore, Scope)

IDENT = MemoryIdentity(user_id="u1", project_id="p1", session_id="s1",
                       task_id="t1")


@pytest.fixture()
def store(tmp_path):
    value = MemoryStore(str(tmp_path / "memory.db"))
    yield value
    value.close()


def _write(store, content, *, fact_key="", source="runtime",
           status=Credibility.VERIFIED, scope=Scope.TASK,
           identity=IDENT):
    return store.write(
        MemoryItem(content=content, kind=MemoryKind.PROJECT_STATE,
                   scope=scope, status=status, source=source,
                   evidence={"verified": True}),
        identity=identity, fact_key=fact_key)


def test_same_fact_key_never_overwrites(store):
    old = _write(store, "端口 8080", fact_key="deploy.port")
    new = _write(store, "端口 9090", fact_key="deploy.port")

    assert old.memory_id != new.memory_id
    old_saved = store.get(old.memory_id)
    new_saved = store.get(new.memory_id)
    assert old_saved.content == "端口 8080"
    assert new_saved.content == "端口 9090"
    assert old_saved.has_conflict and new_saved.has_conflict

    conflicts = store.list_conflicts(status="unresolved")
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["fact_key"] == "deploy.port"
    assert conflict["old_memory_id"] == old.memory_id
    assert conflict["new_memory_id"] == new.memory_id
    assert old_saved.conflict_ids == [conflict["conflict_id"]]
    assert new_saved.conflict_ids == [conflict["conflict_id"]]


def test_rule_prefers_explicit_user_preference(store):
    old = _write(store, "深色主题", fact_key="theme", source="runtime")
    new = _write(store, "浅色主题", fact_key="theme", source="user")
    conflict = store.list_conflicts(status="unresolved")[0]

    resolved = store.adjudicate_conflict(conflict["conflict_id"])
    assert resolved["status"] == "resolved"
    assert resolved["resolution"] == "rule:explicit_user_preference"
    assert resolved["resolved_by"] == "rules"

    loser = store.get(old.memory_id)
    assert loser.status == Credibility.REVOKED
    assert loser.superseded_by == new.memory_id
    assert loser.conflict_ids == [] and loser.has_conflict is False
    assert store.get(new.memory_id).status != Credibility.REVOKED


def test_rule_prefers_higher_credibility(store):
    old = _write(store, "版本 1", fact_key="version",
                 status=Credibility.VERIFIED)
    new = _write(store, "版本 2", fact_key="version",
                 status=Credibility.CANDIDATE)
    conflict = store.list_conflicts(status="unresolved")[0]

    resolved = store.adjudicate_conflict(conflict["conflict_id"])
    assert resolved["resolution"] == "rule:higher_credibility"
    assert store.get(old.memory_id).status == Credibility.VERIFIED
    assert store.get(new.memory_id).superseded_by == old.memory_id


def test_equal_credibility_requires_manual_adjudication(store):
    first = _write(store, "候选 A", fact_key="fact")
    second = _write(store, "候选 B", fact_key="fact")
    conflict = store.list_conflicts(status="unresolved")[0]

    pending = store.adjudicate_conflict(conflict["conflict_id"])
    assert pending["status"] == "unresolved"
    assert "人工" in pending["reason"]

    with pytest.raises(ValueError):
        store.resolve_conflict(conflict["conflict_id"], first.memory_id,
                               actor="", reason="review")
    with pytest.raises(ValueError):
        store.resolve_conflict(conflict["conflict_id"], "not-a-member",
                               actor="admin", reason="review")

    resolved = store.resolve_conflict(
        conflict["conflict_id"], second.memory_id,
        actor="admin", reason="人工核对后采用 B")
    assert resolved["status"] == "resolved"
    assert resolved["resolution"] == "manual"
    assert resolved["resolved_by"] == "admin"
    assert store.get(first.memory_id).status == Credibility.REVOKED
    assert store.get(second.memory_id).status != Credibility.REVOKED


def test_delete_source_tombstones_derived_and_invalidates_conflict(store):
    source = _write(store, "源事实 端口 8080", fact_key="port")
    derived = _write(store, "派生摘要 端口 8080")
    cached = _write(store, "缓存副本 端口 8080")
    unrelated = _write(store, "无关记忆", scope=Scope.SHARED, identity=None)
    store.link(derived.memory_id, "derived_from", source.memory_id)
    store.link(cached.memory_id, "cached_from", source.memory_id)
    survivor = _write(store, "源事实 端口 9090", fact_key="port",
                      source="user")
    conflict = store.list_conflicts(status="unresolved")[0]
    assert survivor.conflict_ids == [conflict["conflict_id"]]

    result = store.delete_with_chain(source.memory_id, actor="admin",
                                     reason="user request")

    assert result["ok"] is True
    assert result["deleted"] == 3
    assert set(result["deleted_ids"]) == {
        source.memory_id, derived.memory_id, cached.memory_id}
    assert store.get(source.memory_id) is None
    assert store.get(derived.memory_id) is None
    assert store.get(cached.memory_id) is None
    assert store.get(unrelated.memory_id) is not None

    tombstones = store.tombstones.list(memory_id=source.memory_id)
    assert {t["memory_id"] for t in tombstones} == {
        source.memory_id, derived.memory_id, cached.memory_id}
    assert all(t["root_memory_id"] == source.memory_id for t in tombstones)
    assert all(t["actor"] == "admin" for t in tombstones)
    root = [t for t in tombstones if t["memory_id"] == source.memory_id][0]
    assert json.loads(root["identity_json"])["user_id"] == "u1"

    invalidated = store.list_conflicts(status="invalidated")
    assert [c["conflict_id"] for c in invalidated] == [
        conflict["conflict_id"]]
    saved = store.get(survivor.memory_id)
    assert saved.conflict_ids == [] and saved.has_conflict is False
    remaining = {m.content for m in store.search(
        "源事实 端口 8080", identity=IDENT, limit=10)}
    assert remaining == {"源事实 端口 9090"}


def test_delete_missing_memory_is_noop(store):
    assert store.delete("missing") is False
