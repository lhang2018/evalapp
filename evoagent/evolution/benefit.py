"""进化收益评价（PRD 50 / Architecture 12.3）。

十维收益：任务质量、任务成功率、稳定性、恢复能力、运行成本、执行时延、
安全风险、维护复杂度、跨领域回归、长期收益。

设计约束：
- 每个维度返回 value / status / evidence；证据不足时明确
  ``insufficient_evidence``，不用默认值伪造收益。
- 长期收益只能由发布后监测的持续样本给出（PRD 82），单次实验不推断。
- 稳定性在只有单次运行数据时使用失败证据代理，不冒充重复运行方差。
"""

from typing import Any, Dict, Optional

from .evaluator import EvalReport

DIFFICULTY_WEIGHTS = {"easy": 1.0, "medium": 1.5, "hard": 2.0}

COMPLEXITY_SCORE = {
    "task_local": 0,
    "local": 0,
    "workflow": 1,
    "tool_or_adapter": 2,
    "tool": 2,
    "adapter": 2,
    "expert": 3,
    "shared": 4,
    "critical": 5,
    "core": 5,
}

LONG_TERM_MIN_SAMPLES = 20


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    return value


def _weighted_quality(report: EvalReport) -> Optional[float]:
    passed_weight = 0.0
    total_weight = 0.0
    for level, bucket in (report.difficulty_breakdown or {}).items():
        cases = int(bucket.get("cases", 0) or 0)
        passed = int(bucket.get("passed", 0) or 0)
        weight = DIFFICULTY_WEIGHTS.get(level, 1.0)
        total_weight += cases * weight
        passed_weight += passed * weight
    if total_weight <= 0:
        return None
    return passed_weight / total_weight


def _stability_proxy(report: EvalReport) -> Optional[float]:
    """失败证据代理：1 - 约束违反率 - 工具失败率。"""
    if report.case_count <= 0:
        return None
    unstable = (report.constraint_violations + report.tool_failures)
    return max(0.0, 1.0 - unstable / report.case_count)


def _delta(candidate: Optional[float],
           baseline: Optional[float]) -> Optional[float]:
    if candidate is None or baseline is None:
        return None
    return _round(candidate - baseline)


def _higher_better(candidate: Optional[float], baseline: Optional[float],
                   epsilon: float = 0.02) -> str:
    d = _delta(candidate, baseline)
    if d is None:
        return "insufficient_evidence"
    if d > epsilon:
        return "improved"
    if d < -epsilon:
        return "regressed"
    return "neutral"


def _lower_better(candidate: Optional[float], baseline: Optional[float],
                  epsilon: float = 0.05) -> str:
    if candidate is None or baseline is None:
        return "insufficient_evidence"
    if baseline == 0:
        return "neutral" if candidate == 0 else "regressed"
    ratio = candidate / baseline
    if ratio < 1.0 - epsilon:
        return "improved"
    if ratio > 1.0 + epsilon:
        return "regressed"
    return "neutral"


def _domain_rates(report: EvalReport) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for row in report.case_results or []:
        domain = str(row.get("domain") or "general")
        bucket = out.setdefault(domain, {"cases": 0, "passed": 0})
        bucket["cases"] += 1
        bucket["passed"] += 1 if row.get("ok") else 0
    return out


def _cross_domain(baseline: Optional[EvalReport],
                  candidate: EvalReport,
                  target_domain: str,
                  threshold: float = 0.10) -> Dict[str, Any]:
    if baseline is None:
        return {"status": "insufficient_evidence",
                "reason": "缺少基线评测", "domains": {}}
    base = _domain_rates(baseline)
    cand = _domain_rates(candidate)
    common = sorted(set(base) & set(cand))
    if len(common) < 2:
        return {"status": "insufficient_evidence",
                "reason": "评测集不足两个领域，无法判断跨领域回归",
                "domains": {}}
    domains: Dict[str, Any] = {}
    degraded: list = []
    max_drop = 0.0
    for domain in common:
        if domain == target_domain:
            continue
        b = base[domain]
        c = cand[domain]
        if not b["cases"] or not c["cases"]:
            continue
        br = b["passed"] / b["cases"]
        cr = c["passed"] / c["cases"]
        drop = _round(br - cr)
        max_drop = max(max_drop, drop)
        domains[domain] = {"baseline": _round(br),
                           "candidate": _round(cr),
                           "delta": _round(cr - br)}
        if cr < br - threshold:
            degraded.append(domain)
    if not domains:
        return {"status": "insufficient_evidence",
                "reason": "目标领域之外没有可比用例", "domains": {}}
    return {"status": "degraded" if degraded else "ok",
            "degraded_domains": degraded,
            "max_drop": _round(max_drop),
            "domains": domains}


def _maintenance_complexity(change_scope: str) -> Dict[str, Any]:
    scope = (change_scope or "").strip().lower()
    if scope not in COMPLEXITY_SCORE:
        return {"status": "insufficient_evidence",
                "value": {"scope": change_scope or "unknown"},
                "reason": "未知改动范围"}
    score = COMPLEXITY_SCORE[scope]
    level = "low" if score <= 1 else ("medium" if score <= 3 else "high")
    return {"status": "informational",
            "value": {"level": level, "scope": scope,
                      "score": score,
                      "priority": score}}


def _long_term(monitor: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    samples = int((monitor or {}).get("metrics", {}).get("samples", 0) or 0)
    if samples < LONG_TERM_MIN_SAMPLES:
        return {"status": "insufficient_evidence",
                "value": {"samples": samples,
                          "required_samples": LONG_TERM_MIN_SAMPLES},
                "reason": "长期收益需要发布后持续观测（PRD 82）"}
    metrics = monitor.get("metrics") or {}
    return {"status": "observed",
            "value": {"samples": samples,
                      "success_rate": metrics.get("success_rate"),
                      "cost_ratio": metrics.get("cost_ratio"),
                      "latency_ratio": metrics.get("latency_ratio")}}


def evaluate_benefit(baseline: Optional[EvalReport], candidate: EvalReport,
                     *, change_scope: str = "local",
                     risk_level: str = "L1",
                     safety: Optional[Dict[str, Any]] = None,
                     monitor: Optional[Dict[str, Any]] = None,
                     target_domain: str = "",
                     cross_domain_threshold: float = 0.10
                     ) -> Dict[str, Any]:
    """PRD 50：十维收益，返回可解释的维度向量。"""
    quality = _weighted_quality(candidate)
    base_quality = _weighted_quality(baseline) if baseline else None
    stability = _stability_proxy(candidate)
    base_stability = _stability_proxy(baseline) if baseline else None
    cand_rates = candidate.metric_rates()
    base_rates = baseline.metric_rates() if baseline else {}

    safety_passed = None
    if safety is not None:
        safety_passed = bool(safety.get("passed"))
    elif candidate.safety_passed is not None:
        safety_passed = bool(candidate.safety_passed)

    cross = _cross_domain(baseline, candidate, target_domain,
                          cross_domain_threshold)
    cost_ratio = None
    if baseline is not None and baseline.cost > 0:
        cost_ratio = _round(candidate.cost / baseline.cost)

    dimensions: Dict[str, Dict[str, Any]] = {
        "quality": {
            "value": {"weighted": _round(quality),
                      "baseline": _round(base_quality),
                      "delta": _delta(quality, base_quality),
                      "weights": DIFFICULTY_WEIGHTS},
            "status": _higher_better(quality, base_quality),
        },
        "success_rate": {
            "value": {"candidate": _round(candidate.success_rate),
                      "baseline": _round(baseline.success_rate)
                      if baseline else None,
                      "delta": _delta(candidate.success_rate,
                                      baseline.success_rate if baseline else None)},
            "status": _higher_better(candidate.success_rate,
                                     baseline.success_rate if baseline else None),
        },
        "stability": {
            "value": {"candidate": _round(stability),
                      "baseline": _round(base_stability),
                      "delta": _delta(stability, base_stability),
                      "method": "failure_evidence_proxy"},
            "status": _higher_better(stability, base_stability),
        },
        "recovery": {
            "value": {"candidate": cand_rates.get("recovery_rate"),
                      "baseline": base_rates.get("recovery_rate"),
                      "delta": _delta(cand_rates.get("recovery_rate"),
                                      base_rates.get("recovery_rate"))},
            "status": _higher_better(cand_rates.get("recovery_rate"),
                                     base_rates.get("recovery_rate")),
        },
        "cost": {
            "value": {"candidate": _round(candidate.cost),
                      "baseline": _round(baseline.cost) if baseline else None,
                      "delta": _round(candidate.cost - baseline.cost)
                      if baseline else None,
                      "ratio": cost_ratio},
            "status": _lower_better(candidate.cost,
                                    baseline.cost if baseline else None),
        },
        "latency": {
            "value": {"candidate": _round(candidate.avg_latency),
                      "baseline": _round(baseline.avg_latency)
                      if baseline else None,
                      "delta": _round(candidate.avg_latency
                                      - baseline.avg_latency)
                      if baseline else None,
                      "ratio": _round(candidate.avg_latency
                                      / baseline.avg_latency)
                      if baseline and baseline.avg_latency > 0 else None},
            "status": _lower_better(candidate.avg_latency,
                                    baseline.avg_latency if baseline else None),
        },
        "safety": {
            "value": {"passed": safety_passed, "risk_level": risk_level},
            "status": ("passed" if safety_passed
                       else "failed" if safety_passed is False
                       else "insufficient_evidence"),
        },
        "maintenance_complexity": _maintenance_complexity(change_scope),
        "cross_domain": cross,
        "long_term": _long_term(monitor),
    }

    reasons: list = []
    recommendation = "review"
    if safety_passed is False:
        reasons.append("安全测试未通过")
        recommendation = "reject"
    elif cross.get("status") == "degraded":
        reasons.append("跨领域回归: " + ", ".join(cross.get("degraded_domains") or []))
        recommendation = "review"
    elif dimensions["quality"]["status"] == "regressed" \
            or dimensions["success_rate"]["status"] == "regressed":
        reasons.append("任务质量或成功率回归")
        recommendation = "review"
    elif any(d["status"] == "improved" for d in dimensions.values()):
        recommendation = "accept"

    return {"dimensions": dimensions,
            "recommendation": recommendation,
            "reasons": reasons,
            "policy": {"long_term_min_samples": LONG_TERM_MIN_SAMPLES,
                       "cross_domain_threshold": cross_domain_threshold}}
