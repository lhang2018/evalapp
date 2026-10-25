"""用户收益说明端点（ALN-29-D，注册表路由）。

把候选/评测/发布/观测四阶段证据翻译成用户可核验的说明；不新增第二套
评测或监测逻辑，也不把基准结论冒充线上收益。
"""
from .routes import get
from ..evolution.benefit_statement import build_user_benefit_statement


@get("/evolution/candidates/{candidate_id}/benefit-statement")
def benefit_statement(ctx):
    candidate_id = ctx.path.split("/")[3]
    candidate = ctx.app.evolution.get_candidate(candidate_id)
    if candidate is None:
        return ctx.json(404, {"error": "候选不存在: " + candidate_id})
    statement = build_user_benefit_statement(candidate)
    return ctx.json(200, statement)
