"""能力状态端点（ALN-24-B）：能力六档状态与原因投影。

口径（任务卡 24-B）：避免"配置存在即显示可用"。每个能力（工具）复用
既有权威数据投影为六档之一并给出原因，不引入第二权威：

  unregistered        未注册（被策略引用但注册表没有）
  denied              已拒绝/撤销（显式 DENY 策略，或 deny 模式拦下；
                      拒绝后受保护动作不能执行由既有 check_tool 保证）
  permission_required 待授权/待确认（CONFIRM 策略，或高风险链在 confirm
                      模式下等用户确认——执行时会走审批/确认）
  dependency_missing  依赖缺失（健康探针 False，fail-closed）
  unverified          探针未声明（外部类服务，如实展示 unknown）
  ready               可用（健康探针 True）

判定复用 PermissionController.check_tool 干跑（纯策略裁决，无副作用）；
health 只读 ToolRegistry.health()（F-04 三值语义）。
"""

from .routes import get
from ..core.permissions import PermissionDecision


def _dry_decision(permissions, tool_id: str, side_effect: str):
    """干跑既有权限裁决链；返回 PermissionDecision。"""
    try:
        return permissions.check_tool(tool_id, {}, risk=None,
                                      side_effect=side_effect)
    except TypeError:
        # risk 为必填位置参数的旧签名兜底
        from ..core.task import RiskLevel
        return permissions.check_tool(tool_id, {}, risk=RiskLevel.LOW,
                                      side_effect=side_effect)


def project_capability(tool_id: str, *, registered: bool, version: str,
                       health, dry, explicit_deny: bool,
                       high_risk_mode: str, note: str,
                       high_risk: bool) -> dict:
    if not registered:
        state, reason = "unregistered", "未在注册表注册，不能执行"
    elif explicit_deny:
        state, reason = "denied", note or "策略已拒绝；受保护动作不会执行"
    elif dry == PermissionDecision.DENY:
        if high_risk_mode == "deny":
            state, reason = "denied", "运行于 deny 模式；受保护动作不会执行"
        else:
            state, reason = ("permission_required",
                             "需要用户确认或授权后才能执行（当前未授权）")
    elif dry == PermissionDecision.CONFIRM:
        state, reason = "permission_required", note or "需要用户确认后放行"
    elif health is False:
        state, reason = "dependency_missing", "健康探针失败（fail-closed）"
    elif health is None:
        state, reason = "unverified", "未声明健康探针（外部类服务，如实未知）"
    else:
        state, reason = "ready", "健康探针通过"
    return {"tool_id": tool_id, "version": version, "state": state,
            "reason": reason, "note": note or None, "high_risk": high_risk}


def collect(ctx) -> dict:
    app = ctx.app
    registry = app.tools
    permissions = app.permissions
    health = registry.health()
    policies = getattr(permissions, "_tool_policies", {})
    tool_ids = {e["tool_id"] for e in registry.version_inventory()}
    capabilities = []
    for tool_id in sorted(tool_ids | set(policies)):
        rt = registry._tools.get(tool_id)
        registered = rt is not None
        version = rt.contract.tool_version if registered else ""
        side_effect = (rt.contract.side_effect_level
                       if registered else "none")
        policy = policies.get(tool_id)
        note = policy.note if policy else ""
        explicit_deny = bool(policy and
                             policy.decision == PermissionDecision.DENY)
        dry = _dry_decision(permissions, tool_id, side_effect)
        capabilities.append(project_capability(
            tool_id, registered=registered, version=version,
            health=health.get(tool_id) if registered else None,
            dry=dry, explicit_deny=explicit_deny,
            high_risk_mode=permissions.high_risk_mode,
            note=note,
            high_risk=tool_id in getattr(permissions, "high_risk_ops",
                                         set())))
    order = {"unregistered": 0, "denied": 1, "permission_required": 2,
             "dependency_missing": 3, "unverified": 4, "ready": 5}
    capabilities.sort(key=lambda c: (order.get(c["state"], 9),
                                     c["tool_id"]))
    counts: dict = {}
    for c in capabilities:
        counts[c["state"]] = counts.get(c["state"], 0) + 1
    return {"capabilities": capabilities, "counts": counts}


@get("/capabilities/status")
def capability_status(ctx):
    """能力六档状态与原因（ALN-24-B；只读投影）。"""
    return ctx.json(200, collect(ctx))
