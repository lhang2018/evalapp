"""ALN-08 模型计量测试：真实用量、预估、未知费用与对账。

锁定四件事：
  1. 提供方返回的 token 用量被采集并按档案价格计费；
  2. 提供方未返回用量时明确标记为 estimate，不冒充实测；
  3. 价格未知时 cost=None（绝不按 0 元记账），显式免费才记 0；
  4. 限流/超时/供应商错误有界，失败证据不丢。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evoagent.models.base import (  # noqa: E402
    Action,
    ChatMessage,
    ModelProfile,
    ModelRegistry,
)
from evoagent.models.metering import (  # noqa: E402
    COST_ESTIMATE,
    COST_FREE,
    COST_PROVIDER,
    COST_UNKNOWN,
    TOKEN_ESTIMATE,
    TOKEN_PROVIDER,
    MeteredModel,
    ModelUsage,
    UsageLedger,
    cost_from_tokens,
    estimate_cost_optional,
    pricing_status,
)
from evoagent.core.leases import CURRENT_LEASE, LeaseToken  # noqa: E402
from evoagent.models.mock import MockModel  # noqa: E402
from evoagent.models.openai_compat import OpenAICompatAdapter  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


class _Bus:
    def __init__(self):
        self.events = []

    def emit(self, **kwargs):
        self.events.append(kwargs)
        return None


class _ScriptedAdapter:
    """确定性适配器：可指定结果、错误、提供方用量与价格档案。"""

    def __init__(self, profile, *, result=None, error=None,
                 usage=None, request_id=None):
        self._profile = profile
        self._result = result
        self._error = error
        self.last_usage = usage
        self.last_request_id = request_id

    def profile(self):
        return self._profile

    def estimate_cost(self, input_chars, output_chars):
        if not self._profile.pricing_known:
            return 0.0
        return (input_chars * self._profile.price_per_1k_input
                + output_chars * self._profile.price_per_1k_output) / 1000

    def generate(self, messages):
        if self._error is not None:
            raise self._error
        return self._result if isinstance(self._result, str) else ""

    def suggest_action(self, messages, available_tools):
        if self._error is not None:
            raise self._error
        return self._result or Action(kind="finish", answer="ok")


def _priced_profile(model_id="priced-1", price_in=1.0, price_out=2.0):
    return ModelProfile(model_id=model_id, provider="acme",
                        price_per_1k_input=price_in,
                        price_per_1k_output=price_out,
                        pricing_known=True)


def _unknown_priced_profile(model_id="unpriced-1"):
    return ModelProfile(model_id=model_id, provider="acme")


class TestPricingHelpers(unittest.TestCase):
    def test_unknown_pricing_is_not_free(self):
        profile = _unknown_priced_profile()
        self.assertEqual(pricing_status(profile), "unknown")
        self.assertIsNone(cost_from_tokens(profile, 100, 50))
        self.assertIsNone(estimate_cost_optional(profile, 400, 100))

    def test_explicit_free_is_zero(self):
        profile = ModelProfile(model_id="free-1", provider="builtin",
                               pricing_known=True)
        self.assertEqual(pricing_status(profile), "free")
        self.assertEqual(cost_from_tokens(profile, 100, 50), 0.0)
        self.assertEqual(estimate_cost_optional(profile, 400, 100), 0.0)

    def test_known_pricing_computes_from_tokens(self):
        profile = _priced_profile(price_in=1.0, price_out=2.0)
        self.assertEqual(pricing_status(profile), "known")
        self.assertAlmostEqual(cost_from_tokens(profile, 120, 30), 0.18)

    def test_missing_token_count_is_unknown_not_zero(self):
        profile = _priced_profile()
        self.assertIsNone(cost_from_tokens(profile, None, 30))
        self.assertIsNone(cost_from_tokens(profile, 120, None))


class TestProviderUsageCapture(unittest.TestCase):
    def test_openai_adapter_keeps_usage_and_request_id(self):
        registry = ModelRegistry()
        registry.register(_priced_profile())
        adapter = OpenAICompatAdapter("priced-1", "test-key",
                                      registry=registry)
        response = _FakeResponse(payload={
            "id": "req-1",
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30,
                      "total_tokens": 150},
        })
        with patch("requests.post", return_value=response) as post:
            text = adapter.generate([ChatMessage(role="user", content="hi")])
        self.assertEqual(text, "hello")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(adapter.last_usage,
                         {"input_tokens": 120, "output_tokens": 30,
                          "total_tokens": 150})
        self.assertEqual(adapter.last_request_id, "req-1")

    def test_openai_adapter_without_usage_leaves_none(self):
        registry = ModelRegistry()
        registry.register(_priced_profile())
        adapter = OpenAICompatAdapter("priced-1", "test-key",
                                      registry=registry)
        response = _FakeResponse(payload={
            "choices": [{"message": {"content": "ok"}}]})
        with patch("requests.post", return_value=response):
            adapter.generate([ChatMessage(role="user", content="hi")])
        self.assertIsNone(adapter.last_usage,
                          "提供方未返回 usage 时不得伪造用量")

    def test_registry_profile_supplies_pricing(self):
        registry = ModelRegistry()
        registry.register(_priced_profile(price_in=0.5, price_out=1.5))
        adapter = OpenAICompatAdapter("priced-1", "test-key",
                                      registry=registry)
        self.assertTrue(adapter.profile().pricing_known)
        self.assertAlmostEqual(
            adapter.estimate_cost(1000, 1000), 2.0)


class TestMeteredModel(unittest.TestCase):
    def test_provider_usage_is_billed_from_real_tokens(self):
        adapter = _ScriptedAdapter(
            _priced_profile(price_in=1.0, price_out=2.0),
            result=Action(kind="finish", answer="done"),
            usage={"input_tokens": 120, "output_tokens": 30,
                   "total_tokens": 150},
            request_id="req-9")
        bus = _Bus()
        ledger = UsageLedger()
        metered = MeteredModel(adapter, bus, ledger=ledger)
        metered.suggest_action([ChatMessage(role="user", content="hi")], [])

        payload = bus.events[-1]["payload"]
        self.assertEqual(payload["token_source"], TOKEN_PROVIDER)
        self.assertEqual(payload["cost_source"], COST_PROVIDER)
        self.assertAlmostEqual(payload["cost"], 0.18)
        self.assertEqual(payload["request_id"], "req-9")
        totals = ledger.totals()
        self.assertEqual(totals["provider_usage_calls"], 1)
        self.assertAlmostEqual(totals["actual_cost"], 0.18)
        self.assertEqual(totals["unknown_calls"], 0)

    def test_unknown_pricing_records_unknown_not_free(self):
        adapter = _ScriptedAdapter(
            _unknown_priced_profile(),
            result=Action(kind="finish", answer="done"))
        bus = _Bus()
        ledger = UsageLedger()
        metered = MeteredModel(adapter, bus, ledger=ledger)
        metered.suggest_action([ChatMessage(role="user", content="hi")], [])

        payload = bus.events[-1]["payload"]
        self.assertIsNone(payload["cost"])
        self.assertEqual(payload["cost_source"], COST_UNKNOWN)
        self.assertEqual(payload["token_source"], TOKEN_ESTIMATE)
        totals = ledger.totals()
        self.assertEqual(totals["unknown_calls"], 1)
        self.assertEqual(totals["actual_cost"], 0.0)
        self.assertEqual(totals["free_calls"], 0)

    def test_mock_model_is_explicitly_free(self):
        bus = _Bus()
        ledger = UsageLedger()
        metered = MeteredModel(MockModel(), bus, ledger=ledger)
        metered.suggest_action([ChatMessage(role="user", content="hi")], [])

        payload = bus.events[-1]["payload"]
        self.assertEqual(payload["cost"], 0.0)
        self.assertEqual(payload["cost_source"], COST_FREE)
        totals = ledger.totals()
        self.assertEqual(totals["free_calls"], 1)
        self.assertEqual(totals["actual_cost"], 0.0)

    def test_estimate_path_is_labelled(self):
        adapter = _ScriptedAdapter(
            _priced_profile(price_in=1.0, price_out=2.0),
            result=Action(kind="finish", answer="abcd"))
        bus = _Bus()
        metered = MeteredModel(adapter, bus)
        metered.suggest_action(
            [ChatMessage(role="user", content="x" * 40)], [])

        payload = bus.events[-1]["payload"]
        self.assertEqual(payload["token_source"], TOKEN_ESTIMATE)
        self.assertEqual(payload["cost_source"], COST_ESTIMATE)
        self.assertGreater(payload["cost"], 0.0)
        self.assertEqual(payload["input_tokens"], 10)

    def test_failure_is_recorded_without_cost(self):
        adapter = _ScriptedAdapter(_unknown_priced_profile(),
                                   error=TimeoutError("slow"))
        bus = _Bus()
        ledger = UsageLedger()
        metered = MeteredModel(adapter, bus, ledger=ledger)
        with self.assertRaises(TimeoutError):
            metered.generate([ChatMessage(role="user", content="hi")])

        payload = bus.events[-1]["payload"]
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_class"], "TimeoutError")
        self.assertIsNone(payload["cost"])
        totals = ledger.totals()
        self.assertEqual(totals["unknown_calls"], 1)
        self.assertEqual(totals["actual_cost"], 0.0)

    def test_bus_failure_does_not_break_call(self):
        class _BrokenBus:
            def emit(self, **kwargs):
                raise RuntimeError("bus down")

        adapter = _ScriptedAdapter(_priced_profile(),
                                   result=Action(kind="finish",
                                                 answer="ok"))
        metered = MeteredModel(adapter, _BrokenBus())
        action = metered.suggest_action(
            [ChatMessage(role="user", content="hi")], [])
        self.assertEqual(action.answer, "ok")


class TestExecutionAssociation(unittest.TestCase):
    """模型调用必须能归因到当前运行，否则账单无法按任务对账。"""

    def _call(self, bus):
        adapter = _ScriptedAdapter(
            _priced_profile(), result=Action(kind="finish", answer="ok"))
        metered = MeteredModel(adapter, bus)
        metered.suggest_action([ChatMessage(role="user", content="hi")], [])
        return metered

    def test_call_inside_run_records_task_and_run(self):
        token = LeaseToken(db=None, task_id="task-1", run_id="run-1",
                           owner_id="owner", generation=1)
        reset = CURRENT_LEASE.set(token)
        try:
            bus = _Bus()
            metered = self._call(bus)
        finally:
            CURRENT_LEASE.reset(reset)
        payload = bus.events[-1]["payload"]
        self.assertEqual(payload["task_id"], "task-1")
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(metered.last_usage.task_id, "task-1")
        self.assertEqual(metered.last_usage.run_id, "run-1")

    def test_call_outside_run_does_not_fabricate_ids(self):
        reset = CURRENT_LEASE.set(None)
        try:
            bus = _Bus()
            metered = self._call(bus)
        finally:
            CURRENT_LEASE.reset(reset)
        payload = bus.events[-1]["payload"]
        self.assertIsNone(payload["task_id"])
        self.assertIsNone(payload["run_id"])
        self.assertIsNone(metered.last_usage.task_id)


class TestOpenAIAdapterFailureBounds(unittest.TestCase):
    """限流/超时/供应商错误必须有界，且不产生伪造用量。"""

    def _adapter(self):
        registry = ModelRegistry()
        registry.register(_priced_profile())
        return OpenAICompatAdapter("priced-1", "test-key",
                                   registry=registry)

    def test_rate_limit_retries_are_bounded(self):
        response = _FakeResponse(status_code=429, text="rate limited")
        adapter = self._adapter()
        with patch("requests.post", return_value=response) as post, \
                patch("evoagent.models.openai_compat.time.sleep"):
            with self.assertRaises(RuntimeError):
                adapter.generate(
                    [ChatMessage(role="user", content="hi")])
        self.assertEqual(post.call_count, 3, "限流重试必须有界")
        self.assertIsNone(adapter.last_usage)

    def test_provider_error_does_not_retry(self):
        response = _FakeResponse(status_code=400, text="bad request")
        with patch("requests.post", return_value=response) as post:
            with self.assertRaises(RuntimeError):
                self._adapter().generate(
                    [ChatMessage(role="user", content="hi")])
        self.assertEqual(post.call_count, 1)

    def test_timeout_is_bounded_and_usage_unknown(self):
        import requests

        adapter = self._adapter()
        with patch("requests.post", side_effect=requests.Timeout("slow")) \
                as post, patch("evoagent.models.openai_compat.time.sleep"):
            with self.assertRaises(RuntimeError):
                adapter.generate([ChatMessage(role="user", content="hi")])
        self.assertEqual(post.call_count, 3, "超时重试必须有界")
        self.assertIsNone(adapter.last_usage,
                          "失败调用不得留下伪造的 token 用量")


class TestUsageLedgerReconciliation(unittest.TestCase):
    def test_estimate_replaced_by_actual_not_double_counted(self):
        ledger = UsageLedger()
        ledger.begin_estimate("c1", 0.5, model_id="m1")
        ledger.record(ModelUsage(call_id="c1", model_id="m1", cost=0.3,
                                 cost_source=COST_PROVIDER,
                                 token_source=TOKEN_PROVIDER))
        totals = ledger.totals()
        self.assertEqual(totals["calls"], 1)
        self.assertAlmostEqual(totals["actual_cost"], 0.3)
        self.assertEqual(totals["pending_estimates"], 0)
        self.assertAlmostEqual(totals["estimated_cost_pending"], 0.0)

    def test_estimate_is_reported_separately_from_actual(self):
        ledger = UsageLedger()
        ledger.record(ModelUsage(model_id="m4", cost=0.5,
                                 cost_source=COST_ESTIMATE,
                                 token_source=TOKEN_ESTIMATE))
        totals = ledger.totals()
        self.assertEqual(totals["actual_cost"], 0.0,
                         "估算金额不得命名为 actual_cost")
        self.assertAlmostEqual(totals["estimated_cost"], 0.5)
        self.assertEqual(totals["unknown_calls"], 0)
        self.assertAlmostEqual(
            totals["by_model"]["m4"]["actual_cost"], 0.0)
        self.assertAlmostEqual(
            totals["by_model"]["m4"]["estimated_cost"], 0.5)

    def test_pending_estimate_is_kept_separate(self):
        ledger = UsageLedger()
        ledger.begin_estimate("c2", 0.2, model_id="m2")
        totals = ledger.totals()
        self.assertEqual(totals["pending_estimates"], 1)
        self.assertAlmostEqual(totals["estimated_cost_pending"], 0.2)
        self.assertEqual(totals["actual_cost"], 0.0)

    def test_unknown_cost_is_not_summed_as_zero(self):
        ledger = UsageLedger()
        ledger.record(ModelUsage(model_id="m3", cost=None,
                                 cost_source=COST_UNKNOWN,
                                 token_source=TOKEN_ESTIMATE))
        totals = ledger.totals()
        self.assertEqual(totals["unknown_calls"], 1)
        self.assertEqual(totals["actual_cost"], 0.0)
        self.assertEqual(totals["by_model"]["m3"]["unknown_calls"], 1)

    def test_by_model_attribution(self):
        ledger = UsageLedger()
        ledger.record(ModelUsage(model_id="a", cost=0.1,
                                 cost_source=COST_PROVIDER))
        ledger.record(ModelUsage(model_id="b", cost=0.2,
                                 cost_source=COST_PROVIDER))
        totals = ledger.totals()
        self.assertAlmostEqual(totals["by_model"]["a"]["actual_cost"], 0.1)
        self.assertAlmostEqual(totals["by_model"]["b"]["actual_cost"], 0.2)
        self.assertAlmostEqual(totals["actual_cost"], 0.3)


if __name__ == "__main__":
    unittest.main()
