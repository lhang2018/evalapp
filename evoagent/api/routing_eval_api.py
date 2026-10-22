"""路由质量评测接口（F-53 / R-03）。注册表路由——server.py 零改动。

暴露路由质量的真实数字：准确率 / 混淆矩阵 / 兜底率 / 弃权率。
在此之前，路由质量**从未被测量过**（R-03 主题 ⬜），
无法判断一次改动是让路由变好还是变坏。
"""

from .routes import get, post


def _build_evaluator(app):
    """构造评估器：优先用注入的模型（真实路径），否则退回纯启发式。"""
    from ..evolution.routing_eval import RoutingEvaluator

    classifier = getattr(app, "classifier", None)
    experts = getattr(app, "experts", None)
    if classifier is None or experts is None:
        return None
    return RoutingEvaluator(classifier, experts)


@get("/routing/eval")
def routing_eval_datasets(ctx):
    """列出可用路由评测集（含样本数与保留集标记）。"""
    from ..evolution.routing_eval import builtin_routing_datasets

    datasets = builtin_routing_datasets()
    ctx.json(200, {"datasets": [
        {"dataset_id": d.dataset_id,
         "version": d.version,
         "case_count": len(d.cases),
         "holdout": d.is_holdout(),
         "source": d.source}
        for d in datasets.values()]})


@post("/routing/eval")
def run_routing_eval(ctx):
    """跑一次路由评测并返回报告摘要。

    请求体：{"dataset_id": "builtin_routing", "top_k": 3,
             "low_confidence_threshold": 0.5}

    报告落库（routing_reports 表），供后续基线对比。
    """
    from ..evolution.routing_eval import (builtin_routing_datasets,
                                         routing_coverage, save_report)

    body = ctx.body or {}
    dataset_id = str(body.get("dataset_id") or "builtin_routing")
    datasets = builtin_routing_datasets()
    dataset = datasets.get(dataset_id)
    if dataset is None:
        ctx.json(404, {"error": f"未知评测集: {dataset_id}",
                       "available": sorted(datasets)})
        return

    evaluator = _build_evaluator(ctx.app)
    if evaluator is None:
        ctx.json(503, {"error": "路由评测不可用：未装配分类器或专家表"})
        return

    try:
        top_k = int(body.get("top_k", 3))
        threshold = float(body.get("low_confidence_threshold", 0.5))
    except (TypeError, ValueError):
        ctx.json(400, {"error": "top_k / low_confidence_threshold 非法"})
        return
    evaluator.top_k = max(1, top_k)
    evaluator.low_confidence_threshold = threshold

    report = evaluator.run(dataset)
    try:
        save_report(report, ctx.app.db)
    except Exception:  # noqa: BLE001 —— 落库失败不应丢掉评测结果
        pass

    regressions = evaluator.regression_cases(report)
    ctx.json(200, {
        "report": report.summary(),
        "confusion": report.confusion,
        "per_intent": report.per_intent,
        "per_expert": report.per_expert,
        "method_counts": report.method_counts,
        "misclassified": [
            {"case_id": r.case_id,
             "expected_intent": r.expected_intent,
             "predicted_intent": r.predicted_intent,
             "expected_expert": r.expected_expert,
             "predicted_expert": r.predicted_expert,
             "method": r.method,
             "confidence": r.confidence,
             "low_confidence": r.low_confidence}
            for r in report.case_results if not r.ok],
        "coverage": routing_coverage(),
        "regression_candidates": len(regressions),
    })


@post("/routing/model-eval")
def run_model_routing_eval(ctx):
    """模型层路由评测：调用方给出期望模型，服务端走真实 ModelRouter。

    请求体：{"cases": [{"case_id","expected_model","intent","domain",
                        "require_vision","prefer_cost","kind"}]}
    期望值必须由调用方提供 —— 评测器不拿路由自己的评分结果当答案。
    """
    from ..evolution.routing_eval import (MODEL_ROUTE_KINDS,
                                         ModelRoutingCase,
                                         ModelRoutingEvaluator)

    router = getattr(ctx.app, "router", None)
    if router is None:
        ctx.json(503, {"error": "模型路由未装配"})
        return
    body = ctx.body or {}
    raw = body.get("cases")
    if not isinstance(raw, list) or not raw:
        ctx.json(400, {"error": "cases 必须是非空数组"})
        return
    cases = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            ctx.json(400, {"error": f"cases[{i}] 必须是对象"})
            return
        expected = str(item.get("expected_model") or "")
        kind = str(item.get("kind") or "score")
        if not expected:
            ctx.json(400, {"error": f"cases[{i}] 缺少 expected_model"})
            return
        if kind not in MODEL_ROUTE_KINDS:
            ctx.json(400, {"error": f"cases[{i}] 未知 kind: {kind}"})
            return
        cases.append(ModelRoutingCase(
            case_id=str(item.get("case_id") or f"case_{i + 1}"),
            expected_model=expected,
            intent=str(item.get("intent") or ""),
            domain=str(item.get("domain") or ""),
            require_vision=bool(item.get("require_vision", False)),
            prefer_cost=bool(item.get("prefer_cost", False)),
            kind=kind))
    report = ModelRoutingEvaluator(router).run(cases)
    ctx.json(200, {
        "report": report.summary(),
        "by_kind": report.by_kind,
        "by_source": report.by_source,
        "per_model": report.per_model,
        "failures": [
            {"case_id": r.case_id, "expected_model": r.expected_model,
             "predicted_model": r.predicted_model, "source": r.source,
             "error": r.error}
            for r in report.case_results if not r.ok],
    })


@get("/routing/coverage")
def routing_coverage_layers(ctx):
    """五层路由覆盖复核：哪些层有真实入口/评测，哪些仍是缺口。"""
    from ..evolution.routing_eval import ROUTING_LAYERS, routing_coverage

    ctx.json(200, {"layers": routing_coverage(),
                   "order": list(ROUTING_LAYERS)})


@get("/routing/reports")
def list_routing_reports(ctx):
    """历史路由评测报告摘要（最近 N 条）。"""
    from ..evolution.routing_eval import load_reports

    try:
        limit = int(ctx.query.get("limit", "20"))
    except (TypeError, ValueError):
        ctx.json(400, {"error": "非法 limit"})
        return
    try:
        ctx.json(200, {"reports": load_reports(ctx.app.db, limit=limit)})
    except Exception as e:  # noqa: BLE001
        ctx.json(500, {"error": f"读取报告失败: {e}"})
