"""用户可核验的「改进效果与用户收益」说明（ALN-29-D）。

把候选生命周期里的三类证据翻译成人话，并强制区分证据阶段：

- **候选阶段**（candidate）：只有提案与假设 → 只能说「已提出、待评测」；
- **评测阶段**（pre_release）：有对照评测/收益结果 → 只能说「在基准上与基线对照如何」，
  必须带证据引用（评测报告 id / 证据摘要），且**基准收益不等于线上收益**；
- **发布阶段**（release）：有发布身份/部署记录 → 说清发布到哪个版本/范围；
- **观测阶段**（observation）：有发布后监测 → 才能提「线上效果」，并附样本窗口；
  缺监测时必须明确写「发布后效果待观测」，**不得声称线上收益**。

硬规则（fail-closed）：
1. 没有对照评测证据 → 不产生任何正向收益表述（`cannot_claim` 里写明原因）；
2. 评测/监测退化、回滚建议、安全事件 → 全部进 `degradations`，并在摘要中可见；
3. 每条 `claims` 携带 `basis` 与 `evidence_refs`，可回查；无证据的表述只能进
   `cannot_claim`，不允许出现在 `claims` 里。

本模块只读消费既有结果（`EvolutionCandidate`/评测报告/监测结果，及 ALN-10-E
的宿主基准报告），不重写评测或监测实现。
"""

from typing import Any, Dict, List, Optional

STAGE_LABELS = {
    "candidate": "候选已提出",
    "pre_release": "发布前对照评测",
    "release": "已发布",
    "observation": "发布后观测",
}


def _digest(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("evidence_digest", "digest", "report_id", "evidence_id"):
            text = value.get(key)
            if isinstance(text, str) and text:
                return text
    if isinstance(value, str) and value:
        return value
    return None


def _dims(benefit: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(benefit, dict):
        return {}
    dims = benefit.get("dimensions")
    return dims if isinstance(dims, dict) else {}


def build_user_benefit_statement(candidate: Any, *,
                                 benchmark_report: Optional[Dict[str, Any]] = None,
                                 release: Optional[Dict[str, Any]] = None,
                                 monitor: Optional[Dict[str, Any]] = None,
                                 product_name: str = "EvoAgent") -> Dict[str, Any]:
    """生成分阶段、可核验的用户收益说明（不夸大、不掩盖退化）。"""
    stages: Dict[str, Any] = {}
    claims: List[Dict[str, Any]] = []
    cannot_claim: List[Dict[str, str]] = []
    degradations: List[Dict[str, Any]] = []
    limitations: List[str] = []

    candidate_id = str(getattr(candidate, "candidate_id", "") or
                       (candidate or {}).get("candidate_id") or "")
    status = str(getattr(getattr(candidate, "status", None), "value",
                         getattr(candidate, "status", "")) or "")
    target = str(getattr(candidate, "target_component", "") or "")
    problem = str(getattr(candidate, "target_problem", "") or "")
    stages["candidate"] = {"candidate_id": candidate_id, "status": status,
                           "target_component": target,
                           "target_problem": problem,
                           "label": STAGE_LABELS["candidate"]}
    claims.append({"stage": "candidate",
                   "text": f"已提出针对「{target or '未知组件'}」的改进候选"
                           + (f"（问题：{problem}）" if problem else ""),
                   "basis": "candidate", "verifiable": False,
                   "evidence_refs": [candidate_id] if candidate_id else []})

    benefit = getattr(candidate, "benefit_result", None)
    quality = getattr(candidate, "quality_result", None)
    report_id = str(getattr(candidate, "evaluation_report_id", "") or "")
    eval_refs = [ref for ref in (report_id, _digest(quality),
                                 _digest(benchmark_report)) if ref]
    evaluated = bool(benefit or quality or benchmark_report)
    if evaluated:
        dims = _dims(benefit)
        verdict = (benchmark_report or {}).get("verdict")
        metrics = (benchmark_report or {}).get("metrics") or {}
        reproducible = (benchmark_report or {}).get("reproducible")
        stages["pre_release"] = {
            "label": STAGE_LABELS["pre_release"], "report_id": report_id,
            "verdict": verdict, "metrics": metrics,
            "reproducible": reproducible,
            "dimensions": {name: dims.get(name) for name in dims} if dims else None}
        if benchmark_report is not None:
            if reproducible is False:
                # fail-closed：不可复现的对照不能支撑任何正向收益表述
                cannot_claim.append({"stage": "pre_release",
                                     "reason": "基准对照不可复现，不能据此声称收益"})
            elif verdict == "improved":
                claims.append({"stage": "pre_release",
                               "text": "在本地基准对照中优于基线（基准范围，不等于线上收益）",
                               "basis": "benchmark", "verifiable": True,
                               "evidence_refs": eval_refs})
            else:
                cannot_claim.append({
                    "stage": "pre_release",
                    "reason": f"基准结论为 {verdict}，不能声称改进"})
                if verdict == "regressed":
                    degradations.append({
                        "stage": "pre_release", "kind": "benchmark_regression",
                        "detail": "基准对照中候选出现退化",
                        "evidence_refs": eval_refs})
        elif dims:
            claims.append({"stage": "pre_release",
                           "text": "已完成发布前评测（十维收益向量见证据）",
                           "basis": "evaluation", "verifiable": True,
                           "evidence_refs": eval_refs})
        for name, dim in (dims or {}).items():
            if isinstance(dim, dict) and dim.get("degraded"):
                degradations.append({"stage": "pre_release",
                                     "kind": "dimension_degraded",
                                     "detail": f"维度 {name} 退化",
                                     "evidence_refs": eval_refs})
    else:
        cannot_claim.append({"stage": "pre_release",
                             "reason": "没有对照评测证据，不能声称效果改进"})

    deployed_release = str(getattr(candidate, "deployed_release_id", "") or "")
    if deployed_release or release:
        stages["release"] = {"label": STAGE_LABELS["release"],
                             "release_id": (release or {}).get("release_id",
                                                              deployed_release),
                             "release_refs": (release or {}).get("refs")}
        claims.append({"stage": "release",
                       "text": f"改进已包含在发布 {(release or {}).get('release_id', deployed_release)} 中",
                       "basis": "release", "verifiable": True,
                       "evidence_refs": [deployed_release or
                                         (release or {}).get("release_id", "")]})
        limitations.append("已发布不等于线上有效：需发布后观测数据才能说明用户侧效果")

    observed = monitor or getattr(candidate, "monitor_result", None)
    if observed:
        decision = str(observed.get("decision") or "")
        metrics = observed.get("metrics") or {}
        stages["observation"] = {"label": STAGE_LABELS["observation"],
                                 "decision": decision, "metrics": metrics,
                                 "window": observed.get("window"),
                                 "sample_size": observed.get("sample_size")}
        if decision in ("rollback", "rollback_recommended"):
            degradations.append({"stage": "observation",
                                 "kind": "post_release_regression",
                                 "detail": "发布后监测建议回滚",
                                 "reasons": observed.get("reasons") or []})
            cannot_claim.append({"stage": "observation",
                                 "reason": "发布后监测显示退化/需回滚，不能声称线上收益"})
        else:
            claims.append({"stage": "observation",
                           "text": "发布后监测窗口内未发现触发回滚的退化"
                                   + (f"（决策：{decision}）" if decision else ""),
                           "basis": "monitor", "verifiable": True,
                           "evidence_refs": [ref for ref in (
                               _digest(observed), str(observed.get("monitor_id") or ""))
                               if ref]})
        for warning in observed.get("warnings") or []:
            limitations.append(str(warning))
    else:
        cannot_claim.append({"stage": "observation",
                             "reason": "缺少发布后监测证据，不能声称线上收益"})
        limitations.append("发布后效果待观测")

    if getattr(candidate, "rollback_version", ""):
        limitations.append(
            f"如出现退化可回滚到 {getattr(candidate, 'rollback_version')}")
    if not claims:
        cannot_claim.append({"stage": "candidate", "reason": "证据不足，无法生成说明"})
    scopes = {item["basis"] for item in claims}
    scope = ("release-monitored" if "monitor" in scopes else
             "benchmark-only" if "benchmark" in scopes else
             "evaluation-only" if "evaluation" in scopes else "candidate-only")
    summary = {
        "candidate-only": "该改进目前只是候选，尚无可核验的效果证据。",
        "evaluation-only": "该改进已完成发布前评测；线上效果尚未观测。",
        "benchmark-only": "该改进在本地基准对照中有效；基准结论不等于线上收益。",
        "release-monitored": "该改进已发布并有监测窗口数据；结论以监测证据为准。",
    }[scope]
    return {"product": product_name,
            "candidate_id": candidate_id,
            "scope": scope,
            "stages": stages,
            "claims": claims,
            "cannot_claim": cannot_claim,
            "degradations": degradations,
            "limitations": list(dict.fromkeys(limitations)),
            "summary": summary,
            "rule": "无真实对照不声称收益；候选/评测/发布/观测四阶段分开表述"}
