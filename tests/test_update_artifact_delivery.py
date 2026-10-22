"""ALN-11 服务端真实制品 envelope 与 Range 下载。"""

import hashlib
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from evoagent.api.server import ApiServer
from evoagent.app import App


class TestUpdateArtifactDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_update_artifact_")
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
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _publish(self, release_id, body, **overrides):
        artifact = Path(self.app.releases.artifact_dir) / f"{release_id}.bundle"
        artifact.write_bytes(body)
        fields = dict(
            release_id=release_id,
            version="v9.1.0",
            channel="stable",
            target_platforms=["android"],
            min_app_version="0.1.0",
            bundle_kind="workflow",
            permission_requirements=["camera"],
            required_tool_contracts=["open_app"],
            artifact_url=f"{self.base}/artifacts/{release_id}",
            artifact_sha256=hashlib.sha256(body).hexdigest(),
            artifact_size=len(body),
            artifact_signature="ab" * 256,
            artifact_signing_key_id="release-key-1",
            signature_schema="evoagent.release.signing/1",
        )
        fields.update(overrides)
        manifest = self.app.releases.build_manifest(**fields)
        self.app.releases.current_release_id = manifest.release_id
        return manifest

    def _reconcile(self):
        body = {
            "installation_id": "inst_update_0001",
            "platform": "android",
            "app_version": "0.1.0",
            "current_release_id": None,
            "tool_contracts": ["open_app"],
        }
        req = urllib.request.Request(
            f"{self.base}/sync/reconcile",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())

    def test_reconcile_emits_full_artifact_envelope(self):
        payload = json.dumps({"workflow": "demo/v2"}).encode()
        manifest = self._publish("rel_envelope_1", payload)
        status, out = self._reconcile()
        self.assertEqual(status, 200)
        self.assertEqual(out["decision"], "update_available")
        target = out["target"]
        self.assertEqual(target["artifact_url"],
                         f"{self.base}/artifacts/{manifest.release_id}")
        self.assertEqual(target["artifact_sha256"],
                         hashlib.sha256(payload).hexdigest())
        self.assertEqual(target["artifact_size"], len(payload))
        self.assertEqual(target["artifact_signature"], "ab" * 256)
        self.assertEqual(target["signing_key_id"], "release-key-1")
        self.assertEqual(target["signature_schema"],
                         "evoagent.release.signing/1")
        self.assertEqual(target["required_permissions"], ["camera"])
        self.assertEqual(target["required_tool_contracts"], ["open_app"])

    def test_incomplete_envelope_blocks_update(self):
        payload = b'{"workflow":"demo/v2"}'
        self._publish("rel_envelope_2", payload, artifact_sha256=None)
        status, out = self._reconcile()
        self.assertEqual(status, 200)
        self.assertEqual(out["decision"], "blocked")
        checks = {item["name"]: item for item in out["checks"]}
        self.assertFalse(checks["artifact_envelope"]["ok"])

    def test_artifact_endpoint_serves_range_and_full_bytes(self):
        payload = json.dumps({"workflow": "demo/v2",
                              "tools": ["open_app"]}).encode()
        manifest = self._publish("rel_range_1", payload)
        url = f"{self.base}/artifacts/{manifest.release_id}"

        req = urllib.request.Request(url, headers={"Range": "bytes=4-11"})
        with urllib.request.urlopen(req, timeout=10) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), payload[4:12])
            self.assertEqual(
                response.headers["Content-Range"],
                f"bytes 4-11/{len(payload)}")
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")

        with urllib.request.urlopen(url, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), payload)

        req = urllib.request.Request(
            url, headers={"Range": f"bytes={len(payload)}-"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 416)

    def test_unknown_artifact_is_not_served(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/artifacts/no_such", timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    def test_new_envelope_fields_do_not_break_metadata_signature(self):
        payload = b'{"workflow":"demo/v2"}'
        manifest = self._publish("rel_sig_1", payload)
        verdict = self.app.releases.verify(manifest.release_id)
        self.assertTrue(verdict["ok"], verdict)
        self.assertTrue(self.app.releases.compatibility_complete(manifest))


if __name__ == "__main__":
    unittest.main()
