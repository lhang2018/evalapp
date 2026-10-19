"""权限与高风险操作控制（Architecture 18.1 / PRD 83-84）。

- 最小权限：模块只能访问完成职责必需的数据与工具。
- 高风险操作确认由独立权限系统执行，不依赖模型提示词。
- Android 场景下系统实际授权为准（此处为服务端策略层）。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional

from .task import RiskLevel


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"   # 需要用户确认后放行


@dataclass
class ToolPolicy:
    """对单个工具的权限声明。"""
    tool_id: str
    decision: PermissionDecision = PermissionDecision.ALLOW
    min_risk: RiskLevel = RiskLevel.LOW
    note: str = ""


class PermissionController:
    def __init__(self, high_risk_mode: str = "confirm",
                 confirm_callback: Optional[Callable[[str, Dict], bool]] = None):
        """
        high_risk_mode: 记录 in deny | confirm | allow（仅测试环境用 allow）
        confirm_callback: (op_name, payload) -> bool；默认无回调=拒绝
        """
        self.high_risk_mode = high_risk_mode
        self.confirm_callback = confirm_callback
        self._tool_policies: Dict[str, ToolPolicy] = {}
        # 内置高风险操作名单（PRD 84：支付、删除、外部发送、账户修改、权限变更）
        self.high_risk_ops = {
            "send_message", "delete", "pay", "modify_account",
            "change_permission", "deploy_release", "publish_external",
        }

    def register_tool_policy(self, policy: ToolPolicy) -> None:
        self._tool_policies[policy.tool_id] = policy

    def check_tool(self, tool_id: str, args: Dict, risk: RiskLevel,
                   side_effect: str = "none") -> PermissionDecision:
        """工具调用前权限校验。裁决链：显式策略 > 副作用登记 > 高风险名单。"""
        policy = self._tool_policies.get(tool_id)
        if policy:
            if policy.decision == PermissionDecision.DENY:
                return PermissionDecision.DENY
            if policy.decision == PermissionDecision.ALLOW:
                return PermissionDecision.ALLOW
        if tool_id in self.high_risk_ops \
                or (policy and policy.decision == PermissionDecision.CONFIRM) \
                or side_effect in ("local", "destructive"):
            return self._decide_high_risk(tool_id, args)
        return PermissionDecision.ALLOW

    def check_op(self, op_name: str, args: Dict) -> PermissionDecision:
        if op_name in self.high_risk_ops:
            return self._decide_high_risk(op_name, args)
        return PermissionDecision.ALLOW

    def _decide_high_risk(self, op: str, args: Dict) -> PermissionDecision:
        if self.high_risk_mode == "allow":
            return PermissionDecision.ALLOW
        if self.high_risk_mode == "deny":
            return PermissionDecision.DENY
        # confirm 模式：有回调且回调同意才放行
        if self.confirm_callback and self.confirm_callback(op, args):
            return PermissionDecision.ALLOW
        return PermissionDecision.DENY
