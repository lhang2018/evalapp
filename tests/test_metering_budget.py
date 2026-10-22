"""ALN-08-M：模型计量结果接入任务预算记账。

为什么要有这层：计量模块算出「这次调用到底花了多少钱」，
但如果运行内核仍按字符数预估结算，实测成本就永远进不了账单。
本测试锁定三条边界：

- 有实测成本 → 按实测金额记账，而不是预估；
- 成本未知（价格档案缺失）→ 单独计数，**绝不按 0 元记账**；
- 旧适配器没有计量结果 → 保留原有字符数预估行为。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest

from evoagent.app import App
from evoagent.core.budget import (BudgetController, BudgetLimits,
                                  BudgetUsage)
from evoagent.core.permissions import PermissionController
from evoagent.core.task import Task
from evoagent.models.base import Action, ChatMessage, ModelProfile
from evoagent.models.metering import MeteredModel, UsageLedger
from evoagent.react.engine import ReactEngine
from evoagent.tools.registry import ToolRegistry


class _InnerModel:
    """直接暴露提供方用量的内层适配器（模拟 OpenAICompatAdapter）。"""

    def __init__(self, pricing_known: bool, tokens=True, estimate=99.0):
        self.pricing_known = pricing_known
        self.tokens = tokens
        self.estimate = estimate
        self.last_usage = None

    def profile(self):
        return ModelProfile(
            model_id="metered-test", provider="test",
            price_per_1k_input=1.0, price_per_1k_output=2.0,
            pricing_known=self.pricing_known)

    def estimate_cost(self, _input_chars, _output_chars):
        return self.estimate

    def suggest_action(self, _messages, _tools):
        if self.tokens:
            # 100 输入 + 50 输出 → 0.1 + 0.1 = 0.2
            self.last_usage = {"input_tokens": 100, "output_tokens": 50}
        return Action(kind="finish", answer="done")

    def generate(self, _messages):
        return "done"


class _LegacyModel:
    """没有计量结果的旧适配器：只有 estimate_cost。"""

    def profile(self):
        return ModelProfile(model_id="legacy-test")

    def estimate_cost(self, _input_chars, _output_chars):
        return 0.25

    def suggest_action(self, _messages, _tools):
        return Action(kind="finish", answer="done")

    def generate(self, _messages):
        return "done"


def _run(model, budget=None):
    budget = budget or BudgetController(BudgetLimits(
        max_model_calls=5, max_tool_calls=5, max_cost=10, max_seconds=60))
    engine = ReactEngine(model, ToolRegistry(PermissionController()))
    outcome = engine.run([ChatMessage(role="user", content="hi")], budget)
    assert outcome.finished
    return budget


class TestMeteredBudgetSettlement(unittest.TestCase):
    def test_provider_cost_settles_actual_not_estimate(self):
        inner = _InnerModel(pricing_known=True)
        model = MeteredModel(inner)
        budget = _run(model)
        # 预登记 1 次，结算不重复计数
        self.assertEqual(budget.usage.model_calls, 1)
        self.assertAlmostEqual(budget.usage.cost, 0.2)
        self.assertEqual(budget.usage.unknown_model_costs, 0)
        self.assertAlmostEqual(
            budget.usage.model_usage["metered-test"]["cost"], 0.2)

    def test_unknown_price_is_counted_not_charged_as_zero(self):
        inner = _InnerModel(pricing_known=False)
        model = MeteredModel(inner)
        budget = _run(model)
        self.assertEqual(budget.usage.model_calls, 1)
        self.assertEqual(budget.usage.cost, 0.0)
        self.assertEqual(budget.usage.unknown_model_costs, 1)
        self.assertEqual(
            budget.usage.model_usage["metered-test"]["unknown_costs"], 1)

    def test_legacy_adapter_keeps_estimate_fallback(self):
        budget = _run(_LegacyModel())
        self.assertEqual(budget.usage.model_calls, 1)
        self.assertAlmostEqual(budget.usage.cost, 0.25)
        self.assertEqual(budget.usage.unknown_model_costs, 0)

    def test_unknown_cost_survives_usage_restore(self):
        restored = BudgetUsage.from_dict({
            "model_calls": 2, "cost": 0.3, "unknown_model_costs": 1})
        self.assertEqual(restored.unknown_model_costs, 1)
        b = BudgetController(BudgetLimits(), initial_usage=restored)
        self.assertEqual(b.usage.unknown_model_costs, 1)

    def test_scoped_budget_propagates_unknown_cost(self):
        parent = BudgetController(BudgetLimits())
        scope = parent.for_scope("workflow-x")
        scope.mark_unknown_model_cost("metered-test")
        self.assertEqual(scope.usage.unknown_model_costs, 1)
        self.assertEqual(parent.usage.unknown_model_costs, 1)
        self.assertEqual(
            parent.snapshot()["usage"]["unknown_model_costs"], 1)


class _ThreeStageModel:
    """分类、规划、ReAct 共用；每次调用都回报提供方用量。"""

    def __init__(self, pricing_known: bool):
        self._profile = ModelProfile(
            model_id="three-stage", provider="feature-fixture",
            pricing_known=pricing_known,
            price_per_1k_input=1.0, price_per_1k_output=2.0)
        self.last_usage = None
        self.calls = 0

    def profile(self):
        return self._profile

    def estimate_cost(self, _input_chars, _output_chars):
        return 0.000001

    def _mark_usage(self):
        self.calls += 1
        # 100 输入 + 50 输出 → 0.1 + 0.1 = 0.2
        self.last_usage = {"input_tokens": 100, "output_tokens": 50}

    def generate(self, messages):
        self._mark_usage()
        if "意图分类器" in messages[0].content:
            return json.dumps({"intent": "general", "domain": "general",
                               "confidence": 1.0})
        return json.dumps({"steps": [{"step_id": "s1",
                                      "description": "answer the goal",
                                      "deps": []}]})

    def suggest_action(self, _messages, _tools):
        self._mark_usage()
        return Action(kind="finish", answer="three-stage answer")


class TestAllModelPhasesSettle(unittest.TestCase):
    """CA-08-01：分类与规划也必须结算真实计量，不能只让 ReAct 记账。"""

    def _run(self, pricing_known: bool):
        tmp = tempfile.mkdtemp(prefix="evoagent_phases_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        app = App(data_dir=tmp)
        self.addCleanup(app.db.close)
        ledger = UsageLedger()
        model = MeteredModel(_ThreeStageModel(pricing_known), app.bus,
                             ledger=ledger)
        app.model_registry.register(model.profile())
        app.classifier.model = model
        app.router.bind_route("general", "three-stage")
        app.router.setup(
            lambda identity: model if identity == "three-stage" else None)
        task = app.controller.submit_task(
            "settlement goal", wait=True,
            acceptance=[{"type": "result_nonempty"}],
            budget_overrides={"max_cost": 1.0,
                              "max_cost_per_model_call": 1.0,
                              "max_model_calls": 10})
        usage = app.db.get_execution_budget(task.task_id)["usage"]
        return task, usage, ledger

    def test_known_price_settles_classification_planning_and_react(self):
        task, usage, ledger = self._run(pricing_known=True)
        self.assertEqual(task.task_status.value, "SUCCEEDED")
        self.assertEqual(usage["model_calls"], 3)
        self.assertAlmostEqual(usage["cost"], 0.6)
        self.assertEqual(usage["unknown_model_costs"], 0)
        self.assertAlmostEqual(
            usage["model_usage"]["three-stage"]["cost"], 0.6)
        totals = ledger.totals()
        self.assertEqual(totals["calls"], 3)
        self.assertAlmostEqual(totals["actual_cost"], 0.6)
        self.assertEqual(totals["unknown_calls"], 0)

    def test_unknown_price_counts_each_phase_not_once(self):
        task, usage, ledger = self._run(pricing_known=False)
        self.assertEqual(task.task_status.value, "SUCCEEDED")
        self.assertEqual(usage["model_calls"], 3)
        self.assertEqual(usage["cost"], 0.0)
        self.assertEqual(usage["unknown_model_costs"], 3)
        self.assertEqual(ledger.totals()["unknown_calls"], 3)

    def test_cumulative_actual_cost_stops_task_over_budget(self):
        tmp = tempfile.mkdtemp(prefix="evoagent_phases_budget_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        app = App(data_dir=tmp)
        self.addCleanup(app.db.close)
        model = MeteredModel(_ThreeStageModel(True), app.bus)
        app.model_registry.register(model.profile())
        app.classifier.model = model
        app.router.bind_route("general", "three-stage")
        app.router.setup(
            lambda identity: model if identity == "three-stage" else None)
        task = app.controller.submit_task(
            "budget goal", wait=True,
            acceptance=[{"type": "result_nonempty"}],
            budget_overrides={"max_cost": 0.3,
                              "max_cost_per_model_call": 1.0,
                              "max_model_calls": 10})
        usage = app.db.get_execution_budget(task.task_id)["usage"]
        # 三阶段累计 0.6 超过 0.3：记账必须完整，任务不得记成功
        self.assertEqual(usage["model_calls"], 3)
        self.assertAlmostEqual(usage["cost"], 0.6)
        self.assertEqual(task.task_status.value, "FAILED")


class TestRuntimeMeteringAssociation(unittest.TestCase):
    """真实任务执行链：model_call 事件必须能按 task/run 查询。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_metering_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_model_call_events_carry_task_and_run(self):
        task = self.app.controller.execute(
            Task(user_goal="你好"), intent_hint="general")
        events = self.app.bus.query(task_id=task.task_id, limit=500)
        calls = [e for e in events if e.event_type == "model_call"]
        self.assertTrue(calls, "任务执行未产生 model_call 事件")
        for evt in calls:
            self.assertEqual(evt.run_id, task.current_run_id)
            self.assertEqual(evt.payload["task_id"], task.task_id)
            self.assertEqual(evt.payload["run_id"], task.current_run_id)


if __name__ == "__main__":
    unittest.main()
