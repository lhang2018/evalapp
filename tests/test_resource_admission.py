# -*- coding: utf-8 -*-
"""ALN-15-D：资源准入与核心任务优先。

验收对照：
- 资源压力下主任务满足明确阈值：前台（user）任务在后台让位阈值内
  正常准入/运行，只有绝对上限才暂缓；
- 主动分析让出资源：后台来源（opportunity/scheduled/derived）在高压
  下 defer（不扰动核心状态，稍后重新 execute 即恢复）；
- 停止不损坏核心状态：defer 不转状态；暂停走既有 pause 通道（决策
  边界 SUSPENDED + 检查点），恢复后继续；
- 采样失败后台defer，前台保留兼容准入。
"""

import shutil
import tempfile
import unittest

import evoagent.core.task as T
from evoagent.app import App
from evoagent.core.resource_admission import (
    AdmissionConfigError, PressureSnapshot, ResourceGovernor,
    ResourcePolicy,
)


def governor(cpu=0.0, memory=0.0, model_pool=0.0, **policy):
    return ResourceGovernor(
        lambda: {"cpu": cpu, "memory": memory, "model_pool": model_pool},
        ResourcePolicy(**policy) if policy else None)


class GovernorDecisionTest(unittest.TestCase):
    def test_pressure_is_max_of_resources_clamped(self):
        self.assertEqual(PressureSnapshot(cpu=0.95, memory=0.5,
                                          model_pool=0.3).pressure, 0.95)
        self.assertEqual(PressureSnapshot(cpu=2.0).pressure, 1.0)
        self.assertEqual(PressureSnapshot(cpu=-1).pressure, 0.0)

    def test_admit_matrix(self):
        # 低压：全部放行
        low = governor(cpu=0.3)
        for origin in ("user", "opportunity", "scheduled", "derived"):
            self.assertEqual(low.admit(origin).decision, "admit")
        # 高压（≥ high_water=0.9，< ceiling=0.98）：后台让位、前台豁免
        high = governor(cpu=0.93)
        self.assertEqual(high.admit("user").decision, "admit")
        self.assertEqual(high.admit("opportunity").decision, "defer")
        self.assertEqual(high.admit("scheduled").decision, "defer")
        # 极端压力（≥ ceiling）：全部暂缓
        extreme = governor(cpu=0.99)
        self.assertEqual(extreme.admit("user").decision, "defer")
        self.assertEqual(extreme.admit("opportunity").decision, "defer")

    def test_pause_only_background_and_records(self):
        high = governor(cpu=0.95)
        self.assertIsNone(high.should_pause("t_user", "user"),
                          "前台任务不自动暂停")
        pause = high.should_pause("t_bg", "opportunity")
        self.assertEqual(pause.decision, "pause")
        self.assertIn("t_bg", high.paused_tasks)

    def test_resume_after_pressure_drops(self):
        sampler = {"cpu": 0.95}
        gov = ResourceGovernor(lambda: sampler)
        gov.should_pause("t_bg", "opportunity")
        sampler["cpu"] = 0.5  # ≤ low_water
        decisions = gov.resume_candidates()
        self.assertIn("t_bg", decisions)
        self.assertEqual(decisions["t_bg"].decision, "resume")
        self.assertEqual(gov.paused_tasks, {})

    def test_sampler_failure_defers_background_but_preserves_foreground(self):
        def broken():
            raise RuntimeError("sensor down")
        gov = ResourceGovernor(broken)
        self.assertEqual(gov.admit("opportunity").decision, "defer")
        self.assertEqual(gov.admit("user").decision, "admit")
        self.assertIsNone(gov.should_pause("t", "opportunity"))

    def test_policy_threshold_validation(self):
        with self.assertRaises(AdmissionConfigError):
            ResourcePolicy(high_water=0.5, low_water=0.7)
        with self.assertRaises(AdmissionConfigError):
            ResourcePolicy(high_water=1.5)


class ControllerAdmissionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_15d_")
        self.sampler = {"cpu": 0.0}
        self.app = App(data_dir=self.tmp)
        self.app.controller.resource_governor = ResourceGovernor(
            lambda: dict(self.sampler))

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_reasons=True) if False else \
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, task_id, origin, goal="后台分析"):
        task = T.Task(task_id=task_id, user_goal=goal, origin=origin)
        self.app.db.save_task(task)
        return task

    def test_background_task_deferred_under_pressure_keeps_core_state(self):
        self.sampler["cpu"] = 0.95
        task = self._task("t_bg", "opportunity")
        out = self.app.controller.execute(task)
        self.assertEqual(out.task_status.value, "CREATED",
                         "defer 不转状态——核心状态零扰动")
        self.assertIn("resource_deferred", out.runtime_manifest)
        self.assertEqual(
            out.runtime_manifest["resource_deferred"]["decision"], "defer")
        events = [e for e in self.app.bus.query()
                  if e.event_type == "task_resource_deferred"]
        self.assertEqual(len(events), 1)
        # 压力回落后同一任务可直接重新执行
        self.sampler["cpu"] = 0.2
        out2 = self.app.controller.execute(
            self.app.db.get_task(task.task_id))
        self.assertNotEqual(out2.task_status.value, "CREATED")
        self.assertIsNone(out2.runtime_manifest.get("resource_deferred"))

    def test_user_task_unaffected_by_background_pressure(self):
        self.sampler["cpu"] = 0.93  # ≥ high_water，< ceiling
        task = T.Task(task_id="t_user", user_goal="前台查询",
                      intent="time_query", domain="general")
        self.app.db.save_task(task)
        out = self.app.controller.execute(task)
        self.assertNotIn("resource_deferred", out.runtime_manifest or {})
        self.assertNotEqual(out.task_status.value, "CREATED")

    def test_no_governor_unchanged(self):
        self.app.controller.resource_governor = None
        self.sampler["cpu"] = 0.99
        task = self._task("t_plain", "opportunity")
        out = self.app.controller.execute(task)
        self.assertNotIn("resource_deferred", out.runtime_manifest or {})


if __name__ == "__main__":
    unittest.main()
