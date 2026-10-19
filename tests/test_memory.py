"""记忆与上下文治理测试（PRD 21-33 / Arch 8）。"""

import shutil
import tempfile
import unittest

from evoagent.memory.context import ContextAssembler, ContextBudget, check_compression
from evoagent.memory.store import (Credibility, MemoryItem, MemoryKind,
                                   MemoryStore, Scope)


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(tempfile.mktemp(dir=self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_requires_evidence_for_verified(self):
        item = self.store.write(MemoryItem(
            content="失败时的推断", status=Credibility.VERIFIED,
            evidence={}))
        self.assertEqual(item.status, Credibility.PENDING_REVIEW)
        ok = self.store.write(MemoryItem(
            content="有证据的经验", status=Credibility.VERIFIED,
            evidence={"eval": "passed"}))
        self.assertEqual(ok.status, Credibility.VERIFIED)

    def test_dedup(self):
        self.store.write(MemoryItem(content="重复内容", scope=Scope.TASK,
                                    scope_key="t1"))
        n = self.store
        again = n.write(MemoryItem(content="重复内容", scope=Scope.TASK,
                                   scope_key="t1"))
        self.assertTrue(again)
        rows = self.store.search("重复内容", scope=Scope.TASK, scope_key="t1")
        contents = [m.content for m in rows]
        self.assertEqual(contents.count("重复内容"), 1)

    def test_revocation_blocks_retrieval(self):
        m = self.store.write(MemoryItem(content="即将撤销的记忆"))
        self.store.set_status(m.memory_id, Credibility.REVOKED, "test")
        self.assertEqual(self.store.search("即将撤销"), [])

    def test_supersede(self):
        old = self.store.write(MemoryItem(content="旧地址是北京"))
        new = self.store.write(MemoryItem(content="新地址是上海"))
        self.store.supersede(old.memory_id, new.memory_id, "地址更新")
        rows = self.store.search("地址")
        self.assertEqual(len(rows), 1)
        self.assertIn("上海", rows[0].content)

    def test_expiry(self):
        import time
        m = self.store.write(MemoryItem(content="限时记忆", valid_until=time.time() - 1))
        self.assertTrue(m.is_expired(time.time()))
        self.assertEqual(self.store.search("限时记忆"), [])


class TestContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(tempfile.mktemp(dir=self.tmp))
        self.assembler = ContextAssembler(self.store)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_assemble_priority_order(self):
        ctx = self.assembler.assemble(
            "目标 G", intent="general", domain="general",
            constraints=["约束1", "约束2"], status="RUNNING")
        msgs = ctx.to_messages()
        # system intent 在最前（含不含硬约束都应保留）
        self.assertTrue(msgs[0].content.startswith("intent:"))
        hard = [m for m in msgs if m.content.startswith("硬约束")]
        self.assertEqual(len(hard), 1)
        self.assertIn("约束1", hard[0].content)

    def test_compression_keeps_constraints(self):
        big_text = "无关内容 " * 500
        self.store.write(MemoryItem(content=big_text))
        self.assembler.budget = ContextBudget(max_chars=150)
        after = self.assembler.assemble("目标", constraints=["必须保留的约束"])
        # 压缩后：硬约束保留（Arch 8.5），记忆被裁剪
        self.assertEqual(after.hard_constraints, ["必须保留的约束"])
        self.assertLessEqual(sum(len(x) for x in after.memories),
                             10 * 500)

    def test_compression_check_reports_loss(self):
        import evoagent.memory.context as C
        b = self.assembler.assemble("目标G", constraints=["C1"])
        a = C.AssembledContext(task_goal="", hard_constraints=[],
                               acceptance_criteria=[])
        dropped = check_compression(b, a)
        self.assertIn("目标丢失", "".join(dropped))


if __name__ == "__main__":
    unittest.main()
