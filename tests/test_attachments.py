"""诊断附件分块上传（补充 C §6）。"""

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from evoagent.api.server import ApiServer
from evoagent.app import App

CHUNK = 16 * 1024


def aid(n):
    return f"att_{n:016d}"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def init_body(attachment_id, data, **overrides):
    body = {
        "attachment_id": attachment_id,
        "file_name": "screen.png",
        "content_type": "image/png",
        "size_bytes": len(data),
        "sha256": digest(data),
        "chunk_size": CHUNK,
        "consent": True,
    }
    body.update(overrides)
    return body


class TestAttachmentApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_attachment_")
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

    def _request(self, method, path, data=None, headers=None):
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def _post(self, path, payload):
        status, _, raw = self._request(
            "POST", path, json.dumps(payload).encode(),
            {"Content-Type": "application/json"})
        return status, json.loads(raw.decode())

    def _put_chunk(self, attachment_id, index, data):
        status, _, raw = self._request(
            "PUT", f"/attachments/{attachment_id}/chunks/{index}", data,
            {"Content-Type": "application/octet-stream"})
        return status, json.loads(raw.decode())

    def _upload_all(self, attachment_id, data, chunk_size=CHUNK):
        for index, start in enumerate(range(0, len(data), chunk_size)):
            status, _ = self._put_chunk(
                attachment_id, index, data[start:start + chunk_size])
            self.assertEqual(status, 200)

    def test_init_requires_consent_valid_sha_and_size(self):
        data = b"hello"
        status, out = self._post("/attachments",
                                 init_body(aid(1), data, consent=False))
        self.assertEqual(status, 403)
        self.assertIn("授权", out["error"])

        status, out = self._post("/attachments",
                                 init_body(aid(2), data, sha256="xyz"))
        self.assertEqual(status, 400)

        status, out = self._post(
            "/attachments",
            init_body(aid(3), data, size_bytes=50 * 1024 * 1024 + 1))
        self.assertEqual(status, 413)

        status, out = self._post(
            "/attachments", init_body(aid(4), data, chunk_size=1024))
        self.assertEqual(status, 400)

    def test_init_idempotent_and_conflict(self):
        data = b"payload-for-idempotency" * 10
        body = init_body(aid(5), data)
        status, first = self._post("/attachments", body)
        self.assertEqual(status, 201)
        self.assertEqual(first["attachment_id"], aid(5))
        self.assertEqual(first["status"], "uploading")

        status, second = self._post("/attachments", body)
        self.assertEqual(status, 200)
        self.assertEqual(second["chunk_count"], first["chunk_count"])

        status, out = self._post(
            "/attachments",
            init_body(aid(5), b"different", file_name="other.png"))
        self.assertEqual(status, 409)

    def test_chunked_upload_complete_and_content(self):
        data = bytes(range(256)) * 156  # 39936 字节，3 块（末块 7168）
        body = init_body(aid(6), data)
        status, _ = self._post("/attachments", body)
        self.assertEqual(status, 201)

        status, chunk0 = self._put_chunk(aid(6), 0, data[:CHUNK])
        self.assertEqual(status, 200)
        self.assertEqual(chunk0["received_bytes"], CHUNK)
        self.assertEqual(chunk0["received_chunks"], [0])
        self.assertEqual(chunk0["chunk_sha256"], digest(data[:CHUNK]))

        self._upload_all(aid(6), data)

        status, out = self._post(f"/attachments/{aid(6)}/complete", {})
        self.assertEqual(status, 200)
        self.assertTrue(out["verified"])
        self.assertEqual(out["attachment"]["status"], "complete")
        self.assertEqual(out["attachment"]["received_bytes"], len(data))

        status, headers, raw = self._request("GET", f"/attachments/{aid(6)}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw.decode())["status"], "complete")

        status, headers, raw = self._request(
            "GET", f"/attachments/{aid(6)}/content")
        self.assertEqual(status, 200)
        self.assertEqual(raw, data)
        self.assertEqual(headers.get("X-Attachment-SHA256"), digest(data))

        # 重复 complete 幂等
        status, again = self._post(f"/attachments/{aid(6)}/complete", {})
        self.assertEqual(status, 200)
        self.assertTrue(again["duplicate"])

    def test_chunk_size_and_index_validation(self):
        data = b"x" * (CHUNK * 2)
        status, _ = self._post("/attachments", init_body(aid(7), data))
        self.assertEqual(status, 201)

        status, out = self._put_chunk(aid(7), 0, b"short")
        self.assertEqual(status, 400)

        status, out = self._put_chunk(aid(7), 0, b"x" * (CHUNK + 1))
        self.assertEqual(status, 413)

        status, out = self._put_chunk(aid(7), 5, b"x" * CHUNK)
        self.assertEqual(status, 400)

        status, out = self._put_chunk(aid(7), 1, b"")
        self.assertEqual(status, 400)

        status, out = self._put_chunk("att_unknown_000000", 0, b"x" * CHUNK)
        self.assertEqual(status, 404)

    def test_complete_requires_all_chunks(self):
        data = b"y" * (CHUNK + 100)
        status, _ = self._post("/attachments", init_body(aid(8), data))
        self.assertEqual(status, 201)
        self._put_chunk(aid(8), 0, data[:CHUNK])

        status, out = self._post(f"/attachments/{aid(8)}/complete", {})
        self.assertEqual(status, 409)
        self.assertEqual(out["received_chunks"], [0])

        status, _, _ = self._request("GET", f"/attachments/{aid(8)}/content")
        self.assertEqual(status, 409)

    def test_digest_mismatch_then_retry(self):
        data = b"z" * (CHUNK * 2)
        status, _ = self._post("/attachments", init_body(aid(9), data))
        self.assertEqual(status, 201)

        corrupted = b"!" * CHUNK
        self._put_chunk(aid(9), 0, corrupted)
        self._put_chunk(aid(9), 1, data[CHUNK:])

        status, out = self._post(f"/attachments/{aid(9)}/complete", {})
        self.assertEqual(status, 422)
        self.assertEqual(out["expected_sha256"], digest(data))

        status, _, raw = self._request("GET", f"/attachments/{aid(9)}")
        state = json.loads(raw.decode())
        self.assertEqual(state["status"], "uploading")
        # 无法定位损坏分块，服务端丢弃并清空接收进度
        self.assertEqual(state["received_chunks"], [])
        self.assertEqual(state["received_bytes"], 0)

        self._upload_all(aid(9), data)
        status, out = self._post(f"/attachments/{aid(9)}/complete", {})
        self.assertEqual(status, 200)
        self.assertTrue(out["verified"])

    def test_event_attachment_refs_round_trip_and_validation(self):
        refs = [aid(10), aid(11)]
        event = {
            "event_id": "evt_attachment_0001",
            "event_type": "client_task_queued",
            "task_id": "att-task",
            "timestamp": time.time(),
            "payload": {"note": "with refs"},
            "privacy_classification": "standard",
            "attachment_refs": refs,
        }
        status, out = self._post("/events/sync", {"events": [event]})
        self.assertEqual(status, 200)
        self.assertEqual(out["accepted"], ["evt_attachment_0001"])

        rows = self.app.bus.query(task_id="att-task", limit=10)
        self.assertEqual(rows[0].attachment_refs, refs)

        bad = dict(event, event_id="evt_attachment_0002",
                   attachment_refs=["short"])
        status, out = self._post("/events/sync", {"events": [bad]})
        self.assertEqual(status, 400)
        self.assertIn("attachment_refs", out["rejected"][0]["error"])

        too_many = dict(event, event_id="evt_attachment_0003",
                        attachment_refs=[aid(100 + i) for i in range(9)])
        status, out = self._post("/events/sync", {"events": [too_many]})
        self.assertEqual(status, 400)

    def test_content_read_requires_admin_when_auth_enabled(self):
        tmp = tempfile.mkdtemp(prefix="evoagent_attachment_auth_")
        app = App(data_dir=tmp)
        app.config.api_token = "op-secret"
        app.config.api_admin_token = "adm-secret"
        srv = ApiServer(app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            operator = {"Authorization": "Bearer op-secret",
                        "Content-Type": "application/json"}
            admin = {"Authorization": "Bearer adm-secret"}
            data = b"secret-diag"
            body = init_body(aid(12), data)

            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/attachments",
                data=json.dumps(body).encode(), headers=operator)
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 201)

            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/attachments/{aid(12)}/chunks/0",
                data=data, method="PUT",
                headers={"Authorization": "Bearer op-secret"})
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 200)

            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/attachments/{aid(12)}/complete",
                data=b"{}", method="POST", headers=operator)
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 200)

            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/attachments/{aid(12)}/content",
                headers={"Authorization": "Bearer op-secret"})
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("operator 不应读取附件正文")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 403)

            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/attachments/{aid(12)}/content",
                headers=admin)
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.read(), data)
        finally:
            srv.shutdown()
            app.db.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
