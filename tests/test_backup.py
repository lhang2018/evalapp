"""备份与恢复（Arch 20.4）。

验证三库打包、校验和/完整性检查、恢复往返、安全备份与路径校验。
"""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.task import Task


class TestBackupService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_backup_")
        self.app = App(data_dir=self.tmp)
        self.task = self.app.controller.submit_task(
            "计算 3+4", intent="computation", wait=True)
        from evoagent.memory.store import (Credibility, MemoryItem, Scope)
        self.item = MemoryItem(content="备份测试记忆",
                               scope=Scope.PROJECT,
                               status=Credibility.VERIFIED,
                               evidence={"source": "test"})
        self.app.memory.write(self.item)

    def tearDown(self):
        self.app.db.close()
        self.app.memory.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_verify_and_list(self):
        out = self.app.backups.create()
        self.assertTrue(Path(out["path"]).exists())
        self.assertEqual(set(out["files"]), {"evoagent.db", "events.db",
                                             "memory.db"})
        self.assertEqual(out["counts"]["tasks"], 1)
        with zipfile.ZipFile(out["path"]) as zf:
            self.assertIn("manifest.json", zf.namelist())
        check = self.app.backups.verify(out["name"])
        self.assertTrue(check["ok"], check["errors"])
        self.assertEqual(check["manifest"]["format"], 1)
        listed = self.app.backups.list()
        self.assertIn(out["name"], [b["name"] for b in listed])

    def test_tampered_backup_fails_verification(self):
        out = self.app.backups.create()
        path = Path(out["path"])
        tampered = path.with_name("tampered.zip")
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(
                tampered, "w", zipfile.ZIP_DEFLATED) as dst:
            for name in src.namelist():
                data = src.read(name)
                if name == "events.db":
                    data = data + b"corrupted"
                dst.writestr(name, data)
        check = self.app.backups.verify("tampered.zip")
        self.assertFalse(check["ok"])
        self.assertTrue(any("校验和" in e for e in check["errors"]))

    def test_restore_roundtrip(self):
        out = self.app.backups.create()
        target = tempfile.mkdtemp(prefix="evoagent_restore_")
        try:
            app2 = App(data_dir=target)
            self.assertIsNone(app2.db.get_task(self.task.task_id))
            result = app2.backups.restore(out["path"])
            self.assertTrue(result["ok"])
            self.assertIsNotNone(result["safety_backup"])
            restored = app2.db.get_task(self.task.task_id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.task_status.value, "SUCCEEDED")
            self.assertIsNotNone(app2.memory.get(self.item.memory_id))
            events = app2.bus.query(task_id=self.task.task_id, limit=100)
            self.assertTrue(events)
            app2.db.close()
            app2.memory.close()
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_rejects_bad_names_and_corrupt_archives(self):
        with self.assertRaises(ValueError):
            self.app.backups.verify("../events.db")
        with self.assertRaises(FileNotFoundError):
            self.app.backups.verify("missing.zip")
        empty = Path(self.tmp) / "backups" / "empty.zip"
        with zipfile.ZipFile(empty, "w") as zf:
            zf.writestr("other.txt", "x")
        check = self.app.backups.verify("empty.zip")
        self.assertFalse(check["ok"])
        self.assertTrue(check["errors"])


class TestBackupApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_backup_api_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.app.db.close()
        cls.app.memory.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_backup_create_and_list(self):
        st, out = self._post("/admin/backup", {})
        self.assertEqual(st, 201)
        self.assertIn("name", out)
        st, listed = self._get("/admin/backups")
        self.assertEqual(st, 200)
        self.assertIn(out["name"], [b["name"] for b in listed["backups"]])

    def test_restore_rejects_active_tasks(self):
        pending = Task(user_goal="未完成任务")
        self.app.db.save_task(pending)
        try:
            st, out = self._post("/admin/restore",
                                 {"name": "whatever.zip"})
            self.assertEqual(st, 409)
            self.assertIn(pending.task_id, out["active_tasks"])
        finally:
            self.app.db._conn.execute(
                "DELETE FROM tasks WHERE task_id=?", (pending.task_id,))
            self.app.db._conn.commit()

    def test_restore_missing_backup_404(self):
        st, out = self._post("/admin/restore", {"name": "missing.zip"})
        self.assertEqual(st, 404)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
