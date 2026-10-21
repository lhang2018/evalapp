# -*- coding: utf-8 -*-
"""Event 契约字段批（GAP 第一批第5项·子步⑤a，契约 §3）。

补齐四缺口：schema_version（全仓零匹配→落字段+持久化）、payload_ref
（契约 local-uri-or-inline-small-payload）、privacy_level（契约四值
public/internal/sensitive/restricted 映射）、sync_status=acknowledged
（contracts.py CONFLICT_DECISIONS 裁定补齐：入库即确认）。
"""

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request

from evoagent.api.events_sync import _one as parse_event_rg


def parse_event(raw):
    return parse_event_rg(raw, time.time())
from evoagent.core.events import EventBus, RunEvent


class TestEventContractFields(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_evt_")
        self.bus = EventBus(db_path=tempfile.mktemp(dir=self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_schema_version_field_and_persistence(self):
        evt = self.bus.emit(event_type="task.started", task_id="t1")
        self.assertEqual(evt.schema_version, "1.0")
        rows = [r for r in self.bus.query() if r.event_id == evt.event_id]
        self.assertTrue(rows)
        self.assertEqual(rows[0].schema_version, "1.0")

    def test_payload_ref_roundtrip(self):
        evt = self.bus.emit(event_type="tool.completed", task_id="t2",
                            payload_ref="file://evidence/att_1.json")
        self.assertEqual(evt.payload_ref, "file://evidence/att_1.json")
        row = [r for r in self.bus.query()
               if r.event_id == evt.event_id][0]
        self.assertEqual(row.payload_ref, "file://evidence/att_1.json")

    def test_privacy_level_contract_mapping(self):
        # 四值映射：standard→internal, sensitive→sensitive,
        # secret/private→restricted（contracts.py 裁定口径）
        self.assertEqual(
            RunEvent(privacy_classification="standard").privacy_level,
            "internal")
        self.assertEqual(
            RunEvent(privacy_classification="sensitive").privacy_level,
            "sensitive")
        self.assertEqual(
            RunEvent(privacy_classification="secret").privacy_level,
            "restricted")
        self.assertEqual(
            RunEvent(privacy_classification="private").privacy_level,
            "restricted")
        self.assertEqual(
            RunEvent().to_dict()["privacy_level"], "internal")

    def test_ingest_marks_acknowledged(self):
        evt = RunEvent(event_id="evt_ack_1", event_type="tool.completed")
        self.assertTrue(self.bus.ingest(evt))
        self.assertEqual(evt.sync_status, "acknowledged")
        row = self.bus.query(task_id=None, limit=10)
        row = [r for r in row if r.event_id == "evt_ack_1"][0]
        self.assertEqual(row.sync_status, "acknowledged")

    def test_parse_event_accepts_new_fields(self):
        raw = {
            "event_id": "evt_9abcdef0123456789",  # 6-64 位
            "event_type": "tool.completed",
            "timestamp": time.time(), "schema_version": "1.0",
            "payload_ref": "file://c/att.json", "severity": "info",
            "clock_source": "device"}
        evt = parse_event(raw)
        self.assertEqual(evt.schema_version, "1.0")
        self.assertEqual(evt.payload_ref, "file://c/att.json")

    def test_parse_event_rejects_bad_payload_ref(self):
        raw = {"event_id": "evt_9abcdef0123456789", "event_type": "t",
               "timestamp": 1.0,
               "payload_ref": "x" * 600}
        self.assertRaises(ValueError, parse_event, raw)

    def test_to_dict_shape_contract(self):
        d = RunEvent().to_dict()
        for k in ("schema_version", "payload_ref", "privacy_level",
                  "sync_status"):
            self.assertIn(k, d)


if __name__ == "__main__":
    unittest.main()
