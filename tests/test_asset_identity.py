"""ALN-06-A 不可变资产身份、依赖门禁与 Pack 字节夹具。

锁定任务卡验收：
  1. 同 ID/version 内容变化被拒绝；别名不会改变已冻结读取；
  2. 依赖缺失/环/不兼容、来源或证据不足不能激活或晋升；
  3. 旧专家/工作流仍有精确版本映射；回滚能取到原内容；
  4. 依赖读取、晋升、退役可追溯；GC 不删在用版本；
  5. Pack 用真实字节验证，且不伪称端侧已安装。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 注册表路由需要先导入模块才能注册（server.py 接线是共享补丁）。
from evoagent.api import assets_api  # noqa: E402,F401
from evoagent.api.server import ApiServer  # noqa: E402
from evoagent.app import App  # noqa: E402
from evoagent.assets.content import (  # noqa: E402
    PackFile,
    PackManifest,
    build_manifest,
    canonical_bytes,
    content_digest,
    load_pack,
    verify_pack,
)
from evoagent.assets.entity import (  # noqa: E402
    ASSET_LIFECYCLE,
    ASSET_NAMESPACE,
    ASSET_TYPE,
    Asset,
    AssetConflict,
    AssetNotFound,
    AssetRef,
    AssetValidationError,
    DependencyError,
    LifecycleError,
    PackVerificationError,
)
from evoagent.assets.mapping import (  # noqa: E402
    LEGACY_STATUS_MAP,
    asset_id_for,
    capability_to_asset,
    expert_to_asset,
    map_inventory,
    model_profile_to_asset,
    workflow_to_asset,
)
from evoagent.assets.refs import (  # noqa: E402
    manifest_asset_ref,
    verify_manifest_ref,
)
from evoagent.assets.repository import AssetRepository  # noqa: E402
from evoagent.experts.registry import CapabilityEntry, Expert  # noqa: E402
from evoagent.models.base import ModelProfile  # noqa: E402
from evoagent.workflows.models import WorkflowDef  # noqa: E402


def _repo() -> AssetRepository:
    return AssetRepository(sqlite3.connect(":memory:"))


def _register(repo, asset_id="tool:x", version="1.0.0", content=None,
              **kwargs):
    params = dict(
        asset_id=asset_id, asset_type="tool", namespace="shared_validated",
        version=version, content=content if content is not None else {"a": 1},
        source_refs=("tests:fixture",))
    params.update(kwargs)
    return repo.register(**params)


def _promote(repo, asset_id, version, *, evidence=("eval:suite",)):
    repo.transition(asset_id, version, "validating")
    return repo.transition(asset_id, version, "approved",
                           evidence_refs=evidence)


class TestAssetContract(unittest.TestCase):
    def test_eight_states_and_enums_come_from_contracts(self):
        self.assertEqual(len(ASSET_LIFECYCLE), 8)
        self.assertEqual(len(ASSET_NAMESPACE), 5)
        self.assertEqual(len(ASSET_TYPE), 8)
        for state in ASSET_LIFECYCLE:
            asset = Asset(asset_id="a", asset_type="tool",
                          namespace="shared_validated", version="1",
                          content_digest="0" * 64, lifecycle_state=state)
            asset.validate()

    def test_unknown_enum_is_rejected(self):
        for field, value in (("asset_type", "plugin"),
                             ("namespace", "global"),
                             ("lifecycle_state", "enabled")):
            kwargs = dict(asset_id="a", asset_type="tool",
                          namespace="shared_validated", version="1",
                          content_digest="0" * 64)
            kwargs[field] = value
            with self.assertRaises(AssetValidationError):
                Asset(**kwargs).validate()

    def test_ref_requires_exact_version(self):
        self.assertEqual(str(AssetRef.parse("expert:coding@1.0.0")),
                         "expert:coding@1.0.0")
        for bad in ("expert:coding", "expert:coding@", "@1.0.0"):
            with self.assertRaises(AssetValidationError):
                AssetRef.parse(bad)

    def test_contract_dict_has_formal_fields_only(self):
        asset = _register(_repo())
        keys = set(asset.to_contract_dict())
        self.assertEqual(keys, {
            "asset_id", "asset_type", "namespace", "version",
            "content_digest", "source_refs", "dependency_refs",
            "compatibility", "lifecycle_state",
            "evaluation_evidence_refs", "rollback_ref"})
        self.assertNotIn("content", keys)

    def test_type_prefix_keeps_ids_namespaced(self):
        self.assertEqual(asset_id_for("expert", "coding"), "expert:coding")
        self.assertNotEqual(asset_id_for("expert", "x"),
                            asset_id_for("workflow", "x"))


class TestImmutability(unittest.TestCase):
    def setUp(self):
        self.repo = _repo()

    def test_same_version_different_content_is_rejected(self):
        _register(self.repo, content={"a": 1})
        with self.assertRaises(AssetConflict):
            _register(self.repo, content={"a": 2})
        stored = self.repo.get("tool:x", "1.0.0")
        self.assertEqual(stored.content, {"a": 1},
                         "冲突登记不得覆盖原内容")

    def test_identical_registration_is_idempotent(self):
        first = _register(self.repo)
        second = _register(self.repo)
        self.assertEqual(first.content_digest, second.content_digest)
        count = self.repo._conn.execute(
            "SELECT COUNT(*) FROM assets").fetchone()[0]
        self.assertEqual(count, 1)
        actions = [e["action"] for e in self.repo.events()]
        self.assertEqual(actions, ["register", "register_idempotent"])

    def test_same_content_different_metadata_is_conflict(self):
        _register(self.repo)
        with self.assertRaises(AssetConflict):
            _register(self.repo, namespace="domain_private")

    def test_digest_ignores_dict_insertion_order(self):
        a = _register(self.repo, version="1", content={"b": 2, "a": 1})
        b = _register(self.repo, version="2", content={"a": 1, "b": 2})
        self.assertEqual(a.content_digest, b.content_digest)
        self.assertEqual(a.content_digest,
                         content_digest({"a": 1, "b": 2}))
        self.assertEqual(canonical_bytes({"b": 2, "a": 1}),
                         canonical_bytes({"a": 1, "b": 2}))

    def test_initial_active_is_rejected(self):
        with self.assertRaises(LifecycleError):
            _register(self.repo, lifecycle_state="active")

    def test_returned_content_mutation_does_not_leak(self):
        asset = _register(self.repo)
        asset.content["tampered"] = True
        again = self.repo.get("tool:x", "1.0.0")
        self.assertNotIn("tampered", again.content)


class TestExactVersionReads(unittest.TestCase):
    def setUp(self):
        self.repo = _repo()

    def test_missing_exact_version_is_404_not_latest(self):
        _register(self.repo, version="1.0.0")
        self.assertIsNone(self.repo.get("tool:x", "9.9.9"))
        with self.assertRaises(AssetNotFound):
            self.repo.get_exact("tool:x@9.9.9")

    def test_no_latest_pointer_api_exists(self):
        self.assertFalse(any("latest" in name.lower()
                             for name in dir(self.repo)))

    def test_resolve_returns_contract_shape(self):
        _register(self.repo)
        out = self.repo.resolve("tool:x@1.0.0")
        self.assertEqual(out["asset_id"], "tool:x")
        self.assertEqual(out["version"], "1.0.0")


class TestDependencyAndLifecycleGates(unittest.TestCase):
    def setUp(self):
        self.repo = _repo()
        self.dep = _register(self.repo, asset_id="tool:dep", version="2.0.0",
                             content={"dep": 1})
        self.asset = _register(
            self.repo, asset_id="tool:x", content={"x": 1},
            dependency_refs=("tool:dep@2.0.0",),
            lifecycle_state="candidate")

    def test_missing_dependency_blocks_promotion(self):
        with self.assertRaises(DependencyError):
            _register(self.repo, asset_id="tool:lonely", content={"y": 1},
                      dependency_refs=("tool:absent@1",),
                      lifecycle_state="candidate")
            _promote(self.repo, "tool:lonely", "1.0.0")

    def test_cycle_blocks_promotion(self):
        _register(self.repo, asset_id="tool:a", version="1",
                  content={"a": 1},
                  dependency_refs=("tool:b@1",), lifecycle_state="candidate")
        _register(self.repo, asset_id="tool:b", version="1",
                  content={"b": 1},
                  dependency_refs=("tool:a@1",), lifecycle_state="candidate")
        with self.assertRaises(DependencyError):
            _promote(self.repo, "tool:a", "1")

    def test_incompatible_pin_blocks_promotion(self):
        _register(self.repo, asset_id="tool:pinned", content={"p": 1},
                  dependency_refs=("tool:dep@2.0.0",),
                  compatibility={"pins": {"tool:dep": "f" * 64}},
                  lifecycle_state="candidate")
        with self.assertRaises(DependencyError):
            _promote(self.repo, "tool:pinned", "1.0.0")

    def test_valid_pin_and_dependency_allow_promotion(self):
        digest = self.dep.content_digest
        _register(self.repo, asset_id="tool:pinned", content={"p": 1},
                  dependency_refs=("tool:dep@2.0.0",),
                  compatibility={"pins": {"tool:dep": digest}},
                  lifecycle_state="candidate")
        approved = _promote(self.repo, "tool:pinned", "1.0.0")
        self.assertEqual(approved.lifecycle_state, "approved")
        active = self.repo.transition("tool:pinned", "1.0.0", "active")
        self.assertEqual(active.lifecycle_state, "active")

    def test_evidence_is_required_to_approve(self):
        with self.assertRaises(LifecycleError):
            self.repo.transition("tool:x", "1.0.0", "validating")
            self.repo.transition("tool:x", "1.0.0", "approved")
        approved = self.repo.transition(
            "tool:x", "1.0.0", "approved", evidence_refs=("eval:r1",))
        self.assertEqual(approved.evaluation_evidence_refs, ("eval:r1",))

    def test_source_is_required_to_approve(self):
        _register(self.repo, asset_id="tool:nosrc", content={"n": 1},
                  source_refs=(), lifecycle_state="candidate")
        with self.assertRaises(LifecycleError):
            _promote(self.repo, "tool:nosrc", "1.0.0")

    def test_illegal_transition_is_rejected(self):
        with self.assertRaises(LifecycleError):
            self.repo.transition("tool:x", "1.0.0", "retired")
            self.repo.transition("tool:x", "1.0.0", "active")

    def test_dependency_reads_are_auditable(self):
        _promote(self.repo, "tool:x", "1.0.0")
        checks = [e for e in self.repo.events("tool:x", "1.0.0")
                  if e["action"] == "dependency_check"]
        self.assertTrue(checks, "依赖读取未留痕")
        self.assertTrue(checks[-1]["detail"]["ok"])
        self.assertEqual(checks[-1]["detail"]["dependencies"],
                         ["tool:dep@2.0.0"])

    def test_failed_dependency_read_leaves_evidence(self):
        _register(self.repo, asset_id="tool:broken", content={"b": 1},
                  dependency_refs=("tool:missing@1",),
                  lifecycle_state="candidate")
        with self.assertRaises(DependencyError):
            _promote(self.repo, "tool:broken", "1.0.0")
        checks = [e for e in self.repo.events("tool:broken", "1.0.0")
                  if e["action"] == "dependency_check"]
        self.assertTrue(checks)
        self.assertFalse(checks[-1]["detail"]["ok"])
        self.assertIn("missing", checks[-1]["detail"]["error"])

    def test_transitions_are_auditable(self):
        _promote(self.repo, "tool:x", "1.0.0")
        actions = [(e["action"], e["from_state"], e["to_state"])
                   for e in self.repo.events("tool:x", "1.0.0")
                   if e["action"] == "transition"]
        self.assertEqual(actions, [
            ("transition", "candidate", "validating"),
            ("transition", "validating", "approved"),
        ])


class TestReferencesRollbackAndGC(unittest.TestCase):
    def setUp(self):
        self.repo = _repo()
        self.base = _register(self.repo, asset_id="tool:base", version="1",
                              content={"base": 1})
        self.user = _register(self.repo, asset_id="tool:user", version="2",
                              content={"user": 1},
                              dependency_refs=("tool:base@1",),
                              lifecycle_state="candidate")

    def test_dependents_are_exact_version_only(self):
        refs = [str(r) for r in self.repo.dependents("tool:base", "1")]
        self.assertEqual(refs, ["tool:user@2"])
        self.assertEqual(self.repo.dependents("tool:base", "3"), [])

    def test_gc_never_collects_in_use_version(self):
        self.repo.transition("tool:base", "1", "retired")
        candidates = [c["asset_id"] for c in self.repo.gc_candidates()]
        self.assertNotIn("tool:base", candidates,
                         "仍被引用的 retired 版本不得进入 GC 候选")
        with self.assertRaises(DependencyError):
            self.repo.collect("tool:base", "1")

    def test_collect_after_dependents_retired(self):
        self.repo.transition("tool:base", "1", "retired")
        self.repo.transition("tool:user", "2", "retired")
        self.assertTrue(self.repo.collect("tool:user", "2"))
        self.assertTrue(self.repo.collect("tool:base", "1"))
        self.assertIsNone(self.repo.get("tool:base", "1"))
        self.assertEqual(self.repo.events("tool:base", "1")[-1]["action"],
                         "collect")

    def test_rollback_reads_original_content(self):
        _register(self.repo, asset_id="tool:base", version="2",
                  content={"base": 2}, rollback_ref="tool:base@1")
        rolled = self.repo.rollback_asset("tool:base", "2")
        self.assertEqual(rolled.version, "1")
        self.assertEqual(rolled.content, {"base": 1})

    def test_rollback_missing_is_explicit(self):
        with self.assertRaises(LifecycleError):
            self.repo.rollback_asset("tool:base", "1")
        _register(self.repo, asset_id="tool:base", version="2",
                  content={"base": 2}, rollback_ref="tool:base@9")
        with self.assertRaises(AssetNotFound):
            self.repo.rollback_asset("tool:base", "2")


class TestPackByteFixture(unittest.TestCase):
    FILES = {
        "SKILL.md": "---\nname: demo\n---\n".encode("utf-8"),
        "scripts/run.py": b"print('hello')\n",
        "checksums.sha256": b"",
    }

    def test_manifest_verifies_real_bytes(self):
        manifest = build_manifest("demo", "1.0.0", self.FILES,
                                  permissions=("file_read",),
                                  dependency_refs=("tool:base@1",),
                                  evaluation_evidence_refs=("eval:r1",))
        result = verify_pack(manifest, self.FILES)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.checked, 3)
        loaded = load_pack(manifest, self.FILES)
        self.assertFalse(loaded.installed,
                         "本地夹具不得伪称端侧已安装")
        self.assertEqual(loaded.to_dict()["file_count"], 3)
        self.assertEqual(loaded.to_dict()["permissions"], ["file_read"])

    def test_tampered_byte_fails(self):
        manifest = build_manifest("demo", "1.0.0", self.FILES)
        files = dict(self.FILES)
        files["scripts/run.py"] = b"print('evil')\n"
        result = verify_pack(manifest, files)
        self.assertFalse(result.ok)
        self.assertTrue(any("摘要不符" in e for e in result.errors))
        with self.assertRaises(PackVerificationError):
            load_pack(manifest, files)

    def test_missing_and_extra_files_fail(self):
        manifest = build_manifest("demo", "1.0.0", self.FILES)
        missing = {k: v for k, v in self.FILES.items() if k != "SKILL.md"}
        self.assertFalse(verify_pack(manifest, missing).ok)
        extra = dict(self.FILES)
        extra["backdoor.sh"] = b"x"
        result = verify_pack(manifest, extra)
        self.assertFalse(result.ok)
        self.assertEqual(result.unused_files, ("backdoor.sh",))

    def test_unsafe_paths_are_rejected(self):
        for path in ("../escape", "/abs", "C:/abs", "a//b", "a\\b"):
            with self.assertRaises(AssetValidationError):
                PackFile(path=path, sha256="0" * 64, size=0)

    def test_manifest_is_order_independent(self):
        reversed_files = {k: self.FILES[k]
                          for k in reversed(list(self.FILES))}
        m1 = build_manifest("demo", "1", self.FILES)
        m2 = build_manifest("demo", "1", reversed_files)
        self.assertEqual([f.path for f in m1.files],
                         [f.path for f in m2.files])

    def test_duplicate_manifest_path_is_rejected(self):
        entry = PackFile("a", "0" * 64, 0)
        with self.assertRaises(AssetValidationError):
            PackManifest(pack_id="p", version="1", files=(entry, entry))

    def test_manifest_dict_round_trip(self):
        manifest = build_manifest("demo", "1.0.0", self.FILES,
                                  permissions=("file_read",),
                                  rollback_ref="pack:demo@0.9.0")
        data = manifest.to_dict()
        restored = PackManifest.from_dict(data)
        self.assertEqual(restored, manifest)
        self.assertEqual(canonical_bytes(data), canonical_bytes(
            restored.to_dict()))

    def test_manifest_dict_rejects_bad_size(self):
        with self.assertRaises(AssetValidationError):
            PackManifest.from_dict({
                "pack_id": "p", "version": "1",
                "files": [{"path": "a", "sha256": "0" * 64,
                           "size": "not-a-number"}]})


class TestManifestAssetRefs(unittest.TestCase):
    def setUp(self):
        self.repo = _repo()
        self.asset = _register(self.repo)

    def test_frozen_ref_verifies_exact_content(self):
        ref = manifest_asset_ref(self.asset)
        self.assertEqual(set(ref), {"asset_id", "version", "content_digest"})
        verified = verify_manifest_ref(ref, self.repo)
        self.assertEqual(verified.content_digest, self.asset.content_digest)
        self.assertEqual(verified.content, {"a": 1})

    def test_tampered_digest_is_rejected(self):
        ref = manifest_asset_ref(self.asset)
        ref["content_digest"] = "f" * 64
        with self.assertRaises(AssetConflict):
            verify_manifest_ref(ref, self.repo)

    def test_incomplete_or_unknown_ref_is_rejected(self):
        with self.assertRaises(AssetValidationError):
            verify_manifest_ref({"asset_id": "tool:x"}, self.repo)
        with self.assertRaises(AssetNotFound):
            verify_manifest_ref({"asset_id": "tool:x", "version": "9",
                                 "content_digest": "0" * 64}, self.repo)

    def test_ref_carries_no_content_or_signing_secret(self):
        ref = manifest_asset_ref(self.asset)
        serialized = json.dumps(ref).lower()
        self.assertNotIn("secret", serialized)
        self.assertNotIn("content", ref)


class TestLegacyMapping(unittest.TestCase):
    def test_expert_mapping_keeps_exact_version_and_lists_gaps(self):
        expert = Expert(expert_id="coding", version="2.1.0",
                        dependencies=["helper"], quality_metrics=[])
        draft = expert_to_asset(expert)
        self.assertEqual(draft.asset_id, "expert:coding")
        self.assertEqual(draft.version, "2.1.0")
        self.assertEqual(draft.lifecycle_state, "candidate")
        self.assertTrue(any("helper" in m for m in draft.missing))
        self.assertTrue(any("评测证据" in m for m in draft.missing))

    def test_workflow_mapping_preserves_definition(self):
        wf = WorkflowDef(workflow_id="wf1", version="1.2.0", name="demo")
        draft = workflow_to_asset(wf)
        self.assertEqual(draft.content["workflow_id"], "wf1")
        self.assertEqual(draft.version, "1.2.0")
        self.assertEqual(draft.namespace, "shared_validated")

    def test_model_mapping_flags_missing_version_and_unknown_price(self):
        profile = ModelProfile(model_id="m1", provider="acme")
        draft = model_profile_to_asset(profile)
        self.assertEqual(draft.version, "unversioned")
        self.assertTrue(any("版本" in m for m in draft.missing))
        self.assertTrue(any("未知价格" in n for n in draft.notes))

    def test_legacy_capability_status_maps_explicitly(self):
        self.assertEqual(LEGACY_STATUS_MAP,
                         {"active": "candidate", "experimental": "draft",
                          "disabled": "retired"})
        cap = CapabilityEntry(capability_id="c1", version="1", domain="d",
                              description="x", status="disabled")
        draft = capability_to_asset(cap)
        self.assertEqual(draft.lifecycle_state, "retired")
        self.assertIn("legacy_status", draft.content)

    def test_inventory_mapping_is_deterministic(self):
        drafts = map_inventory(
            experts=[Expert(expert_id="b"), Expert(expert_id="a")],
            workflows=[WorkflowDef(workflow_id="w")],
            models=[ModelProfile(model_id="m")])
        keys = [(d.asset_type, d.asset_id) for d in drafts]
        self.assertEqual(keys, sorted(keys))
        repo = _repo()
        for draft in drafts:
            repo.register_draft(draft)
        self.assertEqual(len(repo.list()), len(drafts))


class TestAssetsApiEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_assets_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _payload(self, **overrides):
        body = {
            "asset_id": "expert:api", "asset_type": "expert",
            "namespace": "domain_private", "version": "1.0.0",
            "content": {"expert_id": "api", "version": "1.0.0"},
            "source_refs": ["tests:api"],
            "lifecycle_state": "candidate",
        }
        body.update(overrides)
        return body

    def test_register_read_and_conflict(self):
        status, out = self._post("/assets", self._payload())
        self.assertEqual(status, 201, out)
        digest = out["asset"]["content_digest"]

        status, out = self._get("/assets/expert:api/1.0.0")
        self.assertEqual(status, 200)
        self.assertEqual(out["content"]["expert_id"], "api")
        self.assertEqual(out["asset"]["content_digest"], digest)

        status, out = self._post("/assets", self._payload(
            content={"expert_id": "api", "changed": True}))
        self.assertEqual(status, 409, out)

    def test_exact_read_missing_version_is_404(self):
        status, out = self._get("/assets/expert:api/9.9.9")
        self.assertEqual(status, 404)

    def test_lifecycle_and_dependents(self):
        self._post("/assets", self._payload(
            asset_id="tool:api-dep", asset_type="tool",
            namespace="shared_validated", version="1",
            content={"tool": "dep"}))
        status, out = self._post("/assets", self._payload(
            asset_id="skill:api-user", asset_type="skill", version="1",
            content={"skill": "user"},
            dependency_refs=["tool:api-dep@1"]))
        self.assertEqual(status, 201, out)

        status, out = self._post(
            "/assets/skill:api-user/1/lifecycle",
            {"to_state": "validating"})
        self.assertEqual(status, 200, out)
        status, out = self._post(
            "/assets/skill:api-user/1/lifecycle",
            {"to_state": "approved", "evidence_refs": ["eval:api"]})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["asset"]["lifecycle_state"], "approved")

        status, out = self._get("/assets/tool:api-dep/1/dependents")
        self.assertEqual(status, 200)
        self.assertIn("skill:api-user@1", out["dependents"])

    def test_dependency_endpoint_reports_missing(self):
        self._post("/assets", self._payload(
            asset_id="skill:broken", asset_type="skill",
            version="1", content={"broken": True},
            dependency_refs=["tool:nope@1"]))
        status, out = self._get("/assets/skill:broken/1/dependencies")
        self.assertEqual(status, 409, out)
        self.assertIn("nope", out["error"])

    def test_mapping_and_gc_endpoints(self):
        status, out = self._get("/assets/mapping")
        self.assertEqual(status, 200)
        self.assertTrue(any(m["asset_id"].startswith("expert:")
                            for m in out["mappings"]))
        status, out = self._get("/assets/gc/candidates")
        self.assertEqual(status, 200)
        self.assertIn("candidates", out)

    def test_manifest_ref_endpoint(self):
        status, out = self._post("/assets", self._payload(
            asset_id="expert:ref", version="1",
            content={"expert_id": "ref"}))
        self.assertEqual(status, 201, out)
        status, out = self._get("/assets/expert:ref/1/ref")
        self.assertEqual(status, 200, out)
        self.assertEqual(set(out["ref"]),
                         {"asset_id", "version", "content_digest"})
        self.assertEqual(out["ref"]["asset_id"], "expert:ref")

    def test_list_filter(self):
        status, out = self._get("/assets?asset_type=skill")
        self.assertEqual(status, 200)
        self.assertTrue(all(a["asset_type"] == "skill"
                            for a in out["assets"]))


if __name__ == "__main__":
    unittest.main()
