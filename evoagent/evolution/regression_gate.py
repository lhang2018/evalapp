"""Independent, fail-closed execution of every suite required by change impact."""
from dataclasses import replace

from .evaluator import EvalDataset, ReleaseGate


def isolation_problems(dataset, holdout):
    if holdout is None or dataset.is_holdout():
        return []
    training_ids = {c.case_id for c in dataset.cases}
    inputs = {(c.input_goal, c.intent, c.domain) for c in dataset.cases}
    overlap = training_ids & {c.case_id for c in holdout.cases}
    overlap_inputs = inputs & {(c.input_goal, c.intent, c.domain) for c in holdout.cases}
    if overlap or overlap_inputs:
        return ["独立保留集与评测集重叠，禁止继续评测/调优"]
    return []


def component_impact(registry, component, store=None):
    if registry is None:
        return None
    caps = {c.capability_id: c for c in registry.capabilities()}
    roots = [component] if component in caps else []
    part = component.split("/")
    if not roots and len(part) >= 2 and part[0] == "expert":
        expert = registry.get(part[1])
        roots = [cid for cid, c in caps.items() if c.domain == part[1] or
                 (expert and cid in expert.capabilities)]
    if not roots and len(part) >= 2 and part[0] == "tool":
        roots = [cid for cid, c in caps.items() if part[1] in c.tool_ids]
    resolved = bool(roots)
    if not roots:
        roots = list(caps)  # unknown/global component never means no regression
    analyses = [registry.impact_analysis(cid) for cid in roots]
    result = {key: sorted({v for a in analyses for v in a.get(key, [])})
              for key in ("directly_affected", "depends_on", "affected_closure", "upstream_closure")}
    affected = set(roots) | set(result["affected_closure"])
    suites = {v for a in analyses for v in a.get("must_rerun_tests", [])}
    suites.update(caps[cid].eval_suite for cid in affected if cid in caps and caps[cid].eval_suite)
    if not resolved:
        suites.update(("builtin_smoke", "builtin_holdout", "builtin_safety"))
    ordered = [x for x in ("builtin_smoke", "builtin_holdout", "builtin_safety") if x in suites]
    ordered += sorted(suites - set(ordered))
    result.update(component=component, mapped_roots=sorted(roots),
                  mapping_status="resolved" if resolved else "conservative_all",
                  required_domains=sorted({caps[cid].domain for cid in affected if cid in caps
                                           and caps[cid].domain not in ("", "shared")}),
                  must_rerun_tests=ordered, can_reuse_evidence=False)
    from .asset_impact import asset_impact
    assets = asset_impact(store, component, roots)
    result.update(assets)
    result["must_rerun_tests"] += [suite for suite in assets.get("asset_required_suites", [])
                                  if suite not in result["must_rerun_tests"]]
    result["required_domains"] = sorted(set(result["required_domains"]) |
                                        set(assets.get("asset_required_domains", [])))
    return result


def run_pair(controller, dataset, candidate):
    runner = getattr(controller.evaluator, "runner", None)
    previous = getattr(runner, "suite_ref", None)
    if previous is not None:
        runner.suite_ref = f"{dataset.dataset_id}/{dataset.version}"
    try:
        base = controller.evaluator.run(dataset, baseline=True,
            metadata={"kind": "regression_baseline", "candidate": candidate.candidate_id})
        report = controller.evaluator.run(dataset, baseline=False,
            metadata={"kind": "regression", "candidate": candidate.candidate_id})
        return base, report
    finally:
        if previous is not None:
            runner.suite_ref = previous


def evaluate_regressions(controller, candidate, *, evaluated=None):
    controller.regression_builder.convert_pending()
    impact = candidate.impact_result or {}
    required = list(dict.fromkeys(impact.get("must_rerun_tests", [])))
    result = {"passed": True, "degraded": False, "reasons": [], "suites": [],
              "missing_suites": [], "required_suites": required,
              "scope": impact, "coverage": {}}
    result["reasons"].extend(impact.get("asset_scope_errors", []))
    # Old/persisted impact records may have an empty errors list despite an
    # unresolved scope. The status itself must also prevent release.
    if impact.get("asset_scope_status") in ("unavailable", "invalid") and not impact.get("asset_scope_errors"):
        result["reasons"].append("资产影响范围不可核验: " + impact["asset_scope_status"])
    if controller.experts is not None and not impact:
        result["reasons"].append("影响范围不可核验，禁止复用旧证据")
    domains = set(impact.get("required_domains", []))
    if candidate.target_domain:
        domains.add(candidate.target_domain)
    regression = controller.datasets.get("failures_regression")
    selected_domains = set(domains)
    if not selected_domains and candidate.target_component.startswith("expert/"):
        selected_domains.add(candidate.target_component.split("/")[1])
    if regression and regression.cases:
        cases = [c for c in regression.cases if not selected_domains or c.domain in selected_domains]
        if cases:
            subset = EvalDataset(dataset_id="failures_regression_" +
                ("_".join(sorted(selected_domains)) if selected_domains else "all"),
                version=regression.version, source=regression.dataset_id, cases=cases)
            required.append(subset.dataset_id)
        else:
            subset = None
    else:
        subset = None
    pairs = dict(evaluated or {})
    coverage_cases = {}
    for base, report in pairs.values():
        for row in report.case_results:
            coverage_cases[row.get("case")] = row
    for identity in dict.fromkeys(required):
        if identity == "builtin_safety":
            # Safety is executed on every evaluation, independently of the affected domain.
            if candidate.safety_result.get("passed") is not True:
                result["reasons"].append("必跑安全套件未通过")
            continue
        dataset = subset if subset and identity == subset.dataset_id else controller.datasets.get(identity)
        if dataset is None or not dataset.cases:
            result["missing_suites"].append(identity)
            result["reasons"].append("必跑回归套件缺失或为空: " + identity)
            continue
        try:
            problems = isolation_problems(dataset, controller.holdout_dataset)
            if problems:
                raise ValueError(problems[0])
            base, report = pairs.get(identity) or run_pair(controller, dataset, candidate)
            if base is None:
                raise ValueError("缺少基线")
            th = replace(controller.gate_thresholds, require_holdout=False)
            gate = ReleaseGate().judge(base, report, thresholds=th,
                safety_ok=candidate.safety_result.get("passed"), rollback_available=bool(candidate.rollback_version))
            degraded = report.success_rate < base.success_rate
            if degraded:
                result["degraded"] = True
                gate["reasons"].append("回归子集退化: " + identity)
            result["reasons"].extend(identity + ": " + reason for reason in gate["reasons"])
            result["suites"].append({"dataset": identity, "baseline": base.summary(),
                "candidate": report.summary(), "passed": not gate["reasons"],
                "baseline_cases": base.case_results, "candidate_cases": report.case_results,
                "reasons": gate["reasons"]})
            for row in report.case_results:
                coverage_cases[row.get("case")] = row
            if subset and identity == subset.dataset_id:
                result.update(dataset=identity, case_count=report.case_count,
                    baseline_success_rate=round(base.success_rate, 3),
                    candidate_success_rate=round(report.success_rate, 3))
        except Exception as exc:
            result["reasons"].append(identity + ": 回归未完成: " + type(exc).__name__)
            result["suites"].append({"dataset": identity, "passed": False, "error": type(exc).__name__})
    for domain in sorted(domains):
        count = sum(row.get("domain") == domain for row in coverage_cases.values())
        result["coverage"][domain] = count
        if count < controller.gate_thresholds.min_cases_per_domain:
            result["reasons"].append(f"领域样本不足: {domain} ({count})")
    result["passed"] = not result["reasons"]
    if result["reasons"]:
        result["reason"] = "; ".join(result["reasons"])
    return result
