"""候选宿主消费与问题队列管理 API（ALN-09-I）。

受控入口：只有通过摘要/版本校验的真实候选制品可被执行；评测结果绑定
baseline/candidate/dataset/宿主参数，且**不自动发布**。队列管理只做只读
视图与过期租约恢复，不绕过进化控制器的领取/完成消费。
"""
from .routes import get, post
from ..evolution.host import CandidateAssetError
from ..evaluation.host_bench import BenchError
from ..integration.candidate_execution import (CandidateExecutionError,
                                               CandidateEvaluationService)


def _admin(ctx):
    return getattr(getattr(ctx, "auth", None), "role", "") == "admin"


def _principal(ctx):
    return getattr(getattr(ctx, "auth", None), "principal_id", "") or "api:admin"


def _service(app) -> CandidateEvaluationService:
    service = getattr(app, "candidate_execution", None)
    if service is None:
        raise CandidateExecutionError("候选评测服务未装配")
    return service


def _error(ctx, exc):
    if isinstance(exc, (CandidateAssetError, BenchError)):
        return ctx.json(409, {"error": str(exc)})
    return ctx.json(400, {"error": str(exc)})


@get("/evolution/candidate-host/queue")
def candidate_queue(ctx):
    if not _admin(ctx):
        return ctx.json(403, {"error": "候选队列管理需要 admin 角色"})
    try:
        return ctx.json(200, _service(ctx.app).queue_overview(
            limit=int(ctx.query.get("limit", 50))))
    except (CandidateExecutionError, ValueError) as exc:
        return _error(ctx, exc)


@post("/evolution/candidate-host/queue/recover")
def recover_candidate_queue(ctx):
    if not _admin(ctx):
        return ctx.json(403, {"error": "候选队列恢复需要 admin 角色"})
    body = ctx.body if isinstance(ctx.body, dict) else {}
    try:
        return ctx.json(200, _service(ctx.app).queue_recover(
            actor=_principal(ctx),
            reason=str(body.get("reason") or "手动恢复过期租约")))
    except (CandidateExecutionError, ValueError) as exc:
        return _error(ctx, exc)


@post("/evolution/candidate-host/evaluate")
def evaluate_candidate_on_host(ctx):
    if not _admin(ctx):
        return ctx.json(403, {"error": "候选宿主评测需要 admin 角色"})
    body = ctx.body
    if not isinstance(body, dict):
        return ctx.json(400, {"error": "请求体必须是对象"})
    candidate_id = str(body.get("candidate_id") or "").strip()
    if not candidate_id:
        return ctx.json(400, {"error": "缺少 candidate_id"})
    if ctx.app.evolution.get_candidate(candidate_id) is None:
        return ctx.json(404, {"error": "候选不存在: " + candidate_id})
    try:
        result = _service(ctx.app).evaluate(
            candidate_id=candidate_id,
            baseline_ref=body.get("baseline_ref"),
            candidate_ref=body.get("candidate_ref"),
            cases=body.get("cases"),
            dataset_ref=str(body.get("dataset_ref") or ""),
            actor=_principal(ctx),
            reason=str(body.get("reason") or "受控候选评测"))
    except (CandidateExecutionError, CandidateAssetError, BenchError) as exc:
        return _error(ctx, exc)
    return ctx.json(200, result)
