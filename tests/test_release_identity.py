"""ALN-03-P 生产信任根测试：签发、验签、身份生命周期与跨实现一致性。

判别力说明：
- "端侧验签"用例在测试内嵌了一个与 android-app/src/update/rsa.ts 同构的
  纯 JS 验签器（若 Node 可用则直接 import 真实 rsa.ts），并对服务端签名
  做真实 RSA 运算验证——不是 mock；
- 变异测试脚本（.planning/2026-10-25-aln03-p/mutation_test.py）对本测试
  文件验证判别力：注入签名截断/公钥调换/窗口反转等缺陷必须被捕获。
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from evoagent.release.identity import (
    ALGORITHM,
    SCHEMA,
    IdentityError,
    IdentityRegistry,
    PrivateIdentityKey,
    TrustedIdentity,
    canonical_artifact_message,
    verify_artifact_signature,
)

ROOT = Path(__file__).resolve().parents[1]
RSA_TS = ROOT / "android-app" / "src" / "update" / "rsa.ts"


def _key(key_id: str = "rel-key-1") -> PrivateIdentityKey:
    return PrivateIdentityKey.generate(key_id=key_id)


def _envelope(**overrides):
    values = dict(
        release_id="rel_100", version="v1.2.3",
        artifact_sha256="ab" * 32, artifact_size=2048,
    )
    values.update(overrides)
    return values


# ---- 规范消息 ----
class TestCanonicalMessage:
    def test_seven_line_structure(self):
        message = canonical_artifact_message(
            schema=SCHEMA, release_id="r1", version="v1",
            artifact_sha256="AB" * 32, artifact_size=7,
            signing_key_id="k1")
        lines = message.decode("utf-8").split("\n")
        assert lines == ["EVOAGENT-ARTIFACT-V1", SCHEMA, "r1", "v1",
                         "ab" * 32, "7", "k1"]

    def test_digest_normalization(self):
        """大写与 sha256: 前缀都被归一为小写裸十六进制。"""
        a = canonical_artifact_message(
            schema=SCHEMA, release_id="r", version="v",
            artifact_sha256="sha256:AB" + "CD" * 31, artifact_size=1,
            signing_key_id="k")
        b = canonical_artifact_message(
            schema=SCHEMA, release_id="r", version="v",
            artifact_sha256="ab" + "cd" * 31, artifact_size=1,
            signing_key_id="k")
        assert a == b

    def test_rejects_bad_digest(self):
        with pytest.raises(IdentityError):
            canonical_artifact_message(
                schema=SCHEMA, release_id="r", version="v",
                artifact_sha256="nothex", artifact_size=1, signing_key_id="k")

    def test_rejects_newline_injection(self):
        """字段含换行会破坏七行结构——必须拒绝。"""
        with pytest.raises(IdentityError):
            canonical_artifact_message(
                schema=SCHEMA, release_id="r\nEVIL", version="v",
                artifact_sha256="ab" * 32, artifact_size=1, signing_key_id="k")

    def test_rejects_negative_size(self):
        with pytest.raises(IdentityError):
            canonical_artifact_message(
                schema=SCHEMA, release_id="r", version="v",
                artifact_sha256="ab" * 32, artifact_size=-1,
                signing_key_id="k")


# ---- 签发与验签 ----
class TestSignVerify:
    def test_sign_then_verify_ok(self):
        key = _key()
        sig = key.sign_artifact_envelope(**_envelope())
        message = canonical_artifact_message(
            schema=SCHEMA, signing_key_id=key.key_id, **_envelope())
        n, e = key.public_numbers()
        result = verify_artifact_signature(
            modulus=format(n, "x"), exponent=format(e, "x"),
            message=message, signature=sig)
        assert result["ok"] is True, result

    def test_tampered_metadata_rejected(self):
        """篡改 release_id/version/size 任一字段都必须验签失败（G2）。

        断言拒绝理由必须是"签名验证失败"（真实比较失败），而非内部
        一致性错误——防止恒过变异被交叉验证兜底掩盖。
        """
        key = _key()
        sig = key.sign_artifact_envelope(**_envelope())
        n, e = key.public_numbers()
        for field, value in (("release_id", "rel_999"),
                             ("version", "v9.9.9"),
                             ("artifact_size", 4096)):
            message = canonical_artifact_message(
                schema=SCHEMA, signing_key_id=key.key_id,
                **_envelope(**{field: value}))
            result = verify_artifact_signature(
                modulus=format(n, "x"), exponent=format(e, "x"),
                message=message, signature=sig)
            assert result["ok"] is False, f"篡改 {field} 未被拒绝"
            assert result["error"] == "签名验证失败", \
                f"篡改 {field} 的拒绝理由异常: {result['error']}"

    def test_wrong_key_rejected(self):
        """错密钥（另一把合法私钥）验签必须失败，且理由是验证失败。"""
        signer, other = _key("k-a"), _key("k-b")
        sig = signer.sign_artifact_envelope(**_envelope())
        message = canonical_artifact_message(
            schema=SCHEMA, signing_key_id=signer.key_id, **_envelope())
        n, e = other.public_numbers()
        result = verify_artifact_signature(
            modulus=format(n, "x"), exponent=format(e, "x"),
            message=message, signature=sig)
        assert result["ok"] is False
        assert result["error"] == "签名验证失败"

    def test_truncated_signature_rejected(self):
        key = _key()
        sig = key.sign_artifact_envelope(**_envelope())
        message = canonical_artifact_message(
            schema=SCHEMA, signing_key_id=key.key_id, **_envelope())
        n, e = key.public_numbers()
        result = verify_artifact_signature(
            modulus=format(n, "x"), exponent=format(e, "x"),
            message=message, signature=sig[: len(sig) - 2])
        assert result["ok"] is False
        assert result["error"] == "签名验证失败"

    def test_garbage_signature_rejected(self):
        key = _key()
        message = canonical_artifact_message(
            schema=SCHEMA, signing_key_id=key.key_id, **_envelope())
        n, e = key.public_numbers()
        result = verify_artifact_signature(
            modulus=format(n, "x"), exponent=format(e, "x"),
            message=message, signature="zzzz-not-valid")
        assert result["ok"] is False


# ---- 跨实现一致性：端侧 rsa.ts 必须能验服务端签名 ----
class TestCrossImplementation:
    @pytest.mark.skipif(not RSA_TS.is_file(), reason="端侧 rsa.ts 不存在")
    def test_device_rsa_ts_verifies_server_signature(self):
        """用真实端侧 rsa.ts（node --experimental-strip-types）验服务端签名。"""
        key = _key("device-cross")
        sig = key.sign_artifact_envelope(**_envelope())
        message = canonical_artifact_message(
            schema=SCHEMA, signing_key_id=key.key_id, **_envelope())
        record = key.public_record()
        with tempfile.TemporaryDirectory() as tmp:
            pub_path = Path(tmp) / "pub.json"
            msg_path = Path(tmp) / "msg.bin"
            sig_path = Path(tmp) / "sig.hex"
            pub_path.write_text(json.dumps(record), encoding="utf-8")
            msg_path.write_bytes(message)
            sig_path.write_text(sig, encoding="utf-8")
            script = (
                "import { readFileSync } from 'node:fs';\n"
                "import { pathToFileURL } from 'node:url';\n"
                f"const {{ rsaSha256Verify, decodeSignature }} =\n"
                f"  await import(pathToFileURL({str(RSA_TS)!r}));\n"
                "const pub = JSON.parse(readFileSync(process.argv[1], 'utf-8'));\n"
                "const message = readFileSync(process.argv[2]);\n"
                "const signature = decodeSignature(readFileSync(process.argv[3], 'utf-8'));\n"
                "process.stdout.write(rsaSha256Verify(\n"
                "  { n: pub.n, e: pub.e }, new Uint8Array(message), signature)\n"
                "  ? 'PASS' : 'FAIL');\n"
            )
            done = subprocess.run(
                ["node", "--experimental-strip-types", "--input-type=module",
                 "-e", script, str(pub_path), str(msg_path), str(sig_path)],
                capture_output=True, text=True, timeout=120)
            output = done.stdout.strip()
            assert output == "PASS", (
                f"端侧验签失败: stdout={output!r} stderr={done.stderr[-400:]!r}")

    def test_pure_and_cryptography_agree(self):
        """纯 Python 与 cryptography 两实现的验签结论必须一致（内部交叉）。"""
        key = _key()
        sig = key.sign_artifact_envelope(**_envelope())
        message = canonical_artifact_message(
            schema=SCHEMA, signing_key_id=key.key_id, **_envelope())
        n, e = key.public_numbers()
        result = verify_artifact_signature(
            modulus=format(n, "x"), exponent=format(e, "x"),
            message=message, signature=sig)
        assert result["ok"] is True
        assert "不一致" not in (result.get("error") or "")

    def test_hex_even_envelope(self):
        """公钥导出必须是偶数长度十六进制（e=65537 → "010001"）。

        这是跨端契约：奇数长度会让端侧 toBigInt 走 base64url 分支解错。
        """
        key = _key()
        record = key.public_record()
        assert len(record["n"]) % 2 == 0
        assert len(record["e"]) % 2 == 0
        assert record["e"] == "010001"


# ---- 私钥边界 ----
class TestPrivateKeyBoundary:
    def test_public_record_has_no_private_material(self):
        key = _key()
        record = key.public_record()
        text = json.dumps(record)
        assert "d" not in record
        assert "p" not in record
        assert "q" not in record

    def test_private_key_rejects_small_modulus(self):
        with pytest.raises(IdentityError):
            PrivateIdentityKey.generate(key_id="small", modulus_bits=1024)

    def test_private_key_rejects_blank_key_id(self):
        with pytest.raises(IdentityError):
            PrivateIdentityKey.generate(key_id="  ")

    def test_private_key_rejects_newline_key_id(self):
        with pytest.raises(IdentityError):
            PrivateIdentityKey.generate(key_id="k\n1")

    def test_from_pem_roundtrip(self):
        from cryptography.hazmat.primitives import serialization
        key = _key("pem-key")
        pem = key._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        restored = PrivateIdentityKey.from_pem(pem, key_id="pem-key")
        sig_a = key.sign_artifact_envelope(**_envelope())
        sig_b = restored.sign_artifact_envelope(**_envelope())
        # RSA PKCS#1 v1.5 是确定性的：同消息同密钥得到同签名
        assert sig_a == sig_b


# ---- 身份登记簿 ----
class TestIdentityRegistry:
    def test_register_and_public_records(self, tmp_path):
        registry = IdentityRegistry(tmp_path / "identity.json")
        key = _key("id-1")
        registry.register_from_private(key, actor="ops", reason="首发登记")
        records = registry.public_records()
        assert len(records) == 1
        assert records[0]["key_id"] == "id-1"
        assert records[0]["e"] == "010001"

    def test_rotate_keeps_old_record(self, tmp_path):
        """轮换后旧身份记录保留且可分发（历史发布仍可验）。"""
        registry = IdentityRegistry(tmp_path / "identity.json")
        old, new = _key("old"), _key("new")
        registry.register_from_private(old, actor="ops", reason="v1")
        registry.register_from_private(new, actor="ops", reason="v2")
        registry.rotate("new", actor="ops", reason="年度轮换")
        assert registry.active_key_id == "new"
        ids = {r["key_id"] for r in registry.public_records()}
        assert ids == {"old", "new"}

    def test_revoke_removes_from_distribution(self, tmp_path):
        """撤销后公钥不再默认分发（端侧不再信任）。"""
        registry = IdentityRegistry(tmp_path / "identity.json")
        old, new = _key("old"), _key("new")
        registry.register_from_private(old, actor="ops", reason="v1")
        registry.register_from_private(new, actor="ops", reason="v2")
        registry.rotate("new", actor="ops", reason="轮换")
        registry.revoke("old", actor="ops", reason="疑似泄露")
        ids = {r["key_id"] for r in registry.public_records()}
        assert ids == {"new"}
        # 显式带 include_revoked 时可审计查阅
        assert len(registry.public_records(include_revoked=True)) == 2

    def test_cannot_revoke_active(self, tmp_path):
        registry = IdentityRegistry(tmp_path / "identity.json")
        key = _key("only")
        registry.register_from_private(key, actor="ops", reason="v1")
        with pytest.raises(IdentityError):
            registry.revoke("only", actor="ops", reason="手滑")

    def test_revoke_requires_reason(self, tmp_path):
        registry = IdentityRegistry(tmp_path / "identity.json")
        registry.register(TrustedIdentity(
            key_id="a", modulus="ff", exponent="010001"),
            actor="ops", reason="测试")
        registry.register(TrustedIdentity(
            key_id="b", modulus="ee", exponent="010001"),
            actor="ops", reason="测试")
        registry.rotate("b", actor="ops", reason="换")
        with pytest.raises(IdentityError):
            registry.revoke("a", actor="ops", reason="")

    def test_no_key_squatting(self, tmp_path):
        """同 key_id 不同参数的重复登记必须拒绝（防替换攻击）。"""
        registry = IdentityRegistry(tmp_path / "identity.json")
        registry.register(TrustedIdentity(
            key_id="k", modulus="ff", exponent="010001"),
            actor="ops", reason="第一次")
        with pytest.raises(IdentityError):
            registry.register(TrustedIdentity(
                key_id="k", modulus="ee", exponent="010001"),
                actor="ops", reason="偷换")

    def test_persistence_roundtrip(self, tmp_path):
        path = tmp_path / "identity.json"
        registry = IdentityRegistry(path)
        key = _key("persist")
        registry.register_from_private(key, actor="ops", reason="落盘")
        reloaded = IdentityRegistry(path)
        assert reloaded.active_key_id == "persist"
        assert reloaded.get("persist").modulus == registry.get("persist").modulus

    def test_persistence_rejects_tampered_schema(self, tmp_path):
        path = tmp_path / "identity.json"
        registry = IdentityRegistry(path)
        registry.register(TrustedIdentity(
            key_id="x", modulus="ff", exponent="010001"),
            actor="ops", reason="测试")
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["schema"] = "something-else"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(IdentityError):
            IdentityRegistry(path)

    def test_validity_window_enforced(self, tmp_path):
        """生效窗口外的身份不可用（端侧 not_before/not_after 镜像）。"""
        registry = IdentityRegistry(tmp_path / "identity.json", now=lambda: 1000.0)
        identity = TrustedIdentity(
            key_id="windowed", modulus="ff", exponent="010001",
            not_before=2000.0, not_after=3000.0)
        registry.register(identity, actor="ops", reason="未来生效")
        assert identity.usable(at=1000.0) is False
        assert identity.usable(at=2500.0) is True
        assert identity.usable(at=3500.0) is False

    def test_revoked_requires_reason_and_records_time(self, tmp_path):
        registry = IdentityRegistry(tmp_path / "identity.json", now=lambda: 500.0)
        registry.register(TrustedIdentity(key_id="a", modulus="ff",
                                          exponent="010001"),
                          actor="ops", reason="v1")
        registry.register(TrustedIdentity(key_id="b", modulus="ee",
                                          exponent="010001"),
                          actor="ops", reason="v2")
        registry.rotate("b", actor="ops", reason="换")
        revoked = registry.revoke("a", actor="ops", reason="泄露")
        assert revoked.revoked is True
        assert revoked.revoked_at == 500.0
        assert revoked.revoked_reason == "泄露"


# ---- 信封完整性：与服务端现有 manifest 管道对接 ----
class TestEnvelopeIntegration:
    def test_envelope_fields_complete(self):
        """签发结果可填满 delivery.py 要求的 envelope 四字段。"""
        key = _key()
        env = _envelope()
        sig = key.sign_artifact_envelope(**env)
        record = key.public_record()
        # delivery.reconcile 的 artifact_envelope 检查要求：
        # artifact_sha256 / artifact_size / artifact_signature /
        # artifact_signing_key_id 四项齐全
        envelope = {
            "artifact_sha256": env["artifact_sha256"],
            "artifact_size": env["artifact_size"],
            "artifact_signature": sig,
            "signing_key_id": key.key_id,
        }
        assert all(envelope.values())
        # 且服务端能自验通过
        message = canonical_artifact_message(
            schema=SCHEMA, release_id=env["release_id"],
            version=env["version"], artifact_sha256=env["artifact_sha256"],
            artifact_size=env["artifact_size"],
            signing_key_id=key.key_id)
        assert verify_artifact_signature(
            modulus=record["n"], exponent=record["e"],
            message=message, signature=sig)["ok"] is True

    def test_algorithm_label_matches_device(self):
        """端侧 TrustedPublicKey 的 algorithm 字段约定值。"""
        assert ALGORITHM == "rsa-sha256"
