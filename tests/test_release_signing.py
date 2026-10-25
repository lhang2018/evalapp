"""ALN-03: 发布正文签名、制品字节校验与兼容声明。"""
import copy
import json

import pytest

from evoagent.release import KeyRing, ReleaseManager, ReleaseManifest
from evoagent.release import signing
from evoagent.release.manager import _legacy_digest, _sign, _signed_metadata


class _Store:
    def __init__(self):
        self.saved = {}
        self.events = []

    def list_release_manifests(self, limit=100):
        return []

    def list_release_events(self, limit=100):
        return []

    def save_release_manifest(self, manifest):
        self.saved[manifest.release_id] = manifest

    def append_release_event(self, kind, release_id, data):
        self.events.append({"kind": kind, "release_id": release_id,
                            "data": data})


def _stage(manager, release_id, files):
    root = manager.artifact_dir / release_id
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return list(files)


def _legacy_manifest(secret, **overrides):
    fields = dict(release_id="legacy-r", version="v0",
                  target_platforms=["android"],
                  permission_requirements=[], data_schema="evoagent/1",
                  model_capabilities=[], offline_support=False,
                  rollback_supported=True)
    fields.update(overrides)
    manifest = ReleaseManifest(**fields)
    manifest.signature_version = "hmac-sha256-v1"
    manifest.signing_key_id = None
    manifest.signing_payload_digest = None
    manifest.artifact_digest = _legacy_digest(manifest)
    manifest.signed_by = _sign({"digest": manifest.artifact_digest}, secret)
    return manifest


def test_canonical_encoding_is_unambiguous_and_locks_bytes():
    value = {"b": "é", "a": [1, True, None]}
    assert signing.canonical_bytes(value) == b"m2:s1:al3:i1;tns1:bs2:\xc3\xa9"
    assert signing.canonical_bytes({"x": 1.5}) == b"m1:s1:xd1.5;"
    with pytest.raises(signing.SigningError):
        signing.canonical_bytes({"x": float("nan")})
    with pytest.raises(signing.SigningError):
        signing.canonical_bytes({"x": float("inf")})
    with pytest.raises(signing.SigningError):
        signing.canonical_bytes({1: "not-a-string-key"})
    with pytest.raises(signing.SigningError):
        signing.canonical_bytes({"x": {1, 2}})


def test_payload_binds_identity_channel_key_time_and_files():
    manifest = ReleaseManifest(
        release_id="r1", version="v1", channel="stable", created_at=123.5,
        signing_key_id="k1",
        artifact_files=[{"path": "a.bin", "size": 3, "sha256": "a" * 64}])
    payload = signing.build_payload(_signed_metadata(manifest), key_id="k1")
    assert payload["key_id"] == "k1"
    assert payload["schema"] == signing.SCHEMA
    assert payload["manifest"]["channel"] == "stable"
    assert payload["manifest"]["created_at"] == 123.5
    assert payload["manifest"]["artifact_files"][0]["path"] == "a.bin"
    assert payload["manifest_digest"] == signing.payload_digest(
        dict(payload["manifest"]))
    round_tripped = json.loads(json.dumps(payload))
    assert signing.canonical_bytes(round_tripped) == \
        signing.canonical_bytes(payload)
    assert signing.payload_digest(round_tripped) == \
        signing.payload_digest(payload)

    changed = copy.deepcopy(manifest)
    changed.version = "v2"
    assert signing.canonical_bytes(
        signing.build_payload(_signed_metadata(changed), key_id="k1")) != \
        signing.canonical_bytes(payload)


def test_actual_file_bytes_are_verified_and_tampering_rejected(tmp_path):
    manager = ReleaseManager(artifact_dir=str(tmp_path))
    paths = _stage(manager, "bundle-1", {"a.bin": b"one",
                                         "sub/b.txt": b"two"})
    manifest = manager.build_manifest(release_id="bundle-1",
                                      artifact_files=paths)
    assert [item["path"] for item in manifest.artifact_files] == \
        ["a.bin", "sub/b.txt"]
    assert manager.verify("bundle-1")["ok"]

    (tmp_path / "bundle-1" / "a.bin").write_bytes(b"ONE")
    result = manager.verify("bundle-1")
    assert not result["ok"] and not result["artifact_files_ok"]
    assert any("摘要不符" in item["error"]
               for item in result["artifact_files_errors"])

    (tmp_path / "bundle-1" / "a.bin").write_bytes(b"one")
    assert manager.verify("bundle-1")["ok"]


def test_missing_extra_and_undeclared_files_are_rejected(tmp_path):
    manager = ReleaseManager(artifact_dir=str(tmp_path))
    paths = _stage(manager, "bundle-2", {"a.bin": b"one",
                                         "sub/b.txt": b"two"})
    manager.build_manifest(release_id="bundle-2", artifact_files=paths)

    (tmp_path / "bundle-2" / "sub" / "b.txt").unlink()
    result = manager.verify("bundle-2")
    assert not result["ok"]
    assert any("缺失" in item["error"]
               for item in result["artifact_files_errors"])

    (tmp_path / "bundle-2" / "sub" / "b.txt").write_bytes(b"two")
    (tmp_path / "bundle-2" / "extra.bin").write_bytes(b"unexpected")
    result = manager.verify("bundle-2")
    assert not result["ok"]
    assert any("多余" in item["error"]
               for item in result["artifact_files_errors"])

    (tmp_path / "bundle-2" / "extra.bin").unlink()
    assert manager.verify("bundle-2")["ok"]


def test_manifest_cannot_rewrite_its_own_file_declarations(tmp_path):
    manager = ReleaseManager(artifact_dir=str(tmp_path))
    paths = _stage(manager, "bundle-3", {"a.bin": b"one"})
    manifest = manager.build_manifest(release_id="bundle-3",
                                      artifact_files=paths)
    manifest.artifact_files[0]["sha256"] = "0" * 64
    result = manager.verify("bundle-3")
    assert not result["ok"] and not result["digest_ok"]
    assert not result["signature_ok"]


def test_missing_artifact_at_build_time_is_rejected(tmp_path):
    manager = ReleaseManager(artifact_dir=str(tmp_path))
    (tmp_path / "bundle-4").mkdir()
    with pytest.raises(signing.SigningError):
        manager.build_manifest(release_id="bundle-4",
                               artifact_files=["absent.bin"])
    with pytest.raises(signing.SigningError):
        manager.build_manifest(release_id="bundle-4",
                               artifact_files=["../outside.bin"])


def test_unknown_key_and_wrong_signature_are_rejected(tmp_path):
    manager = ReleaseManager(signing_secret="secret-one")
    manifest = manager.build_manifest(release_id="key-1")

    manifest.signing_key_id = "ghost"
    result = manager.verify("key-1")
    assert not result["ok"]
    assert any("未知签名密钥标识" in error for error in result["errors"])

    manifest.signing_key_id = "server-hmac-1"
    manager.keyring.add("other", "secret-two")
    manifest.signing_key_id = "other"
    assert not manager.verify("key-1")["ok"]


def test_key_rotation_keeps_old_manifest_verifiable(tmp_path):
    keyring = KeyRing()
    keyring.add("k1", "secret-one")
    manager = ReleaseManager(keyring=keyring)
    first = manager.build_manifest(release_id="rot-1", version="v1")
    keyring.add("k2", "secret-two")
    keyring.set_active("k2")
    second = manager.build_manifest(release_id="rot-2", version="v2")
    assert first.signing_key_id == "k1" and second.signing_key_id == "k2"
    assert manager.verify("rot-1")["ok"] and manager.verify("rot-2")["ok"]

    keyring.remove("k1")
    assert not manager.verify("rot-1")["ok"]
    assert manager.verify("rot-2")["ok"]


@pytest.mark.parametrize("field,value,token", [
    ("target_platforms", [], "target_platforms"),
    # ALN-21-A：windows 自该卡起进入受支持平台词汇表（Windows 终端
    # 扩展验证），此处改用仍不受支持的平台保持「不支持的平台被拒」语义
    ("target_platforms", ["linux"], "target_platforms"),
    ("min_app_version", "", "min_app_version"),
    ("permission_requirements", None, "permission_requirements"),
    ("required_tool_contracts", None, "required_tool_contracts"),
    ("model_capabilities", None, "model_capabilities"),
    ("data_schema", None, "data_schema"),
    ("offline_support", None, "offline_support"),
    ("rollback_supported", None, "rollback_supported"),
])
def test_stable_channel_requires_complete_declarations(field, value, token):
    manager = ReleaseManager()
    manifest = manager.build_manifest(**{field: value})
    result = manager.deploy(manifest.release_id, gate_passed=True)
    assert not result["ok"] and manager.current_release_id is None
    assert token in result["error"]
    assert any(token in item for item in result["missing"])


def test_draft_and_internal_channels_relax_explicitly():
    manager = ReleaseManager()
    draft = manager.build_manifest(
        release_id="draft-1", channel="draft",
        permission_requirements=None, rollback_supported=None)
    result = manager.deploy("draft-1", gate_passed=True)
    assert result["ok"], result
    assert result["verification"]["compatibility_relaxed"] is True
    assert result["verification"]["ok"] is True
    assert manager.verify(draft.release_id)["ok"]


def test_unknown_channel_is_rejected():
    manager = ReleaseManager()
    manager.build_manifest(release_id="beta-1", channel="beta")
    result = manager.deploy("beta-1", gate_passed=True)
    assert not result["ok"] and "发布通道" in result["error"]


def test_verification_evidence_is_persisted_for_success_and_failure():
    store = _Store()
    manager = ReleaseManager(signing_secret="evidence-key", store=store)
    manifest = manager.build_manifest(release_id="evidence-1")
    assert manager.deploy("evidence-1", gate_passed=True)["ok"]

    success = [event for event in store.events
               if event["kind"] == "verification"][-1]["data"]
    assert success["ok"] is True
    assert success["context"] == "deploy"
    assert success["key_id"] == "server-hmac-1"
    assert success["payload_digest"] == manifest.artifact_digest

    manifest.version = "tampered-after-deploy"
    result = manager.deploy("evidence-1", gate_passed=True)
    assert not result["ok"]
    failure = [event for event in store.events
               if event["kind"] == "verification"][-1]["data"]
    assert failure["ok"] is False and failure["errors"]
    assert failure["release_id"] == "evidence-1"


def test_legacy_signature_requires_explicit_migration():
    store = _Store()
    manager = ReleaseManager(signing_secret="legacy-secret", store=store)
    legacy = _legacy_manifest("legacy-secret")
    manager.manifests[legacy.release_id] = legacy

    result = manager.verify(legacy.release_id)
    assert not result["ok"] and result["legacy"] is True
    assert not manager.deploy(legacy.release_id, gate_passed=True)["ok"]

    migrated = manager.migrate_signature(legacy.release_id)
    assert migrated["ok"] and migrated["migrated"] is True
    assert migrated["legacy_verified"] is True
    assert manager.verify(legacy.release_id)["ok"]
    assert manager.deploy(legacy.release_id, gate_passed=True)["ok"]
    events = [event for event in store.events
              if event["kind"] == "verification"
              and event["data"].get("migrated")]
    assert events and events[-1]["data"]["legacy_verified"] is True


def test_unverifiable_legacy_requires_allow_unverified_and_actor():
    manager = ReleaseManager(signing_secret="legacy-secret")
    legacy = ReleaseManifest.from_dict({"release_id": "legacy-old",
                                        "artifact_digest": "old"})
    manager.manifests[legacy.release_id] = legacy

    assert not manager.migrate_signature(legacy.release_id)["ok"]
    assert not manager.migrate_signature(
        legacy.release_id, allow_unverified=True)["ok"]
    migrated = manager.migrate_signature(
        legacy.release_id, allow_unverified=True, actor="release-admin")
    assert migrated["ok"] and migrated["migrated"] is True
    assert migrated["legacy_verified"] is False
    assert manager.verify(legacy.release_id)["ok"]
