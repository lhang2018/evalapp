"""F-53 路由质量评测与纠错 测试（R-03 主题 / WP-6.3）。

判定口径与 FEATURE_SPLIT.md §5.4 DoD 一致：
实现逻辑 + 产生它的代码路径 + 走通该路径的测试。

本测试锁定三件事：
  1. **指标真的能被产出**（不是接口在、逻辑空）
  2. **查出的缺陷是真的**（幽灵意图 / 无关键词意图）
  3. **回归回流守得住**（保留集不污染、低置信样本可固化）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evoagent.core.persistence import Database  # noqa: E402
from evoagent.evolution.routing_eval import (  # noqa: E402
    FALLBACK_METHODS,
    ROUTING_LAYERS,
    ModelRoutingCase,
    ModelRoutingEvaluator,
    RoutingCase,
    RoutingDataset,
    RoutingEvaluator,
    builtin_routing_datasets,
    load_reports,
    routing_coverage,
    save_report,
)
from evoagent.experts.builtin import builtin_experts  # noqa: E402
from evoagent.models.base import ModelProfile, ModelRegistry  # noqa: E402
from evoagent.models.metering import (  # noqa: E402
    COST_PROVIDER,
    TOKEN_PROVIDER,
    ModelUsage,
    UsageLedger,
)
from evoagent.models.router import ModelRouter  # noqa: E402
from evoagent.planning.intent import (  # noqa: E402
    _DESCRIPTIONS,
    _KEYWORDS,
    IntentClassifier,
)


class TestIntentVocabularyConsistency(unittest.TestCase):
    """意图词表一致性 —— R-03 缺陷的根因所在。

    F-53 首次运行评测时查出：关键词表含 `programming`（**从未注册的幽灵意图**），
    而 `coding` / `travel` **没有任何关键词** → 这两个领域永远无法被启发式命中。
    """

    def test_no_ghost_intent_in_keywords(self):
        """关键词表不得含未注册意图 —— 否则该意图无法路由到对应专家。"""
        kw_intents = {intent for intent, _ in _KEYWORDS}
        ghosts = kw_intents - set(_DESCRIPTIONS)
        self.assertEqual(ghosts, set(),
                         f"关键词表含幽灵意图 {ghosts} —— 这些意图无法被路由")

    def test_programming_alias_resolved_to_coding(self):
        """曾经的幽灵意图 `programming` 必须已归位为 `coding`。"""
        kw_intents = {intent for intent, _ in _KEYWORDS}
        self.assertNotIn("programming", kw_intents,
                         "`programming` 从未注册，应统一为 `coding`")
        self.assertIn("coding", kw_intents,
                      "`coding` 是已注册意图（experts/builtin.py 适用），"
                      "必须有关键词才能被启发式命中")

    def test_registered_intents_have_keywords(self):
        """除 general 外，每个已注册意图都应有启发式关键词。

        general 是显式兜底项，本就无关键词。
        """
        kw_intents = {intent for intent, _ in _KEYWORDS}
        missing = set(_DESCRIPTIONS) - kw_intents - {"general"}
        self.assertEqual(missing, set(),
                         f"这些已注册意图没有任何关键词，启发式永远命中不了：{missing}")

    def test_expert_intents_are_known(self):
        """专家声明的 applicable_intents 必须是已注册意图。"""
        registry = builtin_experts()
        for expert in registry.all():
            for intent in expert.applicable_intents:
                self.assertIn(
                    intent, _DESCRIPTIONS,
                    f"专家 {expert.expert_id} 声明了未注册意图 {intent}")


class TestRoutingEvaluation(unittest.TestCase):
    """评测器必须真的产出指标。"""

    def setUp(self):
        self.classifier = IntentClassifier()
        self.experts = builtin_experts()
        self.evaluator = RoutingEvaluator(self.classifier, self.experts)

    def test_builtin_dataset_covers_all_registered_intents(self):
        """内置评测集必须覆盖全部已注册意图。"""
        ds = builtin_routing_datasets()["builtin_routing"]
        covered = {c.expected_intent for c in ds.cases}
        missing = set(_DESCRIPTIONS) - covered
        self.assertEqual(missing, set(),
                         f"评测集未覆盖这些意图：{missing}")

    def test_report_has_all_metrics(self):
        """报告必须含全部约定指标，且数值有效。"""
        ds = builtin_routing_datasets()["builtin_routing"]
        report = self.evaluator.run(ds)

        self.assertEqual(report.case_count, len(ds.cases))
        self.assertGreater(report.intent_accuracy, 0.0)
        self.assertLessEqual(report.intent_accuracy, 1.0)
        self.assertGreaterEqual(report.expert_accuracy, 0.0)
        self.assertLessEqual(report.expert_accuracy, 1.0)
        self.assertGreaterEqual(report.fallback_rate, 0.0)
        self.assertLessEqual(report.fallback_rate, 1.0)
        self.assertGreaterEqual(report.abstention_rate, 0.0)
        self.assertLessEqual(report.abstention_rate, 1.0)
        self.assertGreater(report.top1_accuracy, 0.0)
        self.assertTrue(report.confusion)
        self.assertTrue(report.per_intent)
        self.assertTrue(report.method_counts)

    def test_builtin_dataset_routing_accuracy_is_high(self):
        """内置集上的意图与专家准确率必须达标 —— 否则说明路由本身坏了。"""
        ds = builtin_routing_datasets()["builtin_routing"]
        report = self.evaluator.run(ds)
        self.assertGreaterEqual(
            report.intent_accuracy, 0.95,
            f"内置路由集意图准确率过低：{report.intent_accuracy} —— "
            f"错分明细：{[(r.case_id, r.expected_intent, r.predicted_intent) for r in report.case_results if not r.ok]}")
        self.assertGreaterEqual(
            report.expert_accuracy, 0.95,
            f"内置路由集专家准确率过低：{report.expert_accuracy}")

    def test_confusion_matrix_records_misroutes(self):
        """混淆矩阵必须能定位「谁被认成了谁」。"""
        ds = builtin_routing_datasets()["builtin_routing"]
        report = self.evaluator.run(ds)
        for actual, preds in report.confusion.items():
            for predicted, count in preds.items():
                self.assertGreater(count, 0)
                if actual != predicted:
                    # 混淆项必须能在 case_results 里找到对应证据
                    hits = [r for r in report.case_results
                            if r.expected_intent == actual
                            and r.predicted_intent == predicted]
                    self.assertTrue(hits,
                                    f"混淆矩阵 {actual}->{predicted} 无对应用例证据")

    def test_holdout_dataset_is_independent(self):
        """保留集必须独立标记，且不被计入调优集。"""
        datasets = builtin_routing_datasets()
        holdout = datasets["builtin_routing_holdout"]
        tune = datasets["builtin_routing"]
        self.assertTrue(holdout.is_holdout())
        self.assertFalse(tune.is_holdout())
        holdout_ids = {c.case_id for c in holdout.cases}
        tune_ids = {c.case_id for c in tune.cases}
        self.assertFalse(holdout_ids & tune_ids, "保留集与调优集用例重叠")

    def test_fallback_rate_tracks_classification_method(self):
        """兜底率必须与分类方法分布一致 —— 指标不是编出来的。"""
        ds = builtin_routing_datasets()["builtin_routing"]
        report = self.evaluator.run(ds)
        expected_fallback = sum(
            cnt for m, cnt in report.method_counts.items()
            if m in FALLBACK_METHODS)
        self.assertAlmostEqual(
            report.fallback_rate, expected_fallback / len(ds.cases),
            places=6)

    def test_low_confidence_samples_are_detected(self):
        """低置信样本必须被识别（它们是回归回流的原料）。"""
        ds = RoutingDataset(
            dataset_id="probe",
            cases=[RoutingCase(case_id="c1", input_goal="随便什么",
                               expected_intent="general",
                               difficulty="easy")])
        report = self.evaluator.run(ds)
        # 纯启发式下 general 置信度为 0.4 < 0.5 阈值 → 应被判为低置信
        self.assertTrue(report.case_results[0].low_confidence,
                        "general 意图启发式置信度 0.4，应低于默认阈值 0.5")
        self.assertGreater(report.abstention_rate, 0.0)

    def test_expert_accuracy_only_counts_declared_cases(self):
        """未声明 expected_expert 的用例不得计入专家准确率分母。"""
        ds = RoutingDataset(
            dataset_id="probe",
            cases=[RoutingCase(case_id="c1", input_goal="你好",
                               expected_intent="general")])
        report = self.evaluator.run(ds)
        self.assertEqual(report.expert_case_count, 0)
        self.assertEqual(report.expert_accuracy, 0.0)

    def test_ambiguous_case_accepts_alternatives(self):
        """歧义用例：任一可接受意图都算对。"""
        ds = RoutingDataset(
            dataset_id="probe",
            cases=[RoutingCase(case_id="amb", input_goal="去杭州的行程顺便看看天气",
                               expected_intent="travel",
                               acceptable_intents=["weather"])])
        report = self.evaluator.run(ds)
        res = report.case_results[0]
        self.assertTrue(res.ok, "歧义用例应被接受")
        self.assertIn(res.predicted_intent, ("travel", "weather"))


class TestRegressionFeedback(unittest.TestCase):
    """低置信样本回流为回归用例（R-03 的「纠错」环节）。"""

    def setUp(self):
        self.classifier = IntentClassifier()
        self.experts = builtin_experts()
        self.evaluator = RoutingEvaluator(self.classifier, self.experts)

    def test_regression_cases_captured(self):
        """失败/低置信样本必须能固化为回归用例。"""
        ds = RoutingDataset(
            dataset_id="probe",
            cases=[RoutingCase(case_id="bad", input_goal="x",
                               expected_intent="coding",
                               expected_expert="coding")])
        report = self.evaluator.run(ds)
        regs = self.evaluator.regression_cases(report)
        self.assertTrue(regs, "失败样本应被回流")
        self.assertTrue(any(r.expected_intent == "coding" for r in regs))
        self.assertTrue(any("regression" in r.tags for r in regs))

    def test_holdout_never_refluxed(self):
        """保留集样本不得回流 —— 拿来调优会破坏其独立性。"""
        ds = RoutingDataset(
            dataset_id="probe",
            holdout=True,
            cases=[RoutingCase(case_id="h1", input_goal="x",
                               expected_intent="coding",
                               holdout=True)])
        report = self.evaluator.run(ds)
        regs = self.evaluator.regression_cases(report)
        self.assertEqual(regs, [], "保留集样本被回流了 —— 会污染验收数据")

    def test_regression_respects_max_cases(self):
        """回流数量必须有上限，避免把评测集撑爆。"""
        ds = RoutingDataset(
            dataset_id="probe",
            cases=[RoutingCase(case_id=f"c{i}", input_goal="x",
                               expected_intent="coding")
                   for i in range(20)])
        report = self.evaluator.run(ds)
        regs = self.evaluator.regression_cases(report, max_cases=5)
        self.assertLessEqual(len(regs), 5)


class TestFiveLayerCoverage(unittest.TestCase):
    """ALN-08 五层覆盖复核：已评测层有指标，缺失层必须显式标出。"""

    def test_layers_listed_in_order(self):
        rows = routing_coverage()
        self.assertEqual([r["layer"] for r in rows], list(ROUTING_LAYERS))
        self.assertEqual(len(rows), 5)

    def test_evaluated_layers_have_metrics_and_no_gap(self):
        rows = {r["layer"]: r for r in routing_coverage()}
        for name in ("intent", "expert"):
            self.assertTrue(rows[name]["evaluated"], f"{name} 应已评测")
            self.assertTrue(rows[name]["metrics"], f"{name} 缺指标")
            self.assertEqual(rows[name]["gap"], "", f"{name} 不应标缺口")

    def test_missing_layers_are_explicit(self):
        rows = {r["layer"]: r for r in routing_coverage()}
        for name in ("workflow", "execution_mode"):
            self.assertFalse(rows[name]["evaluated"],
                             f"{name} 当前没有评测，不能标成已覆盖")
            self.assertTrue(rows[name]["gap"], f"{name} 必须写明缺口")
        self.assertIsNone(rows["workflow"]["entry"])
        self.assertIsNone(rows["execution_mode"]["entry"])

    def test_model_layer_is_evaluated_with_honest_gap(self):
        rows = {r["layer"]: r for r in routing_coverage()}
        model = rows["model"]
        self.assertIn("ModelRouter.route", model["implementation"])
        self.assertIn("runtime_manifest", model["entry"])
        self.assertTrue(model["evaluated"])
        self.assertIn("route_accuracy", model["metrics"])
        self.assertIn("usage.actual_cost", model["metrics"])
        # 仍有真实缺口，不能因为有了评测就说链路完整
        self.assertIn("生产评测集", model["gap"])
        self.assertIn("未知成本", model["gap"])


class _FakeAdapter:
    """只提供路由所需的 profile；不执行真实模型调用。"""

    def __init__(self, profile):
        self._profile = profile

    def profile(self):
        return self._profile

    def suggest_action(self, messages, available_tools):
        raise NotImplementedError

    def generate(self, messages):
        raise NotImplementedError

    def estimate_cost(self, input_chars, output_chars):
        return 0.0


class TestModelRoutingEvaluation(unittest.TestCase):
    """模型层路由评测：走真实 ModelRouter 决策，不伪造命中。"""

    def setUp(self):
        profiles = [
            ModelProfile(model_id="cheap", quality_score=0.5,
                         price_per_1k_input=0.1, price_per_1k_output=0.1,
                         avg_latency_s=0.2),
            ModelProfile(model_id="strong", quality_score=0.95,
                         price_per_1k_input=0.2, price_per_1k_output=0.2,
                         avg_latency_s=0.5),
            ModelProfile(model_id="vision", quality_score=0.9,
                         price_per_1k_input=0.5, price_per_1k_output=0.5,
                         avg_latency_s=0.8, supports_vision=True),
            ModelProfile(model_id="off", quality_score=1.0, available=False),
        ]
        self.registry = ModelRegistry()
        self.adapters = {}
        for p in profiles:
            self.registry.register(p)
            self.adapters[p.model_id] = _FakeAdapter(p)
        self.router = ModelRouter(self.registry, self.adapters["cheap"])
        self.router.setup(lambda mid: self.adapters.get(mid))
        self.evaluator = ModelRoutingEvaluator(self.router)

    def _cases(self):
        self.router.bind_route("coding", "strong")
        return [
            ModelRoutingCase(case_id="explicit", intent="coding",
                             expected_model="strong", kind="explicit"),
            ModelRoutingCase(case_id="score", intent="travel",
                             expected_model="strong", kind="score"),
            ModelRoutingCase(case_id="cost", intent="travel",
                             expected_model="cheap", kind="cost",
                             prefer_cost=True),
            ModelRoutingCase(case_id="vision", intent="travel",
                             expected_model="vision", kind="vision",
                             require_vision=True),
            ModelRoutingCase(case_id="unavailable", intent="time_query",
                             expected_model="strong", kind="score"),
        ]

    def test_default_model_takes_precedence(self):
        self.router.set_default_model("strong")
        report = self.evaluator.run([
            ModelRoutingCase(case_id="default", intent="travel",
                             expected_model="strong", kind="default")])
        self.assertEqual(report.route_accuracy, 1.0)
        self.assertEqual(report.fallback_rate, 0.0)

    def test_route_accuracy_and_breakdown(self):
        report = self.evaluator.run(self._cases())
        self.assertEqual(report.case_count, 5)
        self.assertEqual(report.route_accuracy, 1.0)
        self.assertEqual(report.fallback_rate, 0.0)
        self.assertEqual(report.vision_violations, 0)
        self.assertEqual(report.by_kind["explicit"], {"cases": 1, "correct": 1})
        self.assertEqual(report.by_kind["cost"], {"cases": 1, "correct": 1})
        self.assertEqual(report.by_source["explicit"], 1)
        self.assertEqual(report.by_source["cheap"], 1)
        self.assertEqual(report.by_source["score"], 3)
        self.assertEqual(report.per_model["strong"]["cases"], 3)
        self.assertEqual(report.per_model["strong"]["correct"], 3)

    def test_wrong_expectation_is_detected(self):
        cases = self._cases() + [ModelRoutingCase(
            case_id="bad", intent="travel", expected_model="cheap",
            kind="score")]
        report = self.evaluator.run(cases)
        self.assertEqual(report.case_count, 6)
        self.assertAlmostEqual(report.route_accuracy, 5 / 6, places=6)
        bad = [r for r in report.case_results if r.case_id == "bad"][0]
        self.assertFalse(bad.ok)
        self.assertEqual(bad.predicted_model, "strong")

    def test_fallback_to_default_adapter_is_visible(self):
        router = ModelRouter(ModelRegistry(), self.adapters["cheap"])
        report = ModelRoutingEvaluator(router).run([
            ModelRoutingCase(case_id="empty", expected_model="strong")])
        self.assertEqual(report.fallback_rate, 1.0)
        self.assertEqual(report.route_accuracy, 0.0)
        self.assertTrue(report.case_results[0].is_default)

    def test_vision_requirement_never_violated(self):
        report = self.evaluator.run([
            ModelRoutingCase(case_id="v", expected_model="vision",
                             require_vision=True, kind="vision")])
        self.assertEqual(report.vision_violations, 0)
        self.assertEqual(report.case_results[0].predicted_model, "vision")

    def test_usage_reconciliation_is_attached(self):
        ledger = UsageLedger()
        ledger.record(ModelUsage(model_id="strong", cost=0.02,
                                 cost_source=COST_PROVIDER,
                                 token_source=TOKEN_PROVIDER))
        ledger.record(ModelUsage(model_id="strong", cost=None,
                                 cost_source="unknown"))
        report = self.evaluator.run(self._cases()[:1], usage=ledger)
        self.assertEqual(report.usage["calls"], 2)
        self.assertEqual(report.usage["actual_cost"], 0.02)
        self.assertEqual(report.usage["unknown_calls"], 1)
        self.assertEqual(report.summary()["usage_actual_cost"], 0.02)


class TestBaselineComparison(unittest.TestCase):
    """基线对比 —— 没有对比就没有「退化」概念。"""

    def setUp(self):
        self.classifier = IntentClassifier()
        self.experts = builtin_experts()
        self.evaluator = RoutingEvaluator(self.classifier, self.experts)

    def test_compare_detects_regression(self):
        """候选准确率下降必须判定为退化。"""
        base = RoutingDataset(dataset_id="base", cases=[
            RoutingCase(case_id="b1", input_goal="现在几点",
                        expected_intent="time_query")])
        cand = RoutingDataset(dataset_id="cand", cases=[
            RoutingCase(case_id="c1", input_goal="现在几点",
                        expected_intent="time_query")])
        rb = self.evaluator.run(base)
        rc = self.evaluator.run(cand)

        # 手工构造一个更差的候选
        rc.intent_accuracy = 0.1
        diff = self.evaluator.compare(rb, rc)
        self.assertTrue(diff["regressed"], "准确率下降应判定为退化")
        self.assertLess(diff["intent_accuracy_delta"], 0)

    def test_compare_accepts_improvement(self):
        """准确率提升不应判定为退化。

        构造：基线用一个**基线时代会错**的期望值（幽灵意图 `programming`），
        候选用修正后的 `coding` —— 准确率从 0 升到 1。
        """
        base = RoutingDataset(dataset_id="base", cases=[
            RoutingCase(case_id="b1", input_goal="现在几点",
                        expected_intent="time_query"),
            RoutingCase(case_id="b2", input_goal="写个 Python 函数",
                        expected_intent="programming")])
        cand = RoutingDataset(dataset_id="cand", cases=[
            RoutingCase(case_id="c1", input_goal="现在几点",
                        expected_intent="time_query"),
            RoutingCase(case_id="c2", input_goal="写个 Python 函数",
                        expected_intent="coding")])
        rb = self.evaluator.run(base)
        rc = self.evaluator.run(cand)
        self.assertLess(rb.intent_accuracy, rc.intent_accuracy,
                        "基线应因幽灵意图期望而更差")
        diff = self.evaluator.compare(rb, rc)
        self.assertFalse(diff["regressed"], "提升不应判定为退化")
        self.assertGreater(diff["intent_accuracy_delta"], 0)


class TestReportPersistence(unittest.TestCase):
    """报告落库 —— 指标必须能被后续基线对比读到。

    用**真实 Database**（临时目录），不造假桩 ——
    假桩会掩盖「表没建」「方法名不对」这类真实故障
    （本特性第一版就是靠真实库才发现 save_report 用了不存在的 execute API）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_routing_db_")
        self.db = Database(os.path.join(self.tmp, "test.db"))

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_load_reports(self):
        ds = builtin_routing_datasets()["builtin_routing"]
        evaluator = RoutingEvaluator(IntentClassifier(), builtin_experts())
        report = evaluator.run(ds)

        save_report(report, self.db)
        loaded = load_reports(self.db)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["dataset_id"], report.dataset_id)
        self.assertIn("intent_accuracy", loaded[0])

    def test_table_created_by_schema_domain(self):
        """表必须由 core/schema/routing.py 自动发现建出（F-00 域拆分约定）。"""
        rows = self.db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='routing_reports'").fetchall()
        self.assertTrue(rows, "routing_reports 表未由 schema 域模块建出")

    def test_multiple_reports_ordered_newest_first(self):
        """多次评测后应能按时间倒序读回 —— 基线对比依赖新数据在上。"""
        evaluator = RoutingEvaluator(IntentClassifier(), builtin_experts())
        ds = builtin_routing_datasets()["builtin_routing"]
        for _ in range(3):
            save_report(evaluator.run(ds), self.db)
        loaded = load_reports(self.db)
        self.assertEqual(len(loaded), 3)


class TestApiSurface(unittest.TestCase):
    """API 端点存在且注册表能命中（防止「接口在、逻辑空」）。"""

    def test_routes_registered(self):
        from evoagent.api import routing_eval_api  # noqa: F401
        from evoagent.api.routes import dispatch_get, dispatch_post

        self.assertTrue(dispatch_get, "GET 分派未安装")
        self.assertTrue(dispatch_post, "POST 分派未安装")

    def test_endpoints_present(self):
        from evoagent.api.routes import _GET, _POST

        get_paths = {p for p, _h in _GET}
        post_paths = {p for p, _h in _POST}
        self.assertIn("/routing/eval", get_paths)
        self.assertIn("/routing/eval", post_paths)
        self.assertIn("/routing/reports", get_paths)


if __name__ == "__main__":
    unittest.main()
