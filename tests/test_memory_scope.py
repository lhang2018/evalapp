# -*- coding: utf-8 -*-
"""memory_scope_key 死字段修复（GAP 死字段修复③）：检索隔离真实生效。

原状（M-01..05）：ExpertSpec.memory_scope_key 四专家赋值齐全，但
- store.search 只有在调用方同时给 scope 时才应用键过滤；
- controller 读侧传 scope_key=task.task_id（写侧根本没有任何记忆带该键）；
- `or not m.scope_key` 空键放行使带键记忆与任意键混读。
现口径：带键条目必须键一致；空键条目仅 shared（横切通用层）放行；
读侧（_run_react）与写侧（_remember）同键（专家 memory_scope_key）。
"""

import shutil
import tempfile
import unittest

import evoagent.core.task as T
from evoagent.app import App
from evoagent.memory.store import (Credibility, MemoryItem, MemoryKind,
                                   MemoryStore, Scope)


class TestScopeKeyIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_skey_")
        self.store = MemoryStore(tempfile.mktemp(dir=self.tmp))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, content, scope=Scope.EXPERT, key=""):
        item = self.store.write(MemoryItem(
            content=content, kind=MemoryKind.DOMAIN_KNOWLEDGE,
            scope=scope, scope_key=key,
            status=Credibility.VERIFIED, evidence={"t": 1}))
        self.store._embed_texts([item.content])  # 造词重叠可见性
        return item

    def test_keyed_memory_needs_matching_key(self):
        self._write("coding 独家经验 python", key="coding")
        got = [m.content for m in
               self.store.search("coding 独家经验 python", scope_key="coding")]
        self.assertIn("coding 独家经验 python", got)
        # 错键不可见
        got2 = [m.content for m in
                self.store.search("coding 独家经验 python", scope_key="travel")]
        self.assertNotIn("coding 独家经验 python", got2)

    def test_empty_key_never_passes_non_shared(self):
        # expert 域空键条目：归属未声明 → 不再空键放行
        self._write("未声明归属的专家域记忆", scope=Scope.EXPERT, key="")
        rows = self.store.search("未声明归属的专家域记忆", scope_key="anything")
        self.assertEqual(rows, [])

    def test_shared_empty_key_passes_any_key(self):
        # shared 空键 = 横切通用层（如用户偏好），在任意键下可见
        self._write("用户偏好深色主题", scope=Scope.SHARED, key="")
        rows = self.store.search("用户偏好深色主题", scope_key="coding")
        self.assertTrue(rows)

    def test_no_scope_and_no_key_unchanged(self):
        # 请求无键 → 不参与隔离（既有行为不变）
        self._write("任意条目", scope=Scope.EXPERT, key="x")
        rows = self.store.search("任意条目")
        self.assertTrue(rows)


class TestWriteReadSameKey(unittest.TestCase):
    """写读同键接线（controller 两侧）：expert.memory_scope_key 真正生效。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_skey_c_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_remember_uses_expert_scope_key(self):
        # mobile 专家：expert_id=mobile_operation，但 memory_scope_key=mobile
        task = T.Task(task_id="t_hive", user_goal="打开设置")
        task.task_status = T.TaskStatus.RUNNING
        task.domain = "mobile_operation"
        task.result = "设置已打开"
        self.app.controller._remember(task, verified=True)
        rows = self.app.memory.search(
            "打开设置 设置已打开", scope=Scope.EXPERT, scope_key="mobile")
        self.assertTrue(rows, "写侧应对齐专家键 mobile 而非 domain 名")
        self.assertEqual(rows[0].scope_key, "mobile")

    def test_hive_isolation_between_experts(self):
        task = T.Task(task_id="t_hive2", user_goal="修复崩溃")
        task.task_status = T.TaskStatus.RUNNING
        task.domain = "mobile_operation"
        task.result = "完成"
        self.app.controller._remember(task, verified=True)
        # travel 专家的键检索不到 coding/mobile 蜂巢内容
        rows = self.app.memory.search(
            "修复崩溃 完成", scope=Scope.EXPERT, scope_key="travel")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
