"""模型辅助规划与意图分类（PRD 7-8 / 11.2）。"""

import json
import unittest

from evoagent.models.base import Action, ModelProfile
from evoagent.planning.intent import IntentClassifier
from evoagent.planning.planner import PlanningEngine


class FakeAdapter:
    """返回预设文本；不发网络请求。"""

    def __init__(self, output="", exc=None):
        self.output = output
        self.exc = exc
        self.calls = 0

    def profile(self) -> ModelProfile:
        return ModelProfile(model_id="fake")

    def suggest_action(self, messages, available_tools) -> Action:
        return Action(kind="finish", answer="fake")

    def generate(self, messages):
        self.calls += 1
        self.last_messages = messages
        if self.exc:
            raise self.exc
        return self.output

    def estimate_cost(self, input_chars, output_chars) -> float:
        return 0.0


def _plan(steps):
    return json.dumps({"steps": steps}, ensure_ascii=False)


class TestModelPlanning(unittest.TestCase):
    def test_valid_model_plan_is_used(self):
        output = _plan([
            {"step_id": "s1", "description": "查询时间", "deps": [],
             "tool_hint": "current_time"},
            {"step_id": "s2", "description": "总结结果", "deps": ["s1"]},
        ])
        plan = PlanningEngine(FakeAdapter(output)).make_plan("现在几点")
        self.assertEqual([s.step_id for s in plan.steps], ["s1", "s2"])
        self.assertEqual(plan.steps[0].tool_hint, "current_time")
        self.assertEqual(plan.change_reasons, [])
        self.assertEqual(plan.validate(), [])

    def test_fenced_json_is_accepted(self):
        output = "```json\n" + _plan([
            {"step_id": "a", "description": "执行", "deps": []}]) + "\n```"
        plan = PlanningEngine(FakeAdapter(output)).make_plan("x")
        self.assertEqual([s.step_id for s in plan.steps], ["a"])

    def test_bad_json_falls_back_with_reason(self):
        plan = PlanningEngine(FakeAdapter("这不是 JSON")).make_plan("x")
        self.assertEqual(len(plan.steps), 3)
        self.assertTrue(any(r.startswith("model_plan_failed:")
                            for r in plan.change_reasons))
        self.assertEqual(plan.validate(), [])

    def test_cycle_and_missing_dep_fall_back(self):
        for steps in (
            [{"step_id": "a", "description": "1", "deps": ["b"]},
             {"step_id": "b", "description": "2", "deps": ["a"]}],
            [{"step_id": "a", "description": "1", "deps": ["ghost"]}],
            [{"step_id": "a", "description": "1", "deps": []},
             {"step_id": "a", "description": "2", "deps": []}],
        ):
            plan = PlanningEngine(FakeAdapter(_plan(steps))).make_plan("x")
            self.assertEqual(len(plan.steps), 3)
            self.assertTrue(plan.change_reasons)

    def test_too_many_steps_falls_back(self):
        steps = [{"step_id": f"s{i}", "description": str(i), "deps": []}
                 for i in range(13)]
        plan = PlanningEngine(FakeAdapter(_plan(steps))).make_plan("x")
        self.assertEqual(len(plan.steps), 3)

    def test_no_model_keeps_template_without_reasons(self):
        plan = PlanningEngine(None).make_plan("x")
        self.assertEqual(len(plan.steps), 3)
        self.assertEqual(plan.change_reasons, [])


class TestIntentClassifier(unittest.TestCase):
    def test_hint_short_circuits_model(self):
        fake = FakeAdapter('{"intent":"computation","confidence":0.9}')
        intent, domain, conf, method = IntentClassifier(fake).classify(
            "随便", hint="weather", domain_hint="travel")
        self.assertEqual((intent, domain, conf, method),
                         ("weather", "travel", 1.0, "hint"))
        self.assertEqual(fake.calls, 0)

    def test_model_classification(self):
        fake = FakeAdapter('{"intent":"computation","domain":"programming",'
                           '"confidence":0.82}')
        clf = IntentClassifier(fake, known_intents=["general", "computation"])
        intent, domain, conf, method = clf.classify("12*34")
        self.assertEqual((intent, domain, conf, method),
                         ("computation", "programming", 0.82, "model"))

    def test_prompt_prefers_specific_intents(self):
        fake = FakeAdapter('{"intent":"time_query","domain":"general",'
                           '"confidence":0.9}')
        clf = IntentClassifier(fake, known_intents=["general", "time_query"])
        intent, _, _, method = clf.classify("现在几点了")
        self.assertEqual((intent, method), ("time_query", "model"))
        prompt = "\n".join(m.content for m in fake.last_messages)
        self.assertIn("查询当前时间", prompt)
        self.assertIn("优先选择具体领域", prompt)

    def test_time_query_is_registered_intent(self):
        from evoagent.experts.builtin import builtin_experts
        known = set()
        for expert in builtin_experts().all():
            known.add(expert.expert_id)
            known.update(expert.applicable_intents)
        self.assertIn("time_query", known)

    def test_unknown_intent_falls_back(self):
        fake = FakeAdapter('{"intent":"teleport","confidence":0.99}')
        clf = IntentClassifier(fake, known_intents=["general", "time_query"])
        intent, _, _, method = clf.classify("现在几点了")
        self.assertEqual(intent, "time_query")
        self.assertEqual(method, "heuristic_fallback")

    def test_model_error_falls_back(self):
        """模型出错时必须走启发式兜底。

        期望意图原写作 `programming` —— 那是**从未注册的幽灵意图**
        （描述表与专家表都用 `coding`），导致兜底结果无法路由到任何专家。
        F-53 路由评测查出此缺陷并修正为 `coding`（见 planning/intent.py）。
        本测试锁的核心行为是「回退到启发式且置信度 < 1.0」。
        """
        clf = IntentClassifier(FakeAdapter(exc=RuntimeError("boom")),
                               known_intents=["general"])
        intent, _, conf, method = clf.classify("帮我写代码")
        self.assertEqual(intent, "coding")
        self.assertEqual(method, "heuristic_fallback")
        self.assertLess(conf, 1.0)

    def test_fallback_intent_is_registered(self):
        """兜底得到的意图必须是**已注册**意图 —— 否则路由不到任何专家。

        这是 F-53 修掉的幽灵意图问题的回归锁：任何关键词表里的意图
        都必须存在于 `IntentClassifier._DESCRIPTIONS`。
        """
        from evoagent.planning.intent import _DESCRIPTIONS
        clf = IntentClassifier(FakeAdapter(exc=RuntimeError("boom")),
                               known_intents=["general"])
        for goal in ("帮我写代码", "现在几点", "记个笔记", "规划出行程",
                     "查天气", "做个漫剧", "打开手机", "计算 1+1"):
            intent, _, _, method = clf.classify(goal)
            self.assertIn(intent, _DESCRIPTIONS,
                          f"兜底意图 {intent} 未注册（goal={goal}）")
            self.assertIn(method, ("heuristic", "heuristic_fallback"))

    def test_low_confidence_marked(self):
        fake = FakeAdapter('{"intent":"general","confidence":0.2}')
        clf = IntentClassifier(fake, known_intents=["general"],
                               threshold=0.5)
        _, _, conf, method = clf.classify("模糊目标")
        self.assertEqual(method, "model")
        self.assertLess(conf, clf.threshold)


if __name__ == "__main__":
    unittest.main()
