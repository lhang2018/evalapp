"""统一状态机测试（Architecture 3.3）。"""

import unittest

from evoagent.core.state_machine import (ALLOWED_TRANSITIONS, IllegalTransition,
                                         can_transition, transition)
from evoagent.core.task import TaskStatus


class TestStateMachine(unittest.TestCase):
    def test_happy_path(self):
        path = [TaskStatus.CREATED, TaskStatus.CLASSIFYING, TaskStatus.PLANNING,
                TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.VERIFYING,
                TaskStatus.SUCCEEDED]
        cur = path[0]
        for nxt in path[1:]:
            cur = transition(cur, nxt)
        self.assertEqual(cur, TaskStatus.SUCCEEDED)

    def test_terminal_has_no_outbound(self):
        for st in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            self.assertEqual(ALLOWED_TRANSITIONS[st], frozenset())
            self.assertFalse(can_transition(st, TaskStatus.RUNNING))

    def test_illegal_transition_raises(self):
        with self.assertRaises(IllegalTransition):
            transition(TaskStatus.CREATED, TaskStatus.SUCCEEDED)
        with self.assertRaises(IllegalTransition):
            transition(TaskStatus.SUCCEEDED, TaskStatus.RUNNING)

    def test_recovery_paths_exist(self):
        self.assertTrue(can_transition(TaskStatus.RUNNING, TaskStatus.RECOVERING))
        self.assertTrue(can_transition(TaskStatus.RECOVERING, TaskStatus.SUSPENDED))
        self.assertTrue(can_transition(TaskStatus.SUSPENDED, TaskStatus.RUNNING))
        self.assertTrue(can_transition(TaskStatus.SUSPENDED, TaskStatus.RECOVERING))
        self.assertTrue(can_transition(TaskStatus.SUSPENDED, TaskStatus.WAITING_USER))
        self.assertTrue(can_transition(TaskStatus.RUNNING, TaskStatus.WAITING_DEPENDENCY))
        self.assertTrue(can_transition(TaskStatus.WAITING_DEPENDENCY, TaskStatus.RUNNING))


if __name__ == "__main__":
    unittest.main()
