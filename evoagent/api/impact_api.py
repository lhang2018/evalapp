"""能力影响分析查询接口（GAP 子步④·真实调用方②）。注册表路由。"""

from .routes import get


@get("/impact")
@get("/impact/")
def capability_impact(ctx):
    """GET /impact/?capability_id=... —— 传递闭包 + 回归范围（需 admin）。"""
    if ctx.auth.role != "admin":
        ctx.json(403, {"error": "该操作需要 admin 权限"})
        return
    capability_id = str(ctx.query.get("capability_id", ""))
    if not capability_id:
        ctx.json(400, {"error": "缺少 capability_id"})
        return
    try:
        ctx.json(200, {"impact":
                       ctx.app.experts.impact_analysis(capability_id)})
    except Exception as e:  # noqa: BLE001
        ctx.json(500, {"error": f"影响分析失败: {e}"})
