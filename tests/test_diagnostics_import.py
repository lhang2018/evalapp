"""离线诊断包受控导入（补充 C §8）。"""

import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.task import Task


def make_event(event_id, **overrides):
    data = {
        "event_id": event_id,
        "event_type": "client_task_queued",
        "timestamp": time.time(),
        "severity": "info",
        "payload": {"note": "ok"},
    }
    data.update(overrides)
    return data


def make_entries(events=None, extra=None, manifest=None):
    payload = events if events is not None else [make_event(
        "evt_import_000000_01")]
    if manifest is None:
        manifest = {
            "format_version": "1.0",
            "exported_at": time.time(),
            "filters": {"scope": "all"},
            "files": [],
        }
    entries = {
        "manifest.json": json.dumps(manifest, ensure_ascii=False).encode(),
        "device-info.json": json.dumps(
            {"app_version": "1.0.0", "platform": "android"}).encode(),
        "events.jsonl": "\n".join(
            json.dumps(item, ensure_ascii=False) for item in payload).encode(),
    }
    if manifest.get("files") == []:
        manifest = dict(manifest, files=sorted(entries))
        entries["manifest.json"] = json.dumps(
            manifest, ensure_ascii=False).encode()
    if extra:
        entries.update(extra)
    return entries


def with_checksums(entries):
    out = {name: data for name, data in entries.items()
           if name != "checksums.sha256"}
    lines = [f"{hashlib.sha256(data).hexdigest()}  {name}"
             for name, data in sorted(out.items())]
    out["checksums.sha256"] = ("\n".join(lines) + "\n").encode()
    return out


def to_zip(entries, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def build_package(events=None, extra=None, manifest=None,
                  compression=zipfile.ZIP_DEFLATED):
    return to_zip(with_checksums(make_entries(events, extra, manifest)),
                  compression)


class TestDiagnosticsImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_import_")
        cls.app = App(data_dir=cls.tmp)
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"
        cls.task = Task(task_id="task_import_known", user_goal="诊断导入")
        cls.app.db.save_task(cls.task)
        cls.app.db.save_runtime_manifest(
            "manifest_import_1", cls.task.task_id, "run_import_known",
            "device_import_1", "inst_import_1", {"component": "test"})

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.app.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, data, headers=None):
        merged = {"Content-Type": "application/zip"}
        if headers:
            merged.update(headers)
        req = urllib.request.Request(
            self.base + "/diagnostics/import", data=data, headers=merged)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=15) as r:
            return r.status, json.loads(r.read().decode())

    def test_valid_package_goes_to_quarantine(self):
        package = build_package(
            [make_event("evt_import_valid_01"),
             make_event("evt_import_valid_02")],
            extra={"attachments/screen.png": b"\x89PNG fake"},
        )
        st, out = self._post(package, {"X-File-Name": "diag.zip"})
        self.assertEqual(st, 201)
        record = out["import"]
        self.assertEqual(record["source"], "offline_import")
        self.assertEqual(record["status"], "quarantined")
        self.assertEqual(record["event_count"], 2)
        self.assertEqual(record["file_name"], "diag.zip")
        self.assertEqual(out["needs_review"], 0)

        st, listing = self._get("/diagnostics/imports")
        self.assertEqual(st, 200)
        self.assertTrue(any(item["import_id"] == record["import_id"]
                            for item in listing["imports"]))
        st, detail = self._get(f"/diagnostics/imports/{record['import_id']}")
        self.assertEqual(st, 200)
        self.assertEqual(len(detail["events"]), 2)
        self.assertTrue(all(event["review_status"] == "accepted"
                            for event in detail["events"]))
        self.assertEqual(detail["events"][0]["payload"], {"note": "ok"})

        audit = [item for item in self.app.audit.list(limit=500)
                 if item["action"] == "diagnostics.offline_import"]
        self.assertTrue(any(item["target"] == record["import_id"]
                            and item["outcome"] == "ok" for item in audit))

    def test_duplicate_package_is_idempotent(self):
        package = build_package([make_event("evt_import_dup_0001")])
        st1, out1 = self._post(package)
        st2, out2 = self._post(package)
        self.assertEqual(st1, 201)
        self.assertEqual(st2, 200)
        self.assertTrue(out2["duplicate"])
        self.assertEqual(out1["import"]["import_id"],
                         out2["import"]["import_id"])
        events = self.app.db.list_diagnostics_import_events(
            out1["import"]["import_id"])
        self.assertEqual(len(events), 1)

    def test_path_traversal_rejected(self):
        st, out = self._post(build_package(
            extra={"../evil.txt": b"x"}))
        self.assertEqual(st, 400)
        self.assertIn("非法路径", out["error"])
        st, out = self._post(build_package(extra={"/abs.txt": b"x"}))
        self.assertEqual(st, 400)

    def test_zip_ratio_guard(self):
        bomb = b"\x00" * (2 * 1024 * 1024)
        st, out = self._post(build_package(
            extra={"attachments/huge.txt": bomb}))
        self.assertEqual(st, 413)
        self.assertIn("压缩比", out["error"])

    def test_disallowed_entries_rejected(self):
        st, out = self._post(build_package(extra={"script.sh": b"echo hi"}))
        self.assertEqual(st, 400)
        self.assertIn("不允许的文件", out["error"])
        st, out = self._post(build_package(
            extra={"attachments/run.exe": b"MZ"}))
        self.assertEqual(st, 400)

    def test_checksum_mismatch_and_missing(self):
        entries = with_checksums(make_entries())
        entries["events.jsonl"] = entries["events.jsonl"] + b"\n"
        st, out = self._post(to_zip(entries))
        self.assertEqual(st, 400)
        self.assertIn("SHA-256 不匹配", out["error"])

        entries = make_entries()
        st, out = self._post(to_zip(entries))
        self.assertEqual(st, 400)
        self.assertIn("缺少必需文件", out["error"])

        entries = with_checksums(make_entries())
        lines = entries["checksums.sha256"].decode().splitlines()
        entries["checksums.sha256"] = (
            "\n".join(line for line in lines
                      if "events.jsonl" not in line) + "\n").encode()
        st, out = self._post(to_zip(entries))
        self.assertEqual(st, 400)
        self.assertIn("缺少摘要", out["error"])

    def test_manifest_format_version_enforced(self):
        st, out = self._post(build_package(manifest={
            "format_version": "9.9", "exported_at": time.time()}))
        self.assertEqual(st, 400)
        self.assertIn("格式版本", out["error"])

    def test_relationship_review_marks_conflicts(self):
        package = build_package([
            make_event("evt_import_rel_0001",
                       task_id=self.task.task_id,
                       run_id="run_import_known",
                       runtime_manifest_id="manifest_import_1"),
            make_event("evt_import_rel_0002", task_id="task_missing"),
            make_event("evt_import_rel_0003", run_id="run_no_manifest"),
            make_event("evt_import_rel_0004",
                       task_id=self.task.task_id,
                       run_id="run_import_known",
                       runtime_manifest_id="manifest_missing_1"),
        ])
        st, out = self._post(package)
        self.assertEqual(st, 201)
        self.assertEqual(out["needs_review"], 3)
        _, detail = self._get(
            f"/diagnostics/imports/{out['import']['import_id']}")
        by_id = {event["event_id"]: event for event in detail["events"]}
        self.assertEqual(by_id["evt_import_rel_0001"]["review_status"],
                         "accepted")
        self.assertEqual(by_id["evt_import_rel_0002"]["conflict_reason"],
                         "unknown_task")
        self.assertEqual(by_id["evt_import_rel_0003"]["conflict_reason"],
                         "missing_manifest")
        self.assertEqual(by_id["evt_import_rel_0004"]["conflict_reason"],
                         "unknown_manifest")

    def test_content_type_and_admin_required(self):
        st, out = self._post(build_package(), {"Content-Type": "text/plain"})
        self.assertEqual(st, 415)

        self.app.config.api_token = "operator-token"
        self.app.config.api_admin_token = "admin-token"
        try:
            st, out = self._post(build_package(
                [make_event("evt_import_auth_0001")]),
                {"Authorization": "Bearer operator-token"})
            self.assertEqual(st, 403)
            st, out = self._post(build_package(
                [make_event("evt_import_auth_0002")]),
                {"Authorization": "Bearer admin-token"})
            self.assertEqual(st, 201)
        finally:
            self.app.config.api_token = None
            self.app.config.api_admin_token = None


if __name__ == "__main__":
    unittest.main()
