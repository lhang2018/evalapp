"""模型迁移执行链（PRD 20 / 104 / 111 / Arch 7.4）。

验证九步迁移：档案与资产继承 → 兼容性 → 同数据集回归对比 →
发布组合 → 受控启用 → 回滚，以及固定模型覆盖路由。
"""

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.models.base import ModelProfile
from evoagent.models.metering import MeteredModel
from evoagent.models.migration import MIGRATION_STEPS
from evoagent.models.mock import MockModel


class TestModelMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_migration_")
        self.app = App(data_dir=self.tmp)
        mock2 = MockModel()
        mock2.model_id = "mock-2"
        self.app.model_registry.register(mock2.profile())
        metered2 = MeteredModel(mock2, self.app.bus)
        original = self.app._adapter_for_model_id
        self.app.router.setup(
            lambda mid: metered2 if mid == "mock-2" else original(mid))

    def tearDown(self):
        self.app.db.close()
        self.app.memory.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _advance_to_ready(self):
        mig = self.app.migrations.create("mock-1", "mock-2")
        self.assertEqual(mig.status, "compatible")
        mig = self.app.migrations.run_regression(mig.migration_id)
        self.assertEqual(mig.status, "evaluated")
        migration = self.app.migrations.prepare_release(mig.migration_id)
        self.assertEqual(migration.status, "ready")
        return migration

    def test_full_chain_apply_and_rollback(self):
        mig = self.app.migrations.create("mock-1", "mock-2")
        self.assertEqual([s["step"] for s in mig.steps], MIGRATION_STEPS)
        self.assertTrue(mig.compatibility["passed"])
        self.assertIn("old_model_profile", mig.inherited_assets)

        mig = self.app.migrations.run_regression(mig.migration_id)
        comparison = mig.regression["comparison"]
        self.assertTrue(comparison["passed"], comparison)
        self.assertEqual(comparison["quality_delta"], 0.0)
        self.assertEqual(mig.status, "evaluated")

        mig = self.app.migrations.prepare_release(mig.migration_id)
        self.assertEqual(mig.status, "ready")
        self.assertIsNotNone(mig.release_id)
        self.assertIsNotNone(
            self.app.releases.manifests.get(mig.release_id))

        out = self.app.migrations.apply(mig.migration_id, actor="test")
        self.assertTrue(out["ok"])
        self.assertEqual(self.app.router.default_model_id, "mock-2")
        self.assertEqual(
            self.app.migrations.get(mig.migration_id).status, "applied")
        self.assertIn("model_migration_applied", [
            e.event_type for e in self.app.bus.query(limit=200)])

        out = self.app.migrations.rollback(mig.migration_id,
                                           reason="test rollback")
        self.assertTrue(out["ok"])
        self.assertIsNone(self.app.router.default_model_id)
        self.assertEqual(
            self.app.migrations.get(mig.migration_id).status, "rolled_back")

    def test_unknown_and_same_model_rejected(self):
        with self.assertRaises(KeyError):
            self.app.migrations.create("mock-1", "missing-model")
        with self.assertRaises(ValueError):
            self.app.migrations.create("mock-1", "mock-1")

    def test_blocked_when_adapter_cannot_resolve(self):
        self.app.model_registry.register(ModelProfile(model_id="ghost-1"))
        mig = self.app.migrations.create("mock-1", "ghost-1")
        self.assertEqual(mig.status, "blocked")
        self.assertTrue(mig.blocked_reasons)
        self.assertFalse(mig.compatibility["passed"])
        with self.assertRaises(RuntimeError):
            self.app.migrations.run_regression(mig.migration_id)
        with self.assertRaises(RuntimeError):
            self.app.migrations.apply(mig.migration_id)

    def test_apply_requires_evaluated(self):
        mig = self.app.migrations.create("mock-1", "mock-2")
        with self.assertRaises(RuntimeError):
            self.app.migrations.apply(mig.migration_id)
        self.assertIsNone(self.app.router.default_model_id)

    def test_migration_persisted(self):
        mig = self.app.migrations.create("mock-1", "mock-2")
        stored = self.app.db.get_model_migration(mig.migration_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["new_model_id"], "mock-2")
        listed = self.app.migrations.list()
        self.assertTrue(any(m.migration_id == mig.migration_id
                            for m in listed))

    def test_model_override_records_route(self):
        task = self.app.controller.submit_task(
            "现在几点了", intent="time_query",
            model_override="mock-2", wait=True)
        self.assertEqual(task.runtime_manifest["model_route"]["model_id"],
                         "mock-2")
        self.assertEqual(task.runtime_manifest["model_route"]["source"],
                         "override")
        self.assertEqual(task.task_status.value, "SUCCEEDED")

    def test_unknown_override_falls_back_with_warning(self):
        before = time.time()
        task = self.app.controller.submit_task(
            "现在几点了", intent="time_query",
            model_override="missing-model", wait=True)
        route = task.runtime_manifest["model_route"]
        self.assertNotEqual(route["model_id"], "missing-model")
        self.assertEqual(route["source"], "router")
        warnings = [e for e in self.app.bus.query(task_id=task.task_id,
                                                  limit=100)
                    if e.event_type == "model_override_unavailable"
                    and e.timestamp >= before - 1]
        self.assertTrue(warnings)


class TestMigrationApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_migration_api_")
        cls.app = App(data_dir=cls.tmp)
        mock2 = MockModel()
        mock2.model_id = "mock-2"
        cls.app.model_registry.register(mock2.profile())
        metered2 = MeteredModel(mock2, cls.app.bus)
        original = cls.app._adapter_for_model_id
        cls.app.router.setup(
            lambda mid: metered2 if mid == "mock-2" else original(mid))
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
            return json.loads(r.read().decode())

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_models_endpoint(self):
        out = self._get("/models")
        ids = {m["model_id"] for m in out["models"]}
        self.assertEqual(ids, {"mock-1", "mock-2"})
        self.assertIn("routes", out)

    def test_create_unknown_model_404(self):
        st, out = self._post("/models/migrations",
                             {"old_model_id": "mock-1",
                              "new_model_id": "nope"})
        self.assertEqual(st, 404)
        self.assertIn("error", out)

    def test_migration_chain_via_api(self):
        st, mig = self._post("/models/migrations", {
            "old_model_id": "mock-1", "new_model_id": "mock-2"})
        self.assertEqual(st, 201)
        mid = mig["migration_id"]
        self.assertEqual(mig["status"], "compatible")

        st, mig = self._post(f"/models/migrations/{mid}/regression", {})
        self.assertEqual(st, 200)
        self.assertEqual(mig["status"], "evaluated")

        st, mig = self._post(f"/models/migrations/{mid}/release", {})
        self.assertEqual(st, 200)
        self.assertEqual(mig["status"], "ready")

        st, out = self._post(f"/models/migrations/{mid}/apply",
                             {"actor": "api-test"})
        self.assertEqual(st, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(out["default_model_id"], "mock-2")

        detail = self._get(f"/models/migrations/{mid}")
        self.assertEqual(detail["status"], "applied")
        listed = self._get("/models/migrations")["migrations"]
        self.assertTrue(any(m["migration_id"] == mid for m in listed))

        st, out = self._post(f"/models/migrations/{mid}/rollback",
                             {"reason": "api rollback"})
        self.assertEqual(st, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(self._get(
            f"/models/migrations/{mid}")["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
