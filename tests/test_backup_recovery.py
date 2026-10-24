"""备份/恢复闭环加固（ALN-17-A）。

验证 manifest schema_version 兼容判定（拒绝高于当前/拒绝旧格式/按迁移
路径升级）、行数快照核对、损坏与篡改拒绝且当前数据不变、恢复不执行
历史副作用、恢复耗时字段。旧 tests/test_backup.py 保留基础往返覆盖。
"""

import json
import sqlite3
import tempfile
import shutil
import time
import unittest
import zipfile
from pathlib import Path

from evoagent.app import App
from evoagent.core.schema.migrations import MIGRATIONS, SCHEMA_VERSION


def rewrite_manifest(src: Path, name: str, mutate) -> str:
    """生成修改 manifest 后的备份（文件字节不变，sha256 依然匹配）。"""
    out = src.parent / name
    with zipfile.ZipFile(src) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(items["manifest.json"].decode("utf-8"))
    mutate(manifest)
    items["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for n, data in items.items():
            dst.writestr(n, data)
    return out.name


class TestBackupRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_bkrec_")
        self.app = App(data_dir=self.tmp)
        self.task = self.app.controller.submit_task(
            "计算 5+6", intent="computation", wait=True)
        self.backup = self.app.backups.create("base.zip")

    def tearDown(self):
        self.app.db.close()
        self.app.memory.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 验收1：manifest 记录 schema_version，兼容判定 ----

    def test_manifest_records_schema_version(self):
        with zipfile.ZipFile(self.backup["path"]) as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
        check = self.app.backups.verify("base.zip")
        self.assertTrue(check["ok"], check["errors"])
        self.assertEqual(check["manifest"]["schema_version"], SCHEMA_VERSION)

    def test_restore_rejects_newer_schema_with_actionable_reason(self):
        name = rewrite_manifest(
            Path(self.backup["path"]), "newer.zip",
            lambda m: m.__setitem__("schema_version", SCHEMA_VERSION + 9))
        before = self.app.backups._counts()
        with self.assertRaises(ValueError) as ctx:
            self.app.backups.restore(name)
        self.assertIn("高于当前程序支持", str(ctx.exception))
        self.assertIn("升级程序", str(ctx.exception))
        self.assertEqual(self.app.backups._counts(), before,
                         "拒绝恢复后当前数据必须不变")

    def test_restore_rejects_legacy_manifest_without_schema_version(self):
        def drop_version(m):
            m.pop("schema_version")
        name = rewrite_manifest(Path(self.backup["path"]), "legacy.zip",
                                drop_version)
        check = self.app.backups.verify(name)
        self.assertFalse(check["ok"])
        self.assertTrue(any("schema_version" in e for e in check["errors"]))
        before = self.app.backups._counts()
        with self.assertRaises(ValueError):
            self.app.backups.restore(name)
        self.assertEqual(self.app.backups._counts(), before)

    def test_restore_upgrades_older_schema_via_migrations(self):
        """v2 备份恢复进 v3 程序：暂存区走迁移路径升级，任务历史可读。"""
        staged = Path(self.tmp) / "craft"
        staged.mkdir()
        db_path = staged / "evoagent.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE schema_migrations("
            "version INTEGER PRIMARY KEY, name TEXT, applied_at REAL)")
        for version, name, fn in sorted(MIGRATIONS, key=lambda m: m[0])[:2]:
            fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?,?,?)",
                (version, name, time.time()))
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT,"
            " intent TEXT, domain TEXT, risk_level TEXT, user_goal TEXT,"
            " created_at REAL, data TEXT, goal_id TEXT)")
        from evoagent.core.task import Task
        legacy = Task(user_goal="v2 时期任务")
        conn.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
            (legacy.task_id, "PENDING", legacy.intent, "generic", "LOW",
             legacy.user_goal, time.time(),
             json.dumps(legacy.to_dict(), ensure_ascii=False), None))
        conn.commit()
        conn.close()

        base = Path(self.backup["path"])
        with zipfile.ZipFile(base) as zf:
            items = {n: zf.read(n) for n in zf.namelist()}
        manifest = json.loads(items["manifest.json"].decode("utf-8"))
        import hashlib
        digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
        manifest["schema_version"] = 2
        manifest["files"]["evoagent.db"] = {
            "sha256": digest, "bytes": db_path.stat().st_size}
        manifest["counts"]["tasks"] = 1
        # v2 演练库只重建了 tasks，没有 release_manifests 表——如实写 0
        manifest["counts"]["release_manifests"] = 0
        items["evoagent.db"] = db_path.read_bytes()
        items["manifest.json"] = json.dumps(
            manifest, ensure_ascii=False).encode("utf-8")
        crafted = base.parent / "v2.zip"
        with zipfile.ZipFile(crafted, "w", zipfile.ZIP_DEFLATED) as dst:
            for n, data in items.items():
                dst.writestr(n, data)

        result = self.app.backups.restore("v2.zip")
        self.assertTrue(result["ok"])
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["migrations_applied"],
                         ["async_approval_bindings"])
        self.assertEqual(self.app.backups._schema_version(), SCHEMA_VERSION)
        restored = self.app.db.get_task(legacy.task_id)
        self.assertIsNotNone(restored, "升级恢复后任务历史必须可读")
        self.assertEqual(restored.user_goal, "v2 时期任务")
        self.assertEqual(self.app.backups._counts()["tasks"], 1)

    # ---- 验收2：损坏/篡改/缺表拒绝且当前数据不变 ----

    def test_tampered_and_missing_table_rejected_data_intact(self):
        before = self.app.backups._counts()
        src = Path(self.backup["path"])
        tampered = src.parent / "tampered.zip"
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
                tampered, "w", zipfile.ZIP_DEFLATED) as zout:
            for n in zin.namelist():
                zout.writestr(n, zin.read(n) + b"x" if n == "memory.db"
                              else zin.read(n))
        with self.assertRaises(ValueError):
            self.app.backups.restore("tampered.zip")

        notable = src.parent / "notable.zip"
        with zipfile.ZipFile(src) as zin:
            items = {n: zin.read(n) for n in zin.namelist()}
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.commit()
        buf = Path(self.tmp) / "no_tasks.db"
        real = sqlite3.connect(str(buf))
        conn.backup(real)
        real.close()
        conn.close()
        items["evoagent.db"] = buf.read_bytes()
        manifest = json.loads(items["manifest.json"].decode("utf-8"))
        import hashlib
        manifest["files"]["evoagent.db"] = {
            "sha256": hashlib.sha256(buf.read_bytes()).hexdigest(),
            "bytes": buf.stat().st_size}
        items["manifest.json"] = json.dumps(
            manifest, ensure_ascii=False).encode("utf-8")
        with zipfile.ZipFile(notable, "w", zipfile.ZIP_DEFLATED) as zout:
            for n, data in items.items():
                zout.writestr(n, data)
        with self.assertRaises(ValueError):
            self.app.backups.restore("notable.zip")
        self.assertEqual(self.app.backups._counts(), before,
                         "两类拒绝路径下当前数据都必须原样保留")
        self.assertIsNotNone(self.app.db.get_task(self.task.task_id))

    def test_row_count_snapshot_mismatch_cancels_restore(self):
        before = self.app.backups._counts()
        name = rewrite_manifest(
            Path(self.backup["path"]), "badcounts.zip",
            lambda m: m["counts"].__setitem__("tasks", 999))
        with self.assertRaises(ValueError) as ctx:
            self.app.backups.restore(name)
        self.assertIn("行数快照不符", str(ctx.exception))
        self.assertEqual(self.app.backups._counts(), before)

    # ---- 验收4：恢复不执行历史副作用；验收5：计数对账；RTO 字段 ----

    def test_restore_replays_state_not_side_effects(self):
        target = tempfile.mkdtemp(prefix="evoagent_bkrec_tgt_")
        app2 = None
        try:
            app2 = App(data_dir=target)
            result = app2.backups.restore(
                str(Path(self.backup["path"])), safety_backup=False)
            self.assertTrue(result["ok"])
            self.assertTrue(result["counts_verified"])
            self.assertGreaterEqual(result["duration_ms"], 0)
            snapshot = self.backup["counts"]
            self.assertEqual(app2.backups._counts(), snapshot,
                             "恢复后计数必须与快照一致")
            live_events = app2.bus.query(limit=1000)
            self.assertEqual(len(live_events), snapshot["events"],
                             "事件按快照原样还原，不得额外执行产生新事件")
            restored = app2.db.get_task(self.task.task_id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.task_status.value,
                             self.task.task_status.value)
            self.assertEqual(
                app2.db._conn.execute(
                    "SELECT COUNT(*) FROM tasks").fetchone()[0],
                snapshot["tasks"])
        finally:
            if app2 is not None:
                app2.db.close()
                app2.memory.close()
            shutil.rmtree(target, ignore_errors=True)

    def test_restore_creates_safety_backup_and_reports_it(self):
        result = self.app.backups.restore("base.zip")
        self.assertTrue(result["safety_backup"])
        self.assertTrue((Path(self.tmp) / "backups"
                         / result["safety_backup"]).exists())


if __name__ == "__main__":
    unittest.main()
