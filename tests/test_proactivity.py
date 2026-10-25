# -*- coding: utf-8 -*-
"""ALN-25-B：主动强度、停止与资源非干扰。

验收对照：
- F04-04 关闭不启动新非必要任务且保留安全提示；
- 主动失败不修改核心状态（零写任务/账本）；
- F04-03 资源压力让位（消费 15-D ResourceGovernor，实测另验）；
- 四档强度与独立预算/并发（与核心任务分账）。
"""

import unittest

from evoagent.core.resource_admission import ResourceGovernor
from evoagent.goals.proactivity import (
    NO_PROGRESS_LIMIT, ProactivityError, ProactivityGovernor, level_policy,
)


def governor(cpu=0.0):
    return ResourceGovernor(lambda: {"cpu": cpu, "memory": 0.0,
                                     "model_pool": 0.0})


class LevelPolicyTest(unittest.TestCase):
    def test_four_levels_and_bounds(self):
        self.assertEqual(level_policy("off")["may_start"], False)
        self.assertEqual(level_policy("low")["max_concurrent"], 1)
        self.assertTrue(level_policy("high")["max_model_calls"]
                        > level_policy("normal")["max_model_calls"])
        with self.assertRaises(ProactivityError):
            level_policy("turbo")

    def test_monotonic_budgets(self):
        budgets = [level_policy(l)["max_model_calls"]
                   for l in ("low", "normal", "high")]
        self.assertEqual(budgets, sorted(budgets))


class MayStartTest(unittest.TestCase):
    def test_off_blocks_new_proactive_with_safety_notice(self):
        g = ProactivityGovernor(level="off")
        decision = g.may_start()
        self.assertFalse(decision.allowed)
        self.assertIn("关闭", decision.reasons[0])
        self.assertIsNotNone(decision.safety_notice)
        self.assertIn("保留", decision.safety_notice)

    def test_normal_allows_when_idle(self):
        g = ProactivityGovernor(level="normal")
        self.assertTrue(g.may_start().allowed)

    def test_concurrency_cap_does_not_touch_core(self):
        g = ProactivityGovernor(level="low")
        blocked = g.may_start(concurrent_proactive=1)
        self.assertFalse(blocked.allowed)
        self.assertTrue(any("核心任务不受此限" in r for r in blocked.reasons))

    def test_resource_pressure_yields_to_core(self):
        # F04-03：高压下主动侧让位（15-D 决策器消费）
        g = ProactivityGovernor(level="normal",
                                resource_governor=governor(cpu=0.95))
        decision = g.may_start()
        self.assertFalse(decision.allowed)
        self.assertTrue(any("让位" in r for r in decision.reasons))
        # 压力回落后恢复
        g.resource_governor = governor(cpu=0.2)
        self.assertTrue(g.may_start().allowed)

    def test_independent_budget_pool_exhaustion(self):
        class ExhaustedLedger:
            def check(self, scope, **limits):
                raise RuntimeError(f"{scope} 预算已尽")
        g = ProactivityGovernor(level="normal", ledger=ExhaustedLedger())
        decision = g.may_start()
        self.assertFalse(decision.allowed)
        self.assertTrue(any("主动预算池" in r for r in decision.reasons))


class NoProgressTest(unittest.TestCase):
    def test_stop_after_limit_then_reset(self):
        g = ProactivityGovernor(level="low")   # limit=2
        out1 = g.record_attempt(had_benefit=False)
        self.assertFalse(out1["stopped"])
        out2 = g.record_attempt(had_benefit=False)
        self.assertTrue(out2["stopped"])
        self.assertFalse(g.may_start().allowed)  # 停止后不再启动
        g.reset_no_progress()
        self.assertTrue(g.may_start().allowed)

    def test_benefit_resets_streak(self):
        g = ProactivityGovernor(level="normal")
        g.record_attempt(had_benefit=False)
        g.record_attempt(had_benefit=True)
        self.assertEqual(g.status()["no_progress_streak"], 0)

    def test_level_limits(self):
        self.assertEqual(NO_PROGRESS_LIMIT["low"], 2)
        self.assertGreater(NO_PROGRESS_LIMIT["high"], NO_PROGRESS_LIMIT["low"])


class FailureIsolationTest(unittest.TestCase):
    def test_failure_isolated_and_core_untouched(self):
        g = ProactivityGovernor(level="normal")
        out = g.handle_failure("模型超时")
        self.assertTrue(out["isolated"])
        self.assertFalse(out["core_state_touched"])
        self.assertIn("不受影响", out["notice"])

    def test_set_level_off_returns_notice_and_back(self):
        g = ProactivityGovernor(level="normal")
        out = g.set_level("off")
        self.assertIn("关闭", out["notice"])
        self.assertFalse(g.may_start().allowed)
        back = g.set_level("normal")
        self.assertTrue(g.may_start().allowed)


if __name__ == "__main__":
    unittest.main()
