"""ALN-27-C：知识持久化真实注入与迁移质量验证测试。

覆盖：重启保留版本链与撤销；内存旧用法兼容；内存→持久受控迁移（摘要核验/
撤销保真/幂等/冲突拒绝）；知识按适用域与可信度注入上下文并在 manifest 留
来源/版本；撤销与不适用条目绝不进入模型消息；真实任务数据的前后质量对照。
"""

import sqlite3
from types import SimpleNamespace

import pytest

from evoagent.knowledge.store import (KnowledgeEntry, KnowledgeError,
                                      KnowledgeStore, migrate_knowledge)
from evoagent.knowledge.transfer import (evaluate_transfer,
                                         outcomes_from_tasks)
from evoagent.memory.context import ContextAssembler


def entry(entry_id, *, key="timeout", version=1, domain=("coding",),
          confidence=0.9, source="run-42", revoked_at=None):
    return KnowledgeEntry(
        entry_id=entry_id, key=key, content=f"经验-{entry_id}-v{version}",
        source=source, confidence=confidence,
        applicable_domains=tuple(domain), version=version,
        created_at=1000.0 + version, revoked_at=revoked_at,
        revoke_reason="已推翻" if revoked_at else None)


def connect(path):
    conn = sqlite3.connect(path)
    return conn


def test_persistence_keeps_versions_and_revocations(tmp_path):
    path = str(tmp_path / "knowledge.db")
    conn = connect(path)
    store = KnowledgeStore(conn)
    store.add(entry("k1", version=1))
    store.add(entry("k2", version=2))
    store.revoke("k1", reason="结论被推翻", at=2000.0)
    digest = store.digest()
    conn.close()

    reopened = KnowledgeStore(connect(path))
    assert reopened.digest() == digest
    assert reopened.latest("timeout").entry_id == "k2"
    revoked = reopened.get("k1")
    assert revoked.revoked is True and revoked.revoked_at == 2000.0
    assert revoked.revoke_reason == "结论被推翻"
    assert len(reopened.versions_of("timeout")) == 2


def test_in_memory_usage_remains_compatible():
    store = KnowledgeStore()
    store.add(entry("m1"))
    assert store.get("m1").content == "经验-m1-v1"
    assert store.digest()
    with pytest.raises(KnowledgeError):
        store.add(entry("m1"))


def test_in_memory_to_persistent_migration_is_verified_and_idempotent(tmp_path):
    source = KnowledgeStore()
    source.add(entry("k1", version=1))
    source.add(entry("k2", version=2))
    source.revoke("k1", reason="错误经验", at=1500.0)
    target = KnowledgeStore(connect(str(tmp_path / "migrate.db")))

    result = migrate_knowledge(source, target, actor="admin",
                               reason="27-C 迁移")
    assert result["status"] == "migrated"
    assert result["count"] == 2 and result["revoked"] == 1
    assert target.digest() == source.digest()
    assert target.get("k1").revoked is True
    again = migrate_knowledge(source, target, actor="admin", reason="重复")
    assert again["status"] == "already_migrated"


def test_migration_conflicts_and_missing_attribution_are_refused(tmp_path):
    source = KnowledgeStore()
    source.add(entry("k1"))
    with pytest.raises(KnowledgeError):
        migrate_knowledge(source, KnowledgeStore(), actor="", reason="x")
    with pytest.raises(KnowledgeError):
        migrate_knowledge(source, KnowledgeStore(), actor="admin", reason="")

    target = KnowledgeStore()
    target.add(entry("other", key="other"))
    with pytest.raises(KnowledgeError):
        migrate_knowledge(source, target, actor="admin", reason="冲突")
    with pytest.raises(KnowledgeError):
        migrate_knowledge(source, source, actor="admin", reason="同库")


def test_context_injects_applicable_knowledge_with_provenance_only():
    store = KnowledgeStore()
    store.add(entry("good", version=2, confidence=0.9))
    store.add(entry("revoked", key="revoked", version=3, confidence=0.99,
                    revoked_at=1200.0))
    store.add(entry("lowconf", key="lowconf", version=1, confidence=0.2))
    store.add(entry("other-domain", key="other", version=1,
                    domain=("writing",)))
    assembler = ContextAssembler(knowledge_store=store,
                                knowledge_min_confidence=0.5)
    ctx = assembler.assemble("修复超时问题", domain="coding")
    text = " || ".join(ctx.memories)
    assert "knowledge v2" in text and "经验-good-v2" in text
    assert "经验-revoked-v3" not in text
    assert "经验-lowconf-v1" not in text
    assert "经验-other-domain-v1" not in text
    kinds = [(e.kind, e.source, e.source_version) for e in ctx.manifest.entries
             if e.kind == "knowledge"]
    assert kinds == [("knowledge", "run-42", 2)]
    messages = " ".join(m.content for m in ctx.to_messages())
    assert "经验-good-v2" in messages
    assert "经验-revoked-v3" not in messages


def test_real_task_quality_compares_before_and_after_migration():
    before_tasks = [SimpleNamespace(task_id="t1", task_status="succeeded"),
                    SimpleNamespace(task_id="t2", task_status="failed")]
    after_tasks = [SimpleNamespace(task_id="t1", task_status="succeeded"),
                   SimpleNamespace(task_id="t2", task_status="succeeded")]
    before = outcomes_from_tasks(before_tasks, expert="coding")
    after = outcomes_from_tasks(after_tasks, expert="coding")
    report = evaluate_transfer(before, after, hit_rate=0.99)
    assert report.verdict == "improved" and report.basis == "task_quality"
    degraded = evaluate_transfer(after, before)
    assert degraded.verdict == "degraded"
    unproven = evaluate_transfer([], [], hit_rate=1.0)
    assert unproven.verdict == "unproven" and unproven.sufficient is False


def test_app_wires_persistent_knowledge_and_restart_keeps_it(tmp_path):
    from evoagent.app import App

    first = App(data_dir=str(tmp_path))
    try:
        first.knowledge.add(entry("app-entry", version=1))
        assert first.assembler.knowledge is first.knowledge
        assert first.knowledge.get("app-entry") is not None
    finally:
        first.db.close()

    second = App(data_dir=str(tmp_path))
    try:
        restored = second.knowledge.get("app-entry")
        assert restored is not None and restored.version == 1
    finally:
        second.db.close()
