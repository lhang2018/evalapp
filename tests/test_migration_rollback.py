"""迁移失败回退与回退演练（ALN-17-A）。

验证：迁移执行失败经 SAVEPOINT 自动回退到该条迁移前状态（更早迁移与
业务数据不变、部分变更不残留、任务/运行历史可读）；已提交后的坏变更
用迁移前备份走 restore 回退的演练闭环。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evoagent.app import App
from evoagent.core.schema.migrations import (
    MigrationError,
    apply_migrations,
    current_version,
)
from evoagent.core.schema import migrations as migrations_module


def _failing_migration(conn):
    """部分执行后失败：验证 SAVEPOINT 能清掉已执行的部分。"""
    conn.execute("CREATE TABLE rollback_probe (id INTEGER)")
    conn.execute("INSERT INTO rollback_probe VALUES (1)")
    raise RuntimeError("注入的迁移失败")


class TestMigrationRollback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_migrb_")
        self._appended = False

    def tearDown(self):
        if self._appended:
            migrations_module.MIGRATIONS.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _append_failing(self):
        migrations_module.MIGRATIONS.append(
            (migrations_module.SCHEMA_VERSION + 1, "injected_failure",
             _failing_migration))
        self._appended = True

    def test_failed_migration_rolls_back_keeps_history_readable(self):
        app = App(data_dir=self.tmp)
        try:
            task = app.controller.submit_task(
                "计算 7+8", intent="computation", wait=True)
            self.assertEqual(current_version(app.db._conn),
                             migrations_module.SCHEMA_VERSION)
            self._append_failing()
            with self.assertRaises(MigrationError) as ctx:
                apply_migrations(app.db._conn)
            self.assertEqual(ctx.exception.name, "injected_failure")
            # 回退后：版本不变、探针表不残留、历史可读、连接可用
            self.assertEqual(current_version(app.db._conn),
                             migrations_module.SCHEMA_VERSION)
            tables = {r[0] for r in app.db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("rollback_probe", tables)
            restored = app.db.get_task(task.task_id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.task_status.value, "SUCCEEDED")
            app.db.save_task(restored)
            runs = app.db._conn.execute(
                "SELECT COUNT(*) FROM task_runs").fetchone()[0]
            self.assertGreaterEqual(int(runs), 0, "运行历史表可查询")
        finally:
            app.db.close()
            app.memory.close()

    def test_earlier_migrations_survive_later_failure(self):
        """全新库：前序迁移成功落库，后续迁移失败只回退自身。"""
        import sqlite3
        path = Path(self.tmp) / "fresh.db"
        conn = sqlite3.connect(str(path))
        try:
            self._append_failing()
            with self.assertRaises(MigrationError):
                apply_migrations(conn)
            self.assertEqual(current_version(conn),
                             migrations_module.SCHEMA_VERSION,
                             "失败迁移之前的迁移必须已记录")
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("rollback_probe", tables,
                             "失败迁移的部分变更不得残留")
            names = [r[0] for r in conn.execute(
                "SELECT name FROM schema_migrations ORDER BY version")]
            self.assertNotIn("injected_failure", names)
        finally:
            conn.close()

    def test_backup_restore_rollback_drill(self):
        """已提交坏变更后的回退演练：迁移前备份 → 恢复 → 历史可读。"""
        app = App(data_dir=self.tmp)
        try:
            task = app.controller.submit_task(
                "计算 9+10", intent="computation", wait=True)
            pre = app.backups.create("pre_change.zip")
            # 模拟已提交的坏变更：任务历史被删除（真实场景等价于坏迁移落库）
            app.db._conn.execute(
                "DELETE FROM tasks WHERE task_id=?", (task.task_id,))
            app.db._conn.commit()
            self.assertIsNone(app.db.get_task(task.task_id))
            result = app.backups.restore(pre["name"])
            self.assertTrue(result["ok"])
            self.assertTrue(result["counts_verified"])
            self.assertEqual(result["schema_version"],
                             migrations_module.SCHEMA_VERSION)
            recovered = app.db.get_task(task.task_id)
            self.assertIsNotNone(recovered, "回退后任务历史必须可读")
            self.assertEqual(recovered.task_status.value, "SUCCEEDED")
        finally:
            app.db.close()
            app.memory.close()

    def test_apply_migrations_idempotent_after_rollback(self):
        app = App(data_dir=self.tmp)
        try:
            self._append_failing()
            with self.assertRaises(MigrationError):
                apply_migrations(app.db._conn)
            # 修复问题（移除坏迁移）后再次应用：幂等，无重复执行
            migrations_module.MIGRATIONS.pop()
            self._appended = False
            applied = apply_migrations(app.db._conn)
            self.assertEqual(applied, [])
            self.assertEqual(current_version(app.db._conn),
                             migrations_module.SCHEMA_VERSION)
        finally:
            app.db.close()
            app.memory.close()


if __name__ == "__main__":
    unittest.main()
