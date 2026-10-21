# -*- coding: utf-8 -*-
"""node_run_id 统一标识（GAP ⑤d，契约统一标识）。

DAG 节点执行实体 ID：一次节点执行（含其重试）= 一个 node_run，
ID 形如 nod_{hex16}；入运行事件 payload（workflow_node_run）与任务
清单（task.runtime_manifest["node_runs"]）。
"""

import shutil
import tempfile
import unittest

import evoagent.core.task as T
from evoagent.app import App
from evoagent.workflows.models import WorkflowDef, WorkflowNode


class TestNodeRunId(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_n5d_")
        self.app = App(data_dir=self.tmp)
        self.wf = WorkflowDef(
            workflow_id="wf_n5d", version="1.2.0", name="节点标识测试流",
            nodes=[
                WorkflowNode(node_id="a", tool="calculate",
                             args={"expression": "3+4"}),
                WorkflowNode(node_id="b", tool="calculate",
                             args={"expression": "5*6"}),
            ])
        self.app._workflows["wf_n5d"] = self.wf

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_node_run_id_shape_and_uniqueness(self):
        result = self.app.workflow_engine.run(self.wf, {"goal": "算两个"})
        self.assertTrue(result.ok, result.error)
        ids = [s.get("node_run_id") for s in result.node_stats]
        for nid in ids:
            self.assertRegex(nid, r"^nod_[0-9a-f]{16}$")
        self.assertEqual(len(set(ids)), len(ids))

    def test_event_payload_carries_node_run_id(self):
        result = self.app.workflow_engine.run(self.wf, {"goal": "算两个"})
        ids = {s["node_run_id"] for s in result.node_stats}
        events = [e for e in self.app.bus.query()
                  if e.event_type == "workflow_node_run"]
        self.assertTrue(events, "应发出 workflow_node_run 事件")
        payload_ids = {e.payload.get("node_run_id") for e in events}
        self.assertEqual(ids, payload_ids)
        for e in events:
            self.assertEqual(e.payload.get("workflow_id"), "wf_n5d")
            self.assertTrue(e.payload.get("node_id"))

    def test_task_record_carries_node_run_id(self):
        task = T.Task(task_id="t_n5d", user_goal="跑节点标识流")
        task.workflow_ref = "wf_n5d"
        self.app.db.save_task(task)
        out = self.app.controller.execute(task)
        rows = (out.runtime_manifest or {}).get("node_runs") or []
        self.assertTrue(rows, "任务清单应记录 node_runs")
        by_node = {r["node"]: r["node_run_id"] for r in rows}
        self.assertEqual(set(by_node), {"a", "b"})
        for nid in by_node.values():
            self.assertRegex(nid, r"^nod_[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
