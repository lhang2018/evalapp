"""Bind a durable evaluation decision to the candidate approved for release.

Hashes detect stale or changed evidence; they are not an authorization identity
or proof that a candidate artifact ran in an isolated host.
"""
import hashlib
import json
from dataclasses import asdict


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def candidate_spec(candidate):
    return {key: getattr(candidate, key) for key in (
        "candidate_id", "baseline_version", "target_component", "target_problem",
        "root_cause_evidence", "hypothesis", "change_scope", "eval_dataset_id",
        "target_domain", "risk_level", "compatible_requirements", "rollback_version",
        "impact_result")}


def candidate_digest(candidate):
    return digest({"spec": candidate_spec(candidate), "quality": candidate.quality_result,
        "holdout": candidate.holdout_result, "safety": candidate.safety_result,
        "benefit": candidate.benefit_result, "cost_delta": candidate.cost_delta,
        "latency_delta": candidate.latency_delta})


def context_digest(controller):
    return digest({"thresholds": asdict(controller.gate_thresholds),
        "datasets": {key: asdict(ds) for key, ds in controller.datasets.items()},
        "holdout": asdict(controller.holdout_dataset) if controller.holdout_dataset else None})


def save_evaluation(controller, candidate, report_id, dataset_id, payload):
    """Only advertise the receipt after its complete payload is durably saved."""
    candidate.evaluation_report_id = ""
    candidate.evaluation_report_digest = ""
    candidate.approval_result = None
    if controller.store is None:
        return
    payload["binding"] = {"candidate_digest": candidate_digest(candidate),
                          "context_digest": context_digest(controller)}
    checksum = digest(payload)
    controller.store.save_eval_report(report_id, dataset_id, candidate.candidate_id, payload)
    candidate.evaluation_report_id = report_id
    candidate.evaluation_report_digest = checksum


def verified_evaluation(controller, candidate):
    """Return the exact saved report, never a caller-supplied gate boolean."""
    if controller.store is None or not candidate.evaluation_report_id:
        raise ValueError("缺少持久化评测报告，请重新评测")
    row = controller.store.get_eval_report(candidate.evaluation_report_id)
    if row is None or row["candidate_id"] != candidate.candidate_id:
        raise ValueError("评测报告不存在或不属于当前候选")
    payload = row["data"]
    if digest(payload) != candidate.evaluation_report_digest:
        raise ValueError("评测报告摘要不符")
    binding = payload.get("binding", {})
    if binding.get("candidate_digest") != candidate_digest(candidate):
        raise ValueError("候选内容已改变，请重新评测和批准")
    if binding.get("context_digest") != context_digest(controller):
        raise ValueError("评测集或门禁阈值已改变，请重新评测")
    current_impact = controller._impact_for(candidate.target_component)
    if controller.experts is not None:
        if current_impact is None:
            raise ValueError("无法复核当前影响范围")
        for key in ("must_rerun_tests", "required_domains"):
            current_impact[key] = sorted(set(current_impact.get(key, [])) |
                set((candidate.impact_result or {}).get(key, [])))
        if digest(current_impact) != digest(candidate.impact_result):
            raise ValueError("资产或依赖影响范围已改变，请重新评测")
    gate = payload.get("gate", {})
    if (gate.get("passed") is not True or
            gate.get("independent_gate", {}).get("passed") is not True or
            candidate.publish_suggestion != "release_ready"):
        raise ValueError("评测门禁未通过或没有独立发布结论")
    return row
