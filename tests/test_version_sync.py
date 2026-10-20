"""双端版本对账、应用回执、同步回执与运行清单（双端协同 §3/§5/§10）。"""

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
from evoagent.evolution.candidates import CandidateStatus

INSTALLATION = "inst-abcdefgh-0001"
DEVICE = "device-abcdefgh-0001"


class TestVersionSyncApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_version_sync_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=18304)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:18304"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.app.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _publish(self, **overrides):
        kwargs = {
            "version": "v9.0.0",
            "target_platforms": ["android"],
            "min_app_version": "0.1.0",
            "required_tool_contracts": [],
        }
        kwargs.update(overrides)
        manifest = self.app.releases.build_manifest(**kwargs)
        deployed = self.app.releases.deploy(manifest.release_id,
                                            gate_passed=True)
        self.assertTrue(deployed.get("ok"))
        return manifest, deployed

    def _reconcile(self, **overrides):
        payload = {
            "installation_id": INSTALLATION,
            "device_id": DEVICE,
            "platform": "android",
            "app_version": "0.1.0",
            "current_release_id": self.app.release_manifest.release_id,
            "tool_contracts": [],
        }
        payload.update(overrides)
        return self._post("/sync/reconcile", payload)

    # ---- 对账 ----
    def test_update_available_with_signed_target_and_rollback(self):
        manifest, deployed = self._publish(
            version="v9.1.0", rollback_target=None)
        st, out = self._reconcile()
        self.assertEqual(st, 200)
        self.assertEqual(out["decision"], "update_available")
        self.assertTrue(out["update"]["available"])
        self.assertEqual(out["target"]["release_id"], manifest.release_id)
        self.assertTrue(out["target"]["signature_ok"])
        self.assertEqual(out["target"]["bundle_kind"], "capability")
        self.assertEqual(out["target"]["delivery_mode"], "capability_slot")
        self.assertEqual(out["target"]["rollback_target"],
                         deployed.get("previous"))
        stored = self.app.db.get_installation(INSTALLATION)
        self.assertEqual(stored["target_release_id"], manifest.release_id)
        self.assertEqual(stored["current_release_id"],
                         self.app.release_manifest.release_id)

    def test_up_to_date_when_current_matches(self):
        manifest, _ = self._publish(version="v9.2.0")
        st, out = self._reconcile(current_release_id=manifest.release_id)
        self.assertEqual(st, 200)
        self.assertEqual(out["decision"], "up_to_date")
        self.assertFalse(out["update"]["available"])

    def test_old_app_version_blocks(self):
        self._publish(version="v9.3.0", min_app_version="2.0.0")
        st, out = self._reconcile(app_version="1.9.9")
        self.assertEqual(st, 200)
        self.assertEqual(out["decision"], "blocked")
        names = {c["name"]: c for c in out["checks"]}
        self.assertFalse(names["min_app_version"]["ok"])

    def test_platform_mismatch_blocks(self):
        self._publish(version="v9.4.0", target_platforms=["ios"])
        st, out = self._reconcile(platform="android")
        self.assertEqual(st, 200)
        self.assertEqual(out["decision"], "blocked")
        self.assertIn("platform_compatible",
                      {c["name"] for c in out["checks"]
                       if not c["ok"]})

    def test_missing_tool_contract_blocks_until_reported(self):
        self._publish(version="v9.5.0",
                      required_tool_contracts=["vision_ocr"])
        st, out = self._reconcile(tool_contracts=[])
        self.assertEqual(out["decision"], "blocked")
        st, out = self._reconcile(tool_contracts=["vision_ocr"])
        self.assertEqual(out["decision"], "update_available")

    def test_gray_scope_excludes_installation(self):
        self._publish(version="v9.6.0")
        # 0% 灰度时任何安装实例都拿不到目标
        manifest = self.app.releases.manifests[
            self.app.releases.current_release_id]
        self.app.releases.deploy(manifest.release_id, gate_passed=True,
                                 gray_percent=0)
        st, out = self._reconcile()
        self.assertEqual(st, 200)
        self.assertEqual(out["decision"], "not_eligible")
        self.assertFalse(out["update"]["in_gray_scope"])
        self.assertIsNone(out["target"])

    def test_invalid_installation_rejected(self):
        st, out = self._post("/sync/reconcile", {
            "installation_id": "bad", "platform": "android",
            "app_version": "0.1.0"})
        self.assertEqual(st, 400)

    # ---- 应用回执 ----
    def test_activation_then_rollback(self):
        manifest, _ = self._publish(version="v9.7.0")
        previous = self.app.release_manifest.release_id
        st, out = self._reconcile()
        self.assertEqual(out["decision"], "update_available")

        st, activated = self._post("/sync/apply-result", {
            "installation_id": INSTALLATION, "action": "activated",
            "release_id": manifest.release_id, "apply_id": "apply-activate-01"})
        self.assertEqual(st, 200)
        self.assertFalse(activated["duplicate"])
        self.assertEqual(activated["installation"]["current_release_id"],
                         manifest.release_id)
        self.assertEqual(activated["installation"]["status"], "activated")

        st, rolled = self._post("/sync/apply-result", {
            "installation_id": INSTALLATION, "action": "rolled_back",
            "release_id": manifest.release_id, "reason": "健康检查失败",
            "apply_id": "apply-rollback-01"})
        self.assertEqual(st, 200)
        self.assertEqual(rolled["installation"]["current_release_id"],
                         previous)
        self.assertEqual(rolled["installation"]["status"], "rolled_back")

        records = self.app.db.list_device_deployments(INSTALLATION)
        actions = [r["action"] for r in records]
        self.assertIn("activated", actions)
        self.assertIn("rolled_back", actions)
        audit = [a for a in self.app.db.list_audit(limit=200)
                 if a["action"] == "sync.apply_result"]
        self.assertTrue(audit)

    def test_apply_result_is_idempotent(self):
        manifest, _ = self._publish(version="v9.8.0")
        body = {"installation_id": INSTALLATION,
                "action": "download_started",
                "release_id": manifest.release_id,
                "apply_id": "apply-download-01"}
        st1, out1 = self._post("/sync/apply-result", body)
        st2, out2 = self._post("/sync/apply-result", body)
        self.assertEqual((st1, st2), (200, 200))
        self.assertFalse(out1["duplicate"])
        self.assertTrue(out2["duplicate"])

    def test_apply_result_missing_installation(self):
        st, out = self._post("/sync/apply-result", {
            "installation_id": "inst-missing-0001", "action": "verified"})
        self.assertEqual(st, 404)

    def test_apply_result_unknown_action(self):
        st, out = self._post("/sync/apply-result", {
            "installation_id": INSTALLATION, "action": "explode"})
        self.assertEqual(st, 400)

    # ---- 同步回执 ----
    def test_batch_receipt_replay(self):
        events = [{
            "event_id": f"evt_{30 + i:016d}", "event_type": "client_note",
            "timestamp": time.time(), "payload": {"i": i},
            "privacy_classification": "standard"} for i in range(2)]
        body = {"batch_id": "batch_abc_0001", "client_id": "android",
                "installation_id": INSTALLATION, "events": events}
        st1, out1 = self._post("/events/sync", body)
        st2, out2 = self._post("/events/sync", body)
        self.assertEqual((st1, st2), (200, 200))
        self.assertEqual(out1["receipt"]["receipt_id"],
                         out2["receipt"]["receipt_id"])
        self.assertFalse(out1["receipt"]["replayed"])
        self.assertTrue(out2["receipt"]["replayed"])
        self.assertEqual(out1["accepted"], out2["accepted"])
        stored = self.app.db.get_sync_batch("batch_abc_0001")
        self.assertEqual(stored["accepted"], 2)

    def test_invalid_batch_id_rejected(self):
        st, out = self._post("/events/sync", {
            "batch_id": "bad", "events": [{
                "event_id": "evt_0000000000000100",
                "event_type": "x", "timestamp": time.time()}]})
        self.assertEqual(st, 400)

    # ---- 运行清单 ----
    def test_task_records_device_runtime_manifest(self):
        body = {"goal": "现在几点了", "intent": "time_query",
                "device_id": DEVICE, "installation_id": INSTALLATION,
                "app_version": "0.1.0",
                "client_request_id": "req-manifest-0001"}
        st, out = self._post("/tasks", body)
        self.assertEqual(st, 202)
        task_id = out["task_id"]
        manifests = []
        deadline = time.time() + 10
        while time.time() < deadline:
            st, data = self._get(f"/tasks/{task_id}/manifests")
            manifests = data.get("manifests") or []
            if manifests:
                break
            time.sleep(0.05)
        self.assertTrue(manifests)
        manifest = manifests[0]
        self.assertEqual(manifest["device_id"], DEVICE)
        self.assertEqual(manifest["installation_id"], INSTALLATION)
        inner = manifest["manifest"]
        self.assertEqual(inner["app_version"], "0.1.0")
        self.assertEqual(inner["run_id"], f"run_{task_id}")
        self.assertIn("release_id", inner)

        st, run = self._get(f"/runs/run_{task_id}/manifest")
        self.assertEqual(st, 200)
        self.assertEqual(run["manifest"]["device_id"], DEVICE)

    def test_device_field_validation(self):
        st, out = self._post("/tasks", {
            "goal": "x", "device_id": "bad"})
        self.assertEqual(st, 400)

    # ---- 候选状态同步 ----
    def _candidate_at(self, target):
        cand = self.app.evolution.propose_candidate(
            "v0.1.0", "workflow/demo", "示例问题", "示例假设")
        if target == "CANDIDATE_CREATED":
            return cand
        for name in ("TESTING", "EVALUATING", "APPROVAL_PENDING",
                     "RELEASE_READY", "DEPLOYING", "MONITORING", "PROMOTED"):
            cand.transition(CandidateStatus(name), "测试推进")
            if name == target:
                break
        return cand

    def _candidates(self):
        st, out = self._get(
            f"/sync/candidates?installation_id={INSTALLATION}&limit=100")
        self.assertEqual(st, 200)
        return out

    def test_candidate_states_after_evaluation(self):
        self._reconcile()
        cand = self._candidate_at("APPROVAL_PENDING")
        out = self._candidates()
        item = next(c for c in out["candidates"]
                    if c["candidate_id"] == cand.candidate_id)
        self.assertEqual(item["status"], "APPROVAL_PENDING")
        self.assertEqual(item["target_component"], "workflow/demo")
        self.assertEqual(item["risk_level"], cand.risk_level)
        # 假设与证据留在服务端，不下发客户端
        self.assertNotIn("hypothesis", item)
        self.assertNotIn("root_cause_evidence", item)
        rows = self.app.db.list_device_candidate_states(
            INSTALLATION, limit=100)
        row = next(r for r in rows
                   if r["candidate_id"] == cand.candidate_id)
        self.assertEqual(row["status"], "APPROVAL_PENDING")

    def test_preevaluation_candidates_hidden(self):
        self._reconcile()
        early = self._candidate_at("CANDIDATE_CREATED")
        out = self._candidates()
        ids = {c["candidate_id"] for c in out["candidates"]}
        self.assertNotIn(early.candidate_id, ids)

    def test_candidate_state_release_link_and_refresh(self):
        self._reconcile()
        cand = self._candidate_at("RELEASE_READY")
        cand.transition(CandidateStatus.DEPLOYING, "受控部署",
                        {"release_id": "rel_cand_0001"})
        out = self._candidates()
        item = next(c for c in out["candidates"]
                    if c["candidate_id"] == cand.candidate_id)
        self.assertEqual(item["release_id"], "rel_cand_0001")
        self.assertEqual(item["status"], "DEPLOYING")

        rows = self.app.db.list_device_candidate_states(
            INSTALLATION, limit=100)
        first = next(r for r in rows
                     if r["candidate_id"] == cand.candidate_id)
        cand.transition(CandidateStatus.MONITORING, "进入发布后监测")
        out = self._candidates()
        item = next(c for c in out["candidates"]
                    if c["candidate_id"] == cand.candidate_id)
        self.assertEqual(item["status"], "MONITORING")
        rows = self.app.db.list_device_candidate_states(
            INSTALLATION, limit=100)
        again = next(r for r in rows
                     if r["candidate_id"] == cand.candidate_id)
        self.assertEqual(again["status"], "MONITORING")
        self.assertEqual(again["first_seen_at"], first["first_seen_at"])

    def test_candidate_states_validation(self):
        st, _ = self._get("/sync/candidates?installation_id=bad")
        self.assertEqual(st, 400)
        st, _ = self._get(
            "/sync/candidates?installation_id=inst-unknown-9999")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
