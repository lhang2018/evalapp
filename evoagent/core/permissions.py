"""权限与高风险操作控制（Architecture 18.1 / PRD 83-84）。

- 最小权限：模块只能访问完成职责必需的数据与工具。
- 高风险操作确认由独立权限系统执行，不依赖模型提示词。
- Android 场景下系统实际授权为准（此处为服务端策略层）。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional
from copy import deepcopy

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
                 confirm_callback: Optional[Callable[[str, Dict], bool]] = None,
                 approvals: Optional[Any] = None):
        """
        high_risk_mode: 记录 in deny | confirm | allow（仅测试环境用 allow）
        confirm_callback: (op_name, payload) -> bool；无回调时持久任务进入异步审批，
                          无任务/无审批存储的调用继续拒绝
        approvals: 审批记录存取（core/approvals.ApprovalStore）；F-04 高风险
                   决策与一次性语义落账用，缺省None 保持旧行为
        """
        self.high_risk_mode = high_risk_mode
        self.confirm_callback = confirm_callback
        self.approvals: Optional[Any] = approvals
        self._tool_policies: Dict[str, ToolPolicy] = {}
        # 内置高风险操作名单（PRD 84：支付、删除、外部发送、账户修改、权限变更）
        self.high_risk_ops = {
            "send_message", "delete", "pay", "modify_account",
            "change_permission", "deploy_release", "publish_external",
            "workflow_node_confirm",
        }

    def register_tool_policy(self, policy: ToolPolicy) -> None:
        self._tool_policies[policy.tool_id] = policy

    def check_tool(self, tool_id: str, args: Dict, risk: RiskLevel,
                   side_effect: str = "none",
                   lifted: bool = False, task_id: str = "", *,
                   require_confirmation: bool = False,
                   authorization_context: Optional[Dict] = None,
                   run_id: str = "", confirmed: bool = False) -> PermissionDecision:
        """工具调用前权限校验。裁决链：显式策略 > 副作用登记 > 高风险名单。

        lifted（契约 §4，GAP ⑤b）：工具契约风险声明高于副作用推导时
        由注册表传入——显式声明可把工具拉入高风险确认链（fail-closed）。
        external/local/destructive 即使未显式声明风险，也必须确认。
        """
        policy = self._tool_policies.get(tool_id)
        if policy:
            if policy.decision == PermissionDecision.DENY:
                return PermissionDecision.DENY
        if tool_id in self.high_risk_ops \
                or (policy and policy.decision == PermissionDecision.CONFIRM) \
                or side_effect in ("local", "external", "destructive") \
                or lifted or require_confirmation:
            return self._decide_high_risk(tool_id, args, task_id=task_id,
                context=authorization_context, run_id=run_id, confirmed=confirmed)
        return PermissionDecision.ALLOW

    def check_op(self, op_name: str, args: Dict, *, task_id=None,
                 authorization_context=None, run_id="", confirmed=False) -> PermissionDecision:
        policy = self._tool_policies.get(op_name)
        if policy and policy.decision == PermissionDecision.DENY:
            return PermissionDecision.DENY
        if op_name in self.high_risk_ops:
            return self._decide_high_risk(
                op_name, args, task_id=task_id or str(args.get("task_id") or ""),
                context=authorization_context, run_id=run_id, confirmed=confirmed)
        return PermissionDecision.ALLOW

    def _decide_high_risk(self, op: str, args: Dict,
                          task_id: str = "", context=None, run_id="", confirmed=False) -> PermissionDecision:
        if self.high_risk_mode == "allow":
            decision = PermissionDecision.ALLOW
        elif self.high_risk_mode == "deny":
            decision = PermissionDecision.DENY
        else:
            if not confirmed and self.confirm_callback is None and self.approvals is not None and task_id:
                permitted = self.approvals.authorize_action(
                    op, args, task_id=task_id, context=context, run_id=run_id)
                return PermissionDecision.ALLOW if permitted else PermissionDecision.DENY
            # confirm 模式：有回调且回调同意才放行
            if confirmed or (self.confirm_callback and self.confirm_callback(op, deepcopy(args))):
                decision = PermissionDecision.ALLOW
            else:
                decision = PermissionDecision.DENY
        # F-04：高风险决策必须落审批记录（A-01 五项绑定 + 一次性语义）
        if self.approvals is not None:
            self.approvals.note_decision(
                op, args, granted_by=f"policy:{self.high_risk_mode}",
                task_id=task_id,
                approved=(decision == PermissionDecision.ALLOW))
        return decision
