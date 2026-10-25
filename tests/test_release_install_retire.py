"""ALN-11-P 生产签名与端侧安装退役回执测试。

验收口径（卡片：回执绑定真实设备/制品；坏包拒绝与回滚旧能力实测；
断联不冒充已安装）：
- 退役是发布后生命周期状态：退役版本不得通过验证、部署、回滚或对账下发；
- 安装/激活回执按服务端清单解析签名身份并核对 IdentityRegistry，
  未登记/已撤销/过期一律拒绝；回执记录解析后的身份状态与制品摘要；
- 灰度回滚链：灰度部署 → 对账 → 校验 → 激活 → 健康失败 → 回滚旧版本；
- 跨实现联验：服务端用生产身份签发的 envelope，必须能被真实端侧
  trust.ts 验过（篡改一字节即拒）——不是 mock。
"""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from evoagent.release.delivery import ReleaseDelivery
from evoagent.release.identity import (
    IdentityRegistry,
    PrivateIdentityKey,
)
from evoagent.release.manager import ReleaseManager

ROOT = Path(__file__).resolve().parents[1]
TRUST_TS = ROOT / "android-app" / "src" / "update" / "trust.ts"


class _MemoryStore:
    """最小安装/部署存储（与 test_release_assets 同型）。"""

    def __init__(self):
        self.installations = {}
        self.deployments = {}

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

    def get_device_deployment(self, deployment_id):
        return self.deployments.get(deployment_id)

    def save_device_deployment(self, deployment_id, installation_id,
                               release_id, action, outcome, data):
        self.deployments[deployment_id] = {
            "deployment_id": deployment_id,
            "installation_id": installation_id,
            "release_id": release_id,
            "action": action,
            "outcome": outcome,
            "data": data,
        }


def _envelope_fields(*, release_id, version, body, key, **overrides):
    digest = hashlib.sha256(body).hexdigest()
    fields = dict(
        release_id=release_id,
        version=version,
        channel="stable",
        target_platforms=["android"],
        min_app_version="0.1.0",
        bundle_kind="workflow",
        artifact_url=f"https://example.invalid/{release_id}",
        artifact_sha256=digest,
        artifact_size=len(body),
        artifact_signature=key.sign_artifact_envelope(
            release_id=release_id, version=version,
            artifact_sha256=digest, artifact_size=len(body)),
        artifact_signing_key_id=key.key_id,
        signature_schema="evoagent.release.signing/1",
    )
    fields.update(overrides)
    return fields, digest


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.rm = ReleaseManager()
        self.store = _MemoryStore()
        self.body = b'{"workflow": "demo/v1"}'
        self.key = PrivateIdentityKey.generate(key_id="prod-key-1")
        self.fields, self.digest = _envelope_fields(
            release_id="rel_1", version="v1.0.0", body=self.body,
            key=self.key)
        self.manifest = self.rm.build_manifest(**self.fields)
        self.rm.current_release_id = self.manifest.release_id

    def delivery(self, registry=None):
        return ReleaseDelivery(self.rm, self.store,
                               identity_registry=registry)

    def reconcile(self, delivery, installation_id="inst_1"):
        return delivery.reconcile(
            installation_id=installation_id, device_id="dev_1",
            platform="android", app_version="0.2.0")


class TestRetireLifecycle(_Fixture):
    def test_retire_records_and_blocks_verify_deploy_rollback(self):
        result = self.rm.retire("rel_1", reason="能力下线", actor="ops")
        self.assertTrue(result["ok"])
        self.assertTrue(self.rm.is_retired("rel_1"))
        self.assertEqual(self.rm.retired_release_ids(), ["rel_1"])
        # 退役版本不得通过验证
        verdict = self.rm.verify("rel_1")
        self.assertFalse(verdict["ok"])
        self.assertTrue(verdict.get("retired"))
        # 不得再部署
        deployed = self.rm.deploy("rel_1", gate_passed=True)
        self.assertFalse(deployed["ok"])
        self.assertIn("退役", deployed["error"])
        # 从未部署过：无可回滚记录
        rollback = self.rm.rollback("演练")
        self.assertFalse(rollback["ok"])
        self.assertEqual(rollback["error"], "无可回滚记录")

    def test_rollback_to_retired_target_refused(self):
        second_key = PrivateIdentityKey.generate(key_id="prod-key-2")
        fields2, _ = _envelope_fields(
            release_id="rel_2", version="v2.0.0",
            body=b'{"workflow": "demo/v2"}', key=second_key)
        manifest2 = self.rm.build_manifest(**fields2)
        self.assertTrue(self.rm.deploy("rel_1", gate_passed=True)["ok"])
        self.assertTrue(self.rm.deploy(manifest2.release_id,
                                       gate_passed=True)["ok"])
        self.assertTrue(self.rm.retire("rel_1", reason="能力下线",
                                       actor="ops")["ok"])
        rollback = self.rm.rollback("回到旧版")
        self.assertFalse(rollback["ok"])
        self.assertIn("退役", rollback["error"])

    def test_retire_requires_reason_and_actor(self):
        self.assertFalse(self.rm.retire("rel_1", reason=" ", actor="ops")["ok"])
        self.assertFalse(self.rm.retire("rel_1", reason="下线", actor=" ")["ok"])

    def test_retire_unknown_release_rejected(self):
        result = self.rm.retire("nope", reason="x", actor="ops")
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])

    def test_double_retire_rejected(self):
        self.assertTrue(self.rm.retire("rel_1", reason="下线", actor="ops")["ok"])
        self.assertFalse(self.rm.retire("rel_1", reason="再下线", actor="ops")["ok"])

    def test_reconcile_blocked_when_active_release_retired(self):
        self.rm.retire("rel_1", reason="能力下线", actor="ops")
        out = self.reconcile(self.delivery())
        self.assertEqual(out["decision"], "blocked")
        names = {c["name"] for c in out["checks"] if not c["ok"]}
        self.assertIn("release_not_retired", names)
        self.assertIn("artifact_integrity", names)

    def test_retired_release_not_offered_as_target(self):
        delivery = self.delivery()
        out = self.reconcile(delivery)
        self.assertEqual(out["decision"], "update_available")
        self.assertEqual(out["target"]["release_id"], "rel_1")
        self.assertEqual(out["target"]["signing_identity_status"], "no_registry")
        # 退役后即使重新对账也不得给出目标
        self.rm.retire("rel_1", reason="能力下线", actor="ops")
        after = self.reconcile(delivery)
        self.assertEqual(after["decision"], "blocked")
        self.assertIsNone(after["target"])


class TestRetireReceipt(_Fixture):
    def _publish_second(self, key_id="prod-key-2", release_id="rel_2",
                        version="v2.0.0", body=b'{"workflow": "demo/v2"}'):
        second_key = PrivateIdentityKey.generate(key_id=key_id)
        fields, digest = _envelope_fields(
            release_id=release_id, version=version, body=body, key=second_key)
        manifest = self.rm.build_manifest(**fields)
        return manifest, digest

    def test_retired_receipt_clears_current_and_keeps_known_good(self):
        delivery = self.delivery()
        manifest2, _ = self._publish_second()
        self.assertTrue(self.rm.deploy("rel_1", gate_passed=True)["ok"])
        self.assertTrue(self.rm.deploy(manifest2.release_id, gate_passed=True,
                                       gray_percent=100)["ok"])
        out = delivery.reconcile(installation_id="inst_1", device_id="dev_1",
                                 platform="android", app_version="0.2.0",
                                 current_release_id="rel_1")
        self.assertEqual(out["decision"], "update_available")
        delivery.record_apply(installation_id="inst_1", action="verified",
                              release_id="rel_2")
        activated = delivery.record_apply(installation_id="inst_1",
                                          action="activated",
                                          release_id="rel_2")
        self.assertEqual(
            activated["installation"]["data"]
            .get("last_known_good_release_id"), "rel_1")
        # 退役 rel_2：当前清空、已知良好保留（回滚旧能力仍可走）
        result = delivery.record_apply(installation_id="inst_1",
                                       action="retired", release_id="rel_2",
                                       reason="用户停用该能力")
        self.assertTrue(result["ok"])
        installation = result["installation"]
        self.assertEqual(installation["status"], "retired")
        self.assertIsNone(installation["current_release_id"])
        self.assertIsNone(installation["target_release_id"])
        data = installation["data"]
        self.assertEqual(data["retired_release_id"], "rel_2")
        self.assertEqual(data["last_retire_reason"], "用户停用该能力")
        self.assertEqual(data.get("last_known_good_release_id"), "rel_1")
        record = result["deployment"]
        self.assertEqual(record["action"], "retired")

    def test_retired_receipt_of_pending_target_only_clears_target(self):
        delivery = self.delivery()
        self.reconcile(delivery)
        result = delivery.record_apply(installation_id="inst_1",
                                       action="retired", release_id="rel_1",
                                       reason="放弃更新")
        self.assertTrue(result["ok"])
        installation = result["installation"]
        self.assertIsNone(installation["target_release_id"])
        self.assertIsNone(installation["current_release_id"])
        self.assertEqual(installation["data"]["retired_release_id"], "rel_1")

    def test_retired_without_resolvable_target_rejected(self):
        delivery = self.delivery()
        self.reconcile(delivery)
        first = delivery.record_apply(installation_id="inst_1",
                                      action="retired", release_id="rel_1",
                                      reason="放弃更新")
        self.assertTrue(first["ok"])
        # 当前与目标都已清空：无目标退役被拒绝
        second = delivery.record_apply(installation_id="inst_1",
                                       action="retired", reason="再次退役")
        self.assertFalse(second["ok"])
        self.assertIn("缺少目标版本", second["error"])

    def test_unknown_retire_action_rejected(self):
        delivery = self.delivery()
        self.reconcile(delivery)
        with self.assertRaises(ValueError):
            delivery.record_apply(installation_id="inst_1",
                                  action="decommissioned")


class TestTrustBinding(_Fixture):
    def _registry(self, tmp, keys, active):
        registry = IdentityRegistry(Path(tmp) / "identity.json")
        for key_id, key, activate in keys:
            registry.register_from_private(key, actor="ops",
                                           reason="测试登记",
                                           activate=activate)
        if active:
            registry.rotate(active, actor="ops", reason="轮换")
        return registry

    def test_activation_binds_usable_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._registry(tmp, [("prod-key-1", self.key, True)],
                                      None)
            delivery = self.delivery(registry)
            out = self.reconcile(delivery)
            self.assertEqual(out["target"]["signing_identity_status"],
                             "usable")
            delivery.record_apply(installation_id="inst_1", action="verified",
                                  release_id="rel_1")
            result = delivery.record_apply(installation_id="inst_1",
                                           action="activated",
                                           release_id="rel_1")
            self.assertTrue(result["ok"])
            record = result["deployment"]
            self.assertEqual(record["signing_key_id"], "prod-key-1")
            self.assertEqual(record["signing_identity_status"], "usable")
            self.assertEqual(record["artifact_digest"], self.digest)

    def test_revoked_identity_blocks_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = PrivateIdentityKey.generate(key_id="prod-key-2")
            registry = self._registry(
                tmp, [("prod-key-1", self.key, False),
                      ("prod-key-2", other, True)], None)
            registry.revoke("prod-key-1", reason="私钥泄露", actor="ops")
            delivery = self.delivery(registry)
            out = self.reconcile(delivery)
            self.assertEqual(out["decision"], "blocked")
            failed = {c["name"] for c in out["checks"] if not c["ok"]}
            self.assertIn("signing_identity_trusted", failed)
            delivery.record_apply(installation_id="inst_1", action="verified",
                                  release_id="rel_1")
            blocked = delivery.record_apply(installation_id="inst_1",
                                            action="activated",
                                            release_id="rel_1")
            self.assertFalse(blocked["ok"])
            self.assertIn("revoked", blocked["error"])

    def test_unregistered_identity_blocks_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = PrivateIdentityKey.generate(key_id="prod-key-2")
            registry = self._registry(tmp, [("prod-key-2", other, True)],
                                      None)
            delivery = self.delivery(registry)
            out = self.reconcile(delivery)
            self.assertEqual(out["decision"], "blocked")
            delivery.record_apply(installation_id="inst_1", action="verified",
                                  release_id="rel_1")
            blocked = delivery.record_apply(installation_id="inst_1",
                                            action="activated",
                                            release_id="rel_1")
            self.assertFalse(blocked["ok"])
            self.assertIn("unregistered", blocked["error"])

    def test_expired_identity_blocks_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = IdentityRegistry(Path(tmp) / "identity.json")
            n, e = self.key.public_numbers()
            from evoagent.release.identity import TrustedIdentity
            registry.register(TrustedIdentity(
                key_id="prod-key-1", modulus=format(n, "x"),
                exponent=format(e, "x"), not_before=0.0,
                not_after=1.0), actor="ops", reason="已过期身份")
            delivery = self.delivery(registry)
            self.reconcile(delivery)
            blocked = delivery.record_apply(installation_id="inst_1",
                                            action="verified",
                                            release_id="rel_1")
            self.assertFalse(blocked["ok"])
            self.assertIn("expired", blocked["error"])

    def test_no_registry_keeps_legacy_behavior_with_recorded_status(self):
        delivery = self.delivery()
        out = self.reconcile(delivery)
        self.assertEqual(out["decision"], "update_available")
        delivery.record_apply(installation_id="inst_1", action="verified",
                              release_id="rel_1")
        result = delivery.record_apply(installation_id="inst_1",
                                       action="activated", release_id="rel_1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["deployment"]["signing_identity_status"],
                         "no_registry")

    def test_client_claimed_key_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._registry(tmp, [("prod-key-1", self.key, True)],
                                      None)
            delivery = self.delivery(registry)
            self.reconcile(delivery)
            blocked = delivery.record_apply(installation_id="inst_1",
                                            action="verified",
                                            release_id="rel_1",
                                            signing_key_id="attacker-key")
            self.assertFalse(blocked["ok"])
            self.assertIn("不一致", blocked["error"])


class TestGrayRollbackChain(_Fixture):
    def test_gray_deploy_rollback_chain(self):
        delivery = self.delivery()
        second_key = PrivateIdentityKey.generate(key_id="prod-key-2")
        fields2, _ = _envelope_fields(
            release_id="rel_2", version="v2.0.0",
            body=b'{"workflow": "demo/v2"}', key=second_key)
        manifest2 = self.rm.build_manifest(**fields2)
        # 先部署 rel_1，再灰度 50% 部署 rel_2；rel_1 成为 previous
        self.assertTrue(self.rm.deploy("rel_1", gate_passed=True)["ok"])
        deployed = self.rm.deploy(manifest2.release_id, gate_passed=True,
                                  gray_percent=50)
        self.assertTrue(deployed["ok"])
        self.assertEqual(deployed["gray_percent"], 50)
        self.assertEqual(deployed["previous"], "rel_1")

        # 灰度名单内实例看到更新与回滚目标
        bucket = __import__(
            "evoagent.release.delivery", fromlist=["gray_bucket"]
        ).gray_bucket
        eligible_id = next(
            f"inst_gray_{i}" for i in range(200)
            if bucket(f"inst_gray_{i}") < 50)
        out = delivery.reconcile(installation_id=eligible_id,
                                 device_id="dev_gray", platform="android",
                                 app_version="0.2.0")
        self.assertEqual(out["decision"], "update_available")
        self.assertEqual(out["target"]["release_id"], "rel_2")
        self.assertEqual(out["target"]["rollback_target"], "rel_1")

        # 校验 → 激活 → 健康失败 → 回滚到 rel_1
        delivery.record_apply(installation_id=eligible_id, action="verified",
                              release_id="rel_2")
        delivery.record_apply(installation_id=eligible_id,
                              action="activated", release_id="rel_2")
        unhealthy = delivery.record_apply(
            installation_id=eligible_id, action="health_check",
            release_id="rel_2", outcome="unhealthy", reason="健康检查失败")
        self.assertTrue(unhealthy["ok"])
        rolled = delivery.record_apply(
            installation_id=eligible_id, action="rolled_back",
            release_id="rel_2", to_release_id="rel_1", reason="健康检查失败")
        self.assertTrue(rolled["ok"])
        installation = rolled["installation"]
        self.assertEqual(installation["current_release_id"], "rel_1")
        self.assertEqual(installation["status"], "rolled_back")
        # 回滚后重新对账：回执已把当前版本改回 rel_1；服务端活动版本
        # 仍是 rel_2（灰度继续），是否再次升级由端侧激活门禁决定
        after = delivery.reconcile(installation_id=eligible_id,
                                   device_id="dev_gray", platform="android",
                                   app_version="0.2.0")
        self.assertEqual(after["current_release_id"], "rel_1")
        self.assertEqual(after["decision"], "update_available")
        # 显式退役是退出灰度的正路
        retired = delivery.record_apply(installation_id=eligible_id,
                                        action="retired",
                                        release_id="rel_2",
                                        reason="健康失败后退出灰度")
        self.assertTrue(retired["ok"])

    def test_retired_release_refused_as_gray_activation_target(self):
        delivery = self.delivery()
        self.assertTrue(self.rm.deploy("rel_1", gate_passed=True)["ok"])
        self.rm.retire("rel_1", reason="能力下线", actor="ops")
        delivery.reconcile(installation_id="inst_gray_blocked",
                           device_id="dev_gray", platform="android",
                           app_version="0.2.0")
        blocked = delivery.record_apply(installation_id="inst_gray_blocked",
                                        action="activated",
                                        release_id="rel_1")
        self.assertFalse(blocked["ok"])
        self.assertIn("退役", blocked["error"])


class TestCrossImplementation(unittest.TestCase):
    """服务端生产身份签发的 envelope 必须通过真实端侧 trust.ts 验签。"""

    @unittest.skipIf(not TRUST_TS.is_file(), "端侧 trust.ts 不存在")
    def test_client_trust_ts_verifies_server_envelope(self):
        key = PrivateIdentityKey.generate(key_id="cross-key")
        body = b'{"workflow": "cross/1"}'
        digest = hashlib.sha256(body).hexdigest()
        rm = ReleaseManager()
        fields, _ = _envelope_fields(
            release_id="rel_cross", version="v3.0.0", body=body, key=key)
        manifest = rm.build_manifest(**fields)
        rm.current_release_id = manifest.release_id
        delivery = ReleaseDelivery(rm, _MemoryStore())
        out = delivery.reconcile(installation_id="inst_cross",
                                 device_id="dev_cross", platform="android",
                                 app_version="0.2.0")
        target = out["target"]
        self.assertEqual(out["decision"], "update_available")
        envelope = {
            "schema": target["signature_schema"],
            "release_id": target["release_id"],
            "version": target["version"],
            "artifact_sha256": target["artifact_sha256"],
            "artifact_size": target["artifact_size"],
            "signing_key_id": target["signing_key_id"],
            "signature": target["artifact_signature"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            pub = Path(tmp) / "pub.json"
            env = Path(tmp) / "envelope.json"
            art = Path(tmp) / "artifact.bin"
            tampered = Path(tmp) / "tampered.bin"
            pub.write_text(json.dumps(key.public_record()), encoding="utf-8")
            env.write_text(json.dumps(envelope), encoding="utf-8")
            art.write_bytes(body)
            tampered.write_bytes(body[:-1] + b"X")
            script = (
                "import { readFileSync } from 'node:fs';\n"
                "import { pathToFileURL } from 'node:url';\n"
                f"const {{ TrustedKeyRing }} =\n"
                f"  await import(pathToFileURL({str(TRUST_TS)!r}));\n"
                "const pub = JSON.parse(readFileSync(process.argv[1], 'utf-8'));\n"
                "const envelope = JSON.parse("
                "readFileSync(process.argv[2], 'utf-8'));\n"
                "const bytes = new Uint8Array("
                "readFileSync(process.argv[3]));\n"
                "const ring = new TrustedKeyRing([{ ...pub, revoked: false }]);\n"
                "const result = ring.verifyArtifact(envelope, bytes);\n"
                "process.stdout.write(result.ok ? 'PASS' :"
                " 'FAIL:' + result.reasons.join(';'));\n"
            )
            good = subprocess.run(
                ["node", "--experimental-strip-types",
                 "--input-type=module", "-e", script,
                 str(pub), str(env), str(art)],
                capture_output=True, text=True, timeout=120)
            assert good.stdout.strip() == "PASS", (
                f"端侧验签失败: {good.stdout!r} {good.stderr[-400:]!r}")
            bad = subprocess.run(
                ["node", "--experimental-strip-types",
                 "--input-type=module", "-e", script,
                 str(pub), str(env), str(tampered)],
                capture_output=True, text=True, timeout=120)
            assert bad.stdout.startswith("FAIL"), (
                f"篡改制品必须被拒: {bad.stdout!r} {bad.stderr[-400:]!r}")


if __name__ == "__main__":
    unittest.main()
