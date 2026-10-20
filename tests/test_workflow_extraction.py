"""成功任务 → 稳定工作流（Arch 16.2 / PRD 13.4）。

单次成功不构成证据；候选需重复支持、结构/工具可用性校验，
发布前必须人工批准并绑定工具必填参数。
"""

import shutil
import tempfile
import time
import unittest

from evoagent.app import App
from evoagent.core.task import Task, TaskStatus


class TestWorkflowExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_wfext_")
        self.app = App(data_dir=self.tmp)
        self.extractor = self.app.workflow_extractor
        self._ts = time.time() - 100

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def _seed(self, tools, *, intent="time_query", task_id=None,
              source=None, workflow_ref=None, ok=True):
        task_id = task_id or f"task_{len(self.app.db.list_tasks(1000))}_{intent}"
        task = Task(task_id=task_id, user_goal=intent, intent=intent,
                    task_status=TaskStatus.SUCCEEDED)
        manifest = {"source": source} if source else {}
        task.runtime_manifest = manifest
        task.workflow_ref = workflow_ref
        task.acceptance_criteria = [{"type": "contains", "value": "ok"}]
        task.result_evidence = [{"budget": {"usage": {"tool_calls": len(tools)}}}]
        self.app.db.save_task(task)
        for tool in tools:
            self._ts += 1
            self.app.bus.emit(event_type="tool_call", task_id=task_id,
                              timestamp=self._ts,
                              payload={"tool": tool, "ok": ok})
        return task_id

    # ---- support & extraction ----
    def test_single_success_is_not_enough(self):
        self._seed(["current_time"])
        out = self.extractor.extract()
        self.assertEqual(out["created"], [])
        self.assertEqual(out["skipped"][0]["reason"], "insufficient_support")
        self.assertEqual(out["skipped"][0]["support"], 1)

    def test_repeated_sequence_creates_candidate(self):
        self._seed(["current_time"])
        self._seed(["current_time"])
        out = self.extractor.extract()
        self.assertEqual(len(out["created"]), 1)
        cand = out["created"][0]
        self.assertEqual(cand["intent"], "time_query")
        self.assertEqual(cand["signature"], ["current_time"])
        self.assertEqual(cand["support"], 2)
        self.assertEqual(cand["status"], "candidate")
        definition = cand["definition"]
        self.assertEqual([n["node_id"] for n in definition["nodes"]], ["step_1"])
        self.assertEqual(definition["nodes"][0]["tool"], "current_time")
        self.assertEqual(definition["created_from_task"],
                         cand["sample_tasks"][-1])

    def test_duplicate_extraction_is_skipped(self):
        self._seed(["current_time"])
        self._seed(["current_time"])
        self.assertEqual(len(self.extractor.extract()["created"]), 1)
        again = self.extractor.extract()
        self.assertEqual(again["created"], [])
        self.assertEqual(again["skipped"][0]["reason"], "duplicate")

    def test_min_support_override(self):
        self._seed(["current_time"])
        self._seed(["current_time"])
        out = self.extractor.extract(min_support=3)
        self.assertEqual(out["created"], [])
        self.assertEqual(out["skipped"][0]["reason"], "insufficient_support")

    def test_evaluation_and_workflow_tasks_excluded(self):
        self._seed(["current_time"], source="evaluation")
        self._seed(["current_time"], source="evaluation")
        self._seed(["current_time"], workflow_ref="wf_comic")
        self._seed(["current_time"], workflow_ref="wf_comic")
        out = self.extractor.extract()
        self.assertEqual(out["created"], [])
        self.assertEqual(out["scanned"], 0)

    def test_failed_tool_call_is_not_part_of_signature(self):
        self._seed(["current_time"])
        self._seed(["current_time"])
        self._seed(["calculate"], ok=False)
        self._seed(["calculate"], ok=False)
        out = self.extractor.extract()
        self.assertEqual(len(out["created"]), 1)
        self.assertEqual(out["created"][0]["signature"], ["current_time"])

    # ---- decision gate ----
    def test_rejected_candidate_can_be_extracted_again(self):
        self._seed(["current_time"])
        self._seed(["current_time"])
        first = self.extractor.extract()["created"][0]
        self.extractor.reject(first["candidate_id"], reason="不准确")
        second = self.extractor.extract()["created"][0]
        self.assertNotEqual(first["candidate_id"], second["candidate_id"])
        self.assertEqual(second["status"], "candidate")

    def test_publish_requires_approval(self):
        self._seed(["current_time"])
        self._seed(["current_time"])
        cand = self.extractor.extract()["created"][0]
        with self.assertRaises(ValueError):
            self.extractor.publish(cand["candidate_id"])
        self.assertEqual(
            self.extractor.get(cand["candidate_id"])["status"], "candidate")

    def test_evaluate_reports_binding_and_cost(self):
        self._seed(["calculate"])
        self._seed(["calculate"])
        cand = self.extractor.extract()["created"][0]
        check = self.extractor.evaluate(cand["candidate_id"])
        self.assertTrue(check["passed"])
        self.assertFalse(check["runnable"])
        self.assertEqual(check["input_binding"]["unbound"][0]["missing"],
                         ["expression"])
        self.assertEqual(check["cost"]["planned_tool_calls"], 1)
        self.assertEqual(check["cost"]["observed_avg_tool_calls"], 1.0)

    def test_publish_blocked_until_required_args_bound(self):
        self._seed(["calculate"])
        self._seed(["calculate"])
        cand = self.extractor.extract()["created"][0]
        self.extractor.approve(cand["candidate_id"])
        with self.assertRaises(ValueError):
            self.extractor.publish(cand["candidate_id"])
        self.assertEqual(
            self.extractor.get(cand["candidate_id"])["status"], "approved")

    def test_publish_binds_args_and_registers_workflow(self):
        self._seed(["calculate"])
        self._seed(["calculate"])
        cand = self.extractor.extract()["created"][0]
        self.extractor.approve(cand["candidate_id"], actor="alice")
        published = self.extractor.publish(
            cand["candidate_id"], actor="alice", reason="验证通过",
            args_by_step={"step_1": {"expression": "3+4"}})
        self.assertEqual(published["status"], "published")
        wf_id = published["workflow_id"]
        self.assertEqual(wf_id, cand["definition"]["workflow_id"])
        self.assertIn(wf_id, self.app._workflows)
        self.assertEqual(
            self.app._workflows[wf_id].node("step_1").args,
            {"expression": "3+4"})
        stored = self.app.db.get_workflow_def(wf_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["nodes"][0]["args"], {"expression": "3+4"})
        self.assertEqual(stored["status"], "published")
        self.assertEqual(stored["created_from_task"],
                         cand["definition"]["created_from_task"])


if __name__ == "__main__":
    unittest.main()
