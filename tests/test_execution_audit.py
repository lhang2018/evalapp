# -*- coding: utf-8 -*-
"""S-05 执行环节审计（GAP 死字段修复②）：执行侧落地审计断言。

原状：audit 只有 API/CLI 边界在写，执行环节（状态流转/记忆沉淀/
失败建案/暂停）零留痕。现口径：RuntimeController 持可选 audit，
五类执行事件追加记录、不参与业务判断；审计自身故障不阻断执行。
"""

import shutil
import tempfile
import unittest

import evoagent.core.task as T
from evoagent.acceptance.verify import Verdict
from evoagent.app import App


class TestExecutionAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_s05_")
        self.app = App(data_dir=self.tmp)
        self.task = T.Task(task_id="t_s05", user_goal="g")
        self.app.db.save_task(self.task)
        self.c = self.app.controller

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _actions(self):
        return [r["action"] for r in self.app.audit.list()]

    def test_app_wires_audit_into_controller(self):
        self.assertIs(self.c.audit, self.app.audit)

    def test_state_transition_audited(self):
        self.task.task_status = T.TaskStatus.RUNNING
        self.c._move(self.task, T.TaskStatus.SUSPENDED)
        acts = self._actions()
        self.assertIn("task_state_transition", acts)
        row = self.app.audit.list()[0]
        self.assertEqual(row["target"], "t_s05")
        self.assertEqual(row["detail"]["to"], "SUSPENDED")
        self.assertEqual(row["actor"], "controller")

    def test_memory_remembered_audited(self):
        self.task.result = "ok done"
        self.c._remember(self.task, verified=True)
        acts = self._actions()
        self.assertIn("memory_remembered", acts)
        row = self.app.audit.list()[0]
        self.assertEqual(row["target"], "t_s05")
        self.assertEqual(row["detail"]["verified"], True)
        self.assertEqual(row["detail"]["status"], "verified")

    def test_diagnostic_case_audited(self):
        self.c._record_diagnostic(self.task, Verdict(passed=False),
                                  [], stop_reason="s05_test")
        acts = self._actions()
        self.assertIn("diagnostic_case_recorded", acts)
        row = self.app.audit.list()[0]
        self.assertEqual(row["outcome"], "warn")
        self.assertEqual(row["detail"]["stop_reason"], "s05_test")

    def test_audit_failure_never_blocks_business(self):
        class Boom:
            def record(self, *args, **kwargs):
                raise RuntimeError("audit down")

            def list(self, limit=100):
                return []

        self.c.audit = Boom()
        self.task.task_status = T.TaskStatus.RUNNING
        moved = self.c._move(self.task, T.TaskStatus.SUSPENDED)  # 不得抛
        self.assertEqual(moved.task_status, T.TaskStatus.SUSPENDED)
        # _remember 同样不被审计故障拖垮
        self.task.result = "x"
        self.c._remember(self.task, verified=True)  # 不得抛


if __name__ == "__main__":
    unittest.main()
