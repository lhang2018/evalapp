"""发布清单/签名/回滚测试（PRD 59-63 / Arch 13.3-13.5）。"""

import unittest

from evoagent.release.manager import ReleaseManager


class TestRelease(unittest.TestCase):
    def test_manifest_signed_and_verified(self):
        rm = ReleaseManager()
        m = rm.build_manifest(version="v1")
        out = rm.verify(m.release_id)
        self.assertTrue(out["ok"])
        # 篡改检测：签名绑定内容摘要
        m2 = rm.build_manifest(version="v2")
        m2.version = "v3-tampered"
        self.assertFalse(rm.verify(m2.release_id)["ok"])
        m3 = rm.build_manifest(version="v3")
        m3.artifact_digest = "deadbeef-tampered"
        self.assertFalse(rm.verify(m3.release_id)["ok"])

    def test_deploy_requires_gate(self):
        rm = ReleaseManager()
        m = rm.build_manifest(version="v1")
        out = rm.deploy(m.release_id, gate_passed=False)
        self.assertFalse(out["ok"])
        self.assertIn("门禁", out["error"])
        out2 = rm.deploy(m.release_id, gate_passed=True)
        self.assertTrue(out2["ok"])
        self.assertEqual(rm.current_release_id, m.release_id)

    def test_critical_scope_rejected(self):
        rm = ReleaseManager()
        m = rm.build_manifest(version="v1")
        out = rm.deploy(m.release_id, gate_passed=True, scope="critical")
        self.assertFalse(out["ok"])

    def test_rollback_recovers_previous(self):
        rm = ReleaseManager()
        m1 = rm.build_manifest(version="v1")
        rm.deploy(m1.release_id, gate_passed=True)
        m2 = rm.build_manifest(version="v2")
        rm.deploy(m2.release_id, gate_passed=True)
        self.assertEqual(rm.current_release_id, m2.release_id)
        out = rm.rollback("指标异常")
        self.assertTrue(out["ok"])
        self.assertEqual(rm.current_release_id, m1.release_id)
        self.assertEqual(len(rm.rollback_records), 1)

    def test_rollback_warns_irreversible(self):
        rm = ReleaseManager()
        rm.irreversible_actions.append({"action": "db_migration"})
        v0 = rm.build_manifest(version="v0")
        rm.deploy(v0.release_id, gate_passed=True)
        v1 = rm.build_manifest(version="v1")
        rm.deploy(v1.release_id, gate_passed=True)
        out = rm.rollback("回滚")
        self.assertTrue(out["ok"], out)
        self.assertIn("irreversible_warning", out)


if __name__ == "__main__":
    unittest.main()
