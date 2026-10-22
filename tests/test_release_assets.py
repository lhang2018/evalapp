"""发布清单的冻结资产引用边界（ALN-06-A 步骤 5 生产端）。

覆盖：构建期形状校验、部署前精确版本/摘要/生命周期门禁、清单篡改、
无资产包回归、下发载荷含引用但不宣称端侧已安装。
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from evoagent.assets.repository import AssetRepository
from evoagent.assets.refs import manifest_asset_ref
from evoagent.release.delivery import ReleaseDelivery
from evoagent.release.manager import ReleaseManager


def _approved_asset(repo, asset_id="skill:demo", version="1.0.0"):
    asset = repo.register(
        asset_id=asset_id, asset_type="skill", namespace="domain_private",
        version=version, content={"name": "demo", "entry": "main.py"},
        source_refs=["repo:tests/fixtures/skill-demo"])
    for state in ("candidate", "validating"):
        asset = repo.transition(asset_id, version, state)
    return repo.transition(asset_id, version, "approved",
                           evidence_refs=["eval:run-1"])


class _MemoryStore:
    def __init__(self):
        self.installations = {}

    def get_installation(self, installation_id):
        return self.installations.get(installation_id)

    def save_installation(self, installation_id, device_id, platform,
                          app_version, current_release_id, target_release_id,
                          status, data):
        self.installations[installation_id] = {
            "installation_id": installation_id,
            "device_id": device_id,
            "platform": platform,
            "app_version": app_version,
            "current_release_id": current_release_id,
            "target_release_id": target_release_id,
            "status": status,
            "data": data,
        }


class TestReleaseAssetRefs(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.repo = AssetRepository(self.conn)
        self.asset = _approved_asset(self.repo)
        self.ref = manifest_asset_ref(self.asset)
        self.rm = ReleaseManager()

    def tearDown(self):
        self.conn.close()

    def test_build_normalizes_and_signs_exact_ref(self):
        m = self.rm.build_manifest(version="v1", asset_refs=[self.ref])
        self.assertEqual(m.asset_refs, [self.ref])
        self.assertTrue(self.rm.verify(m.release_id)["ok"])

        # 清单被篡改摘要 → 签名/摘要校验失败
        m.asset_refs[0]["content_digest"] = "0" * 64
        self.assertFalse(self.rm.verify(m.release_id)["ok"])

    def test_build_rejects_malformed_refs(self):
        with self.assertRaises(ValueError):
            self.rm.build_manifest(version="v1", asset_refs=self.ref)
        with self.assertRaises(ValueError):
            self.rm.build_manifest(version="v1", asset_refs=[
                {"asset_id": "skill:demo", "version": "1.0.0"}])
        with self.assertRaises(ValueError):
            self.rm.build_manifest(version="v1", asset_refs=[
                {"asset_id": "skill:demo", "version": "1.0.0",
                 "content_digest": "not-a-digest"}])

    def test_build_rejects_conflicting_duplicate_versions(self):
        bad = dict(self.ref, content_digest="f" * 64)
        with self.assertRaises(ValueError):
            self.rm.build_manifest(version="v1",
                                   asset_refs=[self.ref, bad])
        # 完全相同的重复引用是幂等输入
        m = self.rm.build_manifest(version="v2",
                                   asset_refs=[self.ref, dict(self.ref)])
        self.assertEqual(m.asset_refs, [self.ref])

    def test_deploy_requires_repo_for_asset_manifest(self):
        m = self.rm.build_manifest(version="v1", asset_refs=[self.ref])
        out = self.rm.deploy(m.release_id, gate_passed=True)
        self.assertFalse(out["ok"])
        self.assertIn("资产仓储", out["error"])

    def test_deploy_verifies_exact_version_and_digest(self):
        m = self.rm.build_manifest(version="v1", asset_refs=[self.ref])
        out = self.rm.deploy(m.release_id, gate_passed=True,
                             asset_repo=self.repo)
        self.assertTrue(out["ok"], out)

        missing = dict(self.ref, version="9.9.9")
        m2 = self.rm.build_manifest(version="v2", asset_refs=[missing])
        out2 = self.rm.deploy(m2.release_id, gate_passed=True,
                              asset_repo=self.repo)
        self.assertFalse(out2["ok"])
        self.assertIn("资产引用校验失败", out2["error"])

        mismatch = dict(self.ref, content_digest="a" * 64)
        m3 = self.rm.build_manifest(version="v3", asset_refs=[mismatch])
        out3 = self.rm.deploy(m3.release_id, gate_passed=True,
                              asset_repo=self.repo)
        self.assertFalse(out3["ok"])
        self.assertIn("摘要不符", out3["errors"][0]["error"])

    def test_deploy_rejects_unapproved_asset(self):
        draft = self.repo.register(
            asset_id="skill:draft", asset_type="skill",
            namespace="domain_private", version="0.1.0",
            content={"name": "draft"}, source_refs=["repo:draft"])
        m = self.rm.build_manifest(version="v1",
                                   asset_refs=[manifest_asset_ref(draft)])
        out = self.rm.deploy(m.release_id, gate_passed=True,
                             asset_repo=self.repo)
        self.assertFalse(out["ok"])
        self.assertIn("生命周期", out["errors"][0]["error"])

    def test_metadata_only_manifest_still_deploys(self):
        m = self.rm.build_manifest(version="v1")
        out = self.rm.deploy(m.release_id, gate_passed=True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(self.rm.verify_assets(m.release_id,
                                              self.repo)["ok"])

    def test_legacy_artifact_without_asset_refs_still_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            rm = ReleaseManager(artifact_dir=tmp)
            m = rm.build_manifest(version="v1")
            artifact = Path(tmp) / f"{m.release_id}.json"
            legacy = json.loads(artifact.read_text(encoding="utf-8"))
            legacy.pop("asset_refs", None)
            # 模拟旧版本写出的制品：没有 asset_refs 字段
            artifact.write_text(
                json.dumps(legacy, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")), encoding="utf-8")
            self.assertTrue(rm.verify(m.release_id)["ok"])

    def test_delivery_sends_refs_without_claiming_install(self):
        m = self.rm.build_manifest(version="v1", asset_refs=[self.ref])
        self.assertTrue(self.rm.deploy(m.release_id, gate_passed=True,
                                       asset_repo=self.repo)["ok"])
        delivery = ReleaseDelivery(self.rm, _MemoryStore())
        out = delivery.reconcile(
            installation_id="inst-assets-0001", platform="android",
            app_version="0.1.0")
        self.assertEqual(out["decision"], "update_available")
        self.assertEqual(out["target"]["asset_refs"], [self.ref])
        self.assertTrue(out["target"]["asset_refs_require_client_verification"])
        self.assertNotIn("signed_by_secret", out["target"])


if __name__ == "__main__":
    unittest.main()
