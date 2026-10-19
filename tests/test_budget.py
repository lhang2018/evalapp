"""硬预算控制器测试（PRD 56-58）。"""

import unittest

from evoagent.core.budget import BudgetController, BudgetExceeded, BudgetLimits


class TestBudget(unittest.TestCase):
    def test_model_call_limit(self):
        b = BudgetController(BudgetLimits(max_model_calls=2, max_tool_calls=10,
                                          max_cost=10, max_seconds=60))
        b.record_model_call(0.1)
        b.record_model_call(0.1)
        with self.assertRaises(BudgetExceeded):
            b.check_model_call()

    def test_tool_call_limit(self):
        b = BudgetController(BudgetLimits(max_model_calls=10, max_tool_calls=1,
                                          max_cost=10, max_seconds=60))
        b.record_tool_call()
        with self.assertRaises(BudgetExceeded):
            b.check_tool_call()

    def test_cost_limit(self):
        b = BudgetController(BudgetLimits(max_model_calls=100, max_tool_calls=100,
                                          max_cost=0.1, max_seconds=600))
        b.record_model_call(0.2)
        with self.assertRaises(BudgetExceeded):
            b.check_alive()

    def test_overrides(self):
        b = BudgetController(BudgetLimits(), overrides={"max_model_calls": 1})
        b.record_model_call()
        with self.assertRaises(BudgetExceeded):
            b.check_model_call()


if __name__ == "__main__":
    unittest.main()
