"""路由质量评测与纠错（PRD 8 / Architecture 4.2 —— R-03 主题）。

背景
----
本模块补上一个长期存在的空白：**路由从来没有被评测过**。

`evolution/datasets.py` 的两个内置集共 4 个用例，全是 time_query/computation
通用任务，**零个路由/分类用例**。因此以下问题一直无法回答：

  - 意图分类准确率是多少？哪些意图最容易混？
  - 路由到专家的正确率是多少？哪些专家被误命中？
  - 启发式兜底（heuristic / heuristic_fallback）占多少比例？
    —— 兜底率高说明模型分类在真实输入上大量失败
  - 弃权率（低置信挂起）是多少？澄清是否被滥用？
  - top-k（候选意图排序）里正确答案排第几？

**没有这些数字，就无法判断路由质量是变好了还是变坏了** ——
而路由错了，后面规划、工具选择、专家提示词全部跟着错。

本模块提供
--------
1. `RoutingCase` / `RoutingDataset` —— 路由专用评测集（与任务级 `EvalCase` 分离：
   路由评测关注的是**分类决策**，不需要真的执行任务）
2. `RoutingEvaluator` —— 产出 `RoutingReport`，含准确率/混淆矩阵/top-k/回退率/弃权率
3. `RoutingRegressionCase` —— **低置信样本回流**：把模型分不清的样本固化成
   回归用例，下次改路由时自动验证没退化
4. `builtin_routing_datasets()` —— 内置路由评测集，覆盖全部已注册意图

设计约束（与 FEATURE_SPLIT.md §5.4 DoD 一致）
------------------------------------------------
- **不碰热点文件**：本模块只读 `IntentClassifier` 与 `ExpertRegistry`，不修改它们
- **不造第二个枚举权威**：意图名一律从 classifier / registry 现取，不本地硬编码
- **零调用方不留空壳**：每个指标都有产生它的代码路径，且有走通路径的测试
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# 分类方法常量（与 planning/intent.py::IntentClassifier.classify 的返回值对应）
METHOD_HINT = "hint"
METHOD_MODEL = "model"
METHOD_HEURISTIC = "heuristic"
METHOD_HEURISTIC_FALLBACK = "heuristic_fallback"
METHOD_ROUTE = "route"

#: 视为「走了兜底」的方法集合 —— 兜底率越高说明模型分类失败越多
FALLBACK_METHODS = frozenset({METHOD_HEURISTIC, METHOD_HEURISTIC_FALLBACK,
                               METHOD_ROUTE})


@dataclass
class RoutingCase:
    """单条路由评测用例。

    与 `evaluator.EvalCase` 的区别：路由评测**不执行任务**，只判断分类决策对不对。
    因此不需要 `expected` 验收标准，只需要期望的意图/专家。
    """

    case_id: str
    input_goal: str
    expected_intent: str
    expected_expert: str = ""
    #: 允许的正确意图集合（歧义意图用，如 travel/weather 边界）
    acceptable_intents: List[str] = field(default_factory=list)
    #: 期望的分类方法（空=不限；"heuristic_fallback" = 期望模型失败走兜底）
    expected_method: str = ""
    difficulty: str = "easy"
    holdout: bool = False
    tags: List[str] = field(default_factory=list)
    #: 显式 hint（有 hint 时分类器直接短路，不应调用模型）
    hint: str = ""
    domain_hint: str = ""

    def acceptable(self) -> List[str]:
        """全部可接受的意图（含 expected_intent 本身）。"""
        return [self.expected_intent] + [
            i for i in self.acceptable_intents if i != self.expected_intent]


@dataclass
class RoutingDataset:
    dataset_id: str
    version: str = "1.0.0"
    task_category: str = "routing"
    source: str = "builtin"
    holdout: bool = False
    cases: List[RoutingCase] = field(default_factory=list)

    def is_holdout(self) -> bool:
        return self.holdout or any(c.holdout for c in self.cases)


@dataclass
class RoutingCaseResult:
    case_id: str
    ok: bool
    expected_intent: str
    predicted_intent: str
    predicted_expert: str
    expected_expert: str
    method: str
    confidence: float
    #: 正确答案在候选列表中的排名（1-based）；不在候选里为 0
    rank: int = 0
    #: 是否低置信（模型不敢确定）—— 这些样本会被回流为回归用例
    low_confidence: bool = False
    difficulty: str = "easy"
    holdout: bool = False
    tags: List[str] = field(default_factory=list)
    error: str = ""


@dataclass
class RoutingReport:
    """路由评测报告。

    指标全部由实际运行产出，不接受外部传入 —— 避免"报告好看但没跑过"。
    """

    dataset_id: str
    version: str = "1.0.0"
    holdout: bool = False
    case_count: int = 0
    #: 意图分类准确率（predicted == expected 或在 acceptable 集合内）
    intent_accuracy: float = 0.0
    #: 专家路由准确率（仅对声明了 expected_expert 的用例计算）
    expert_accuracy: float = 0.0
    expert_case_count: int = 0
    #: top-k 命中率：正确答案落在候选列表前 k 位
    top1_accuracy: float = 0.0
    top3_accuracy: float = 0.0
    #: 兜底率：走 heuristic/heuristic_fallback/route 的比例
    fallback_rate: float = 0.0
    #: 弃权率：低置信（置信度低于阈值）被挂起澄清的比例
    abstention_rate: float = 0.0
    #: 平均置信度
    avg_confidence: float = 0.0
    #: 混淆矩阵：actual_intent -> {predicted_intent: count}
    confusion: Dict[str, Dict[str, int]] = field(default_factory=dict)
    #: 每个意图的样本数与正确数
    per_intent: Dict[str, Dict[str, int]] = field(default_factory=dict)
    #: 每个专家的样本数与正确数
    per_expert: Dict[str, Dict[str, int]] = field(default_factory=dict)
    #: 分类方法分布
    method_counts: Dict[str, int] = field(default_factory=dict)
    case_results: List[RoutingCaseResult] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        """摘要视图（用于日志/事件负载/台账引用）。"""
        return {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "case_count": self.case_count,
            "intent_accuracy": round(self.intent_accuracy, 4),
            "expert_accuracy": round(self.expert_accuracy, 4),
            "top1_accuracy": round(self.top1_accuracy, 4),
            "top3_accuracy": round(self.top3_accuracy, 4),
            "fallback_rate": round(self.fallback_rate, 4),
            "abstention_rate": round(self.abstention_rate, 4),
            "avg_confidence": round(self.avg_confidence, 4),
            "holdout": self.holdout,
        }


# ---------------------------------------------------------------- 候选意图

#: 候选意图提供器：由宿主注入（默认用启发式近似）。
#: 真实实现应给出**排序后的候选列表**（top-k 的 k 来源）。
CandidateProvider = Callable[[str, str], Sequence[str]]


def _keyword_candidates(goal: str, domain_hint: str = "") -> List[str]:
    """按启发式关键词给候选意图排序（近似 top-k）。

    这是**兜底实现**：真实系统应由模型给出带分数的候选。
    但它能提供确定性的、可复现的排序，让 top-k 指标有意义。
    """
    from ..planning.intent import _KEYWORDS  # 单一权威，不本地复制关键词

    text = (goal or "").lower()
    scored: List[Tuple[int, str]] = []
    for intent, words in _KEYWORDS:
        hits = sum(1 for w in words if w in text)
        if hits:
            scored.append((hits, intent))
    # 命中数降序；同分按关键词表原序（稳定）
    scored.sort(key=lambda x: -x[0])
    out = [intent for _hits, intent in scored]
    if domain_hint:
        out.insert(0, domain_hint)
    if "general" not in out:
        out.append("general")
    return out


# ---------------------------------------------------------------- 评估器

class RoutingEvaluator:
    """路由质量评估器。

    用法::

        evaluator = RoutingEvaluator(classifier, experts)
        report = evaluator.run(builtin_routing_datasets()["builtin_routing"])
        print(report.summary())

    参数
    ----
    classifier
        `planning.intent.IntentClassifier` 实例（只读调用）
    experts
        `experts.registry.ExpertRegistry` 实例（只读调用）
    top_k
        候选列表长度上限，默认 3
    low_confidence_threshold
        低于此置信度视为「模型不确定」，会被回流为回归用例
    candidates
        候选意图提供器；默认用关键词启发式近似
    """

    def __init__(self, classifier: Any, experts: Any, *,
                 top_k: int = 3,
                 low_confidence_threshold: float = 0.5,
                 candidates: Optional[CandidateProvider] = None):
        self.classifier = classifier
        self.experts = experts
        self.top_k = max(1, int(top_k))
        self.low_confidence_threshold = float(low_confidence_threshold)
        self.candidates = candidates or _keyword_candidates

    # ---- 单例 ----
    def evaluate_case(self, case: RoutingCase) -> RoutingCaseResult:
        intent, domain, conf, method = self.classifier.classify(
            case.input_goal, hint=case.hint or None,
            domain_hint=case.domain_hint or None)
        expert_id = ""
        try:
            expert = self.experts.route(intent or "general", domain or "")
            expert_id = getattr(expert, "expert_id", "") or ""
        except Exception:  # noqa: BLE001 —— 路由异常不应中断整批评测
            expert_id = ""

        cands = list(self.candidates(case.input_goal, case.domain_hint or ""))[:self.top_k]
        rank = 0
        for i, c in enumerate(cands, 1):
            if c in case.acceptable():
                rank = i
                break

        ok = intent in case.acceptable()
        expert_ok = (not case.expected_expert) or (expert_id == case.expected_expert)
        method_ok = (not case.expected_method) or (method == case.expected_method)
        low_conf = conf < self.low_confidence_threshold

        return RoutingCaseResult(
            case_id=case.case_id,
            ok=bool(ok and expert_ok and method_ok),
            expected_intent=case.expected_intent,
            predicted_intent=intent,
            predicted_expert=expert_id,
            expected_expert=case.expected_expert,
            method=method,
            confidence=float(conf),
            rank=rank,
            low_confidence=low_conf,
            difficulty=case.difficulty,
            holdout=case.holdout,
            tags=list(case.tags),
        )

    # ---- 整批 ----
    def run(self, dataset: RoutingDataset) -> RoutingReport:
        report = RoutingReport(dataset_id=dataset.dataset_id,
                               version=dataset.version,
                               holdout=dataset.is_holdout(),
                               case_count=len(dataset.cases))
        confusion: Dict[str, Dict[str, int]] = defaultdict(Counter)
        per_intent: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"cases": 0, "correct": 0})
        per_expert: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"cases": 0, "correct": 0})
        method_counts: Counter = Counter()

        intent_ok = 0
        expert_total = 0
        expert_ok = 0
        top1 = 0
        top3 = 0
        fallback = 0
        abstained = 0
        conf_sum = 0.0

        for case in dataset.cases:
            res = self.evaluate_case(case)
            report.case_results.append(res)

            confusion[case.expected_intent][res.predicted_intent] += 1
            method_counts[res.method] += 1
            conf_sum += res.confidence

            ib = per_intent[case.expected_intent]
            ib["cases"] += 1
            if res.predicted_intent in case.acceptable():
                ib["correct"] += 1
                intent_ok += 1

            if case.expected_expert:
                eb = per_expert[case.expected_expert]
                eb["cases"] += 1
                expert_total += 1
                if res.predicted_expert == case.expected_expert:
                    eb["correct"] += 1
                    expert_ok += 1

            if res.rank == 1:
                top1 += 1
            if 1 <= res.rank <= 3:
                top3 += 1
            if res.method in FALLBACK_METHODS:
                fallback += 1
            if res.low_confidence:
                abstained += 1

        n = len(dataset.cases)
        if n:
            report.intent_accuracy = intent_ok / n
            report.top1_accuracy = top1 / n
            report.top3_accuracy = top3 / n
            report.fallback_rate = fallback / n
            report.abstention_rate = abstained / n
            report.avg_confidence = conf_sum / n
        if expert_total:
            report.expert_accuracy = expert_ok / expert_total
            report.expert_case_count = expert_total

        report.confusion = {k: dict(v) for k, v in confusion.items()}
        report.per_intent = {k: dict(v) for k, v in per_intent.items()}
        report.per_expert = {k: dict(v) for k, v in per_expert.items()}
        report.method_counts = dict(method_counts)
        return report

    # ---- 回归回流 ----
    def regression_cases(self, report: RoutingReport, *,
                         only_low_confidence: bool = True,
                         max_cases: int = 50) -> List[RoutingCase]:
        """把失败/低置信样本回流为回归用例。

        这是 R-03 的「纠错」环节：模型分不清的样本必须固化成用例，
        否则改路由时**无从判断是否退化**。

        只回流 `holdout=False` 的样本 —— 保留集是独立验收数据，
        拿来调优会破坏其独立性（PRD 52）。
        """
        out: List[RoutingCase] = []
        for res in report.case_results:
            if res.holdout:
                continue
            if only_low_confidence and not (res.low_confidence or not res.ok):
                continue
            out.append(RoutingCase(
                case_id=f"reg_{res.case_id}",
                input_goal="",   # 原始输入不落库，只留判定痕迹
                expected_intent=res.expected_intent,
                expected_expert=res.expected_expert,
                difficulty=res.difficulty,
                tags=sorted(set(res.tags) | {
                    "regression", "low_confidence" if res.low_confidence
                    else "misclassified"}),
            ))
            if len(out) >= max_cases:
                break
        return out

    # ---- 基线对比 ----
    def compare(self, baseline: RoutingReport,
                candidate: RoutingReport) -> Dict[str, Any]:
        """基线 vs 候选的路由质量对比。

        退化判定：意图准确率下降、专家准确率下降、兜底率上升 → 退化。
        """
        d_intent = candidate.intent_accuracy - baseline.intent_accuracy
        d_expert = (candidate.expert_accuracy - baseline.expert_accuracy
                    if baseline.expert_case_count and candidate.expert_case_count
                    else 0.0)
        d_fallback = candidate.fallback_rate - baseline.fallback_rate
        regressed = d_intent < 0 or d_expert < 0 or d_fallback > 0
        return {
            "baseline_dataset": baseline.dataset_id,
            "candidate_dataset": candidate.dataset_id,
            "intent_accuracy_delta": round(d_intent, 4),
            "expert_accuracy_delta": round(d_expert, 4),
            "fallback_rate_delta": round(d_fallback, 4),
            "top1_accuracy_delta": round(
                candidate.top1_accuracy - baseline.top1_accuracy, 4),
            "regressed": bool(regressed),
            "baseline": baseline.summary(),
            "candidate": candidate.summary(),
        }


# ---------------------------------------------------------------- 内置评测集

def builtin_routing_datasets() -> Dict[str, RoutingDataset]:
    """内置路由评测集。

    覆盖全部**已注册**意图（构造时从 IntentClassifier 的描述表取，
    不本地硬编码意图清单 —— 避免与真实注册表漂移）。

    `expected_expert` 从 `ExpertRegistry` 反查（按 `applicable_intents` 匹配），
    **不本地硬编码专家名** —— 否则专家表一变评测集就悄悄过期。
    """
    from ..planning.intent import _DESCRIPTIONS
    from ..experts.builtin import builtin_experts

    intents = sorted(_DESCRIPTIONS.keys())

    # 反查：意图 -> 第一个适用该意图的已注册专家
    registry = builtin_experts()
    expert_of: Dict[str, str] = {}
    for expert in registry.all():
        for it in expert.applicable_intents:
            expert_of.setdefault(it, expert.expert_id)

    cases: List[RoutingCase] = []
    # 每种意图两条：一条直接命中，一条带干扰词
    samples = {
        "time_query": ("现在几点了", "帮我看下今天的日期"),
        "computation": ("计算 3 * 7", "算出 100 除以 4 等于多少"),
        "coding": ("帮我写一个 Python 函数", "这段代码报错了，调试一下"),
        "travel": ("规划去成都的三天行程", "帮我安排一下出差路线"),
        "weather": ("北京今天天气怎么样", "明天会下雨吗"),
        "comic_drama": ("做一个漫剧分镜", "给这个漫画配音"),
        "mobile_operation": ("打开手机上的相机", "点击屏幕上的按钮"),
        "note_taking": ("帮我记个笔记", "记录一下待办事项"),
        "general": ("你好", "讲个笑话"),
    }
    for intent in intents:
        goal, goal2 = samples.get(intent, (f"处理一个 {intent} 任务", ""))
        cases.append(RoutingCase(
            case_id=f"route_{intent}",
            input_goal=goal,
            expected_intent=intent,
            expected_expert=expert_of.get(intent, ""),
            difficulty="easy",
            tags=["builtin", intent],
        ))
        if goal2:
            cases.append(RoutingCase(
                case_id=f"route_{intent}_b",
                input_goal=goal2,
                expected_intent=intent,
                expected_expert=expert_of.get(intent, ""),
                difficulty="medium",
                tags=["builtin", intent],
            ))

    # 歧义用例：travel/weather 边界，允许两个意图都算对
    cases.append(RoutingCase(
        case_id="route_ambiguous_travel_weather",
        input_goal="去杭州的行程顺便看看天气",
        expected_intent="travel",
        expected_expert=expert_of.get("travel", ""),
        acceptable_intents=["weather"],
        difficulty="hard",
        tags=["builtin", "ambiguous"],
    ))

    # 保留集：独立于调优集，用于验收
    holdout_cases = [
        RoutingCase(
            case_id="route_holdout_coding",
            input_goal="帮我重构这个模块并补上单元测试",
            expected_intent="coding",
            expected_expert=expert_of.get("coding", ""),
            difficulty="hard",
            holdout=True,
            tags=["builtin", "holdout"],
        ),
        RoutingCase(
            case_id="route_holdout_time",
            input_goal="现在星期几",
            expected_intent="time_query",
            expected_expert=expert_of.get("time_query", ""),
            difficulty="medium",
            holdout=True,
            tags=["builtin", "holdout"],
        ),
    ]

    return {
        "builtin_routing": RoutingDataset(
            dataset_id="builtin_routing",
            version="1.0.0",
            task_category="routing",
            source="builtin",
            cases=cases,
        ),
        "builtin_routing_holdout": RoutingDataset(
            dataset_id="builtin_routing_holdout",
            version="1.0.0",
            task_category="routing",
            source="builtin_holdout",
            holdout=True,
            cases=holdout_cases,
        ),
    }


# ---------------------------------------------------------------- 报告持久化

def save_report(report: RoutingReport, db: Any) -> None:
    """把路由评测报告落库（供 /metrics 与基线对比）。

    走 `Database` 的具名方法（`save_routing_report`）——
    本项目**没有**通用 execute/query 接口（见 core/persistence.py）。
    表结构在 `core/schema/routing.py`（F-00 域拆分约定）。
    """
    db.save_routing_report(report.dataset_id, report.version,
                           report.summary())


def load_reports(db: Any, limit: int = 20) -> List[Dict[str, Any]]:
    """读取最近的路由评测报告摘要。"""
    return db.list_routing_reports(limit=limit)
