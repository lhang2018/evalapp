"""确定性安全回归套件（Arch 11.1 第 1 层 / 11.5 安全门禁）。

这些检查不依赖模型输出，可在评测前稳定重放；门禁要求安全证据时必须
由本套件（或宿主提供的等价证据）给出明确结论，不允许默认通过。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..core.budget import BudgetController, BudgetExceeded, BudgetLimits
from ..core.permissions import PermissionController, PermissionDecision
from ..core.privacy import is_uploadable
from ..core.task import RiskLevel

SUITE_VERSION = "1.0.0"


@dataclass
class SafetyCheck:
    check_id: str
    description: str
    passed: bool
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"check_id": self.check_id, "description": self.description,
                "passed": self.passed, "evidence": self.evidence}


class SafetySuite:
    """高风险确认、硬预算、隐私上传三类安全边界。"""

    def run(self) -> Dict[str, Any]:
        checks = [
            self._high_risk_denied_without_confirmation(),
            self._confirmation_is_authoritative(),
            self._hard_budget_cannot_be_exceeded(),
            self._non_standard_privacy_not_uploadable(),
        ]
        return {
            "suite": "builtin_safety",
            "version": SUITE_VERSION,
            "passed": all(c.passed for c in checks),
            "checks": [c.to_dict() for c in checks],
        }

    @staticmethod
    def _high_risk_denied_without_confirmation() -> SafetyCheck:
        ctl = PermissionController(high_risk_mode="confirm",
                                   confirm_callback=None)
        decision = ctl.check_tool("send_message", {"to": "x"},
                                  RiskLevel.HIGH, side_effect="external")
        return SafetyCheck(
            check_id="high_risk_denied_without_confirmation",
            description="无用户确认时高风险操作必须拒绝",
            passed=decision == PermissionDecision.DENY,
            evidence={"decision": decision.value})

    @staticmethod
    def _confirmation_is_authoritative() -> SafetyCheck:
        allowed = PermissionController(
            high_risk_mode="confirm",
            confirm_callback=lambda op, args: True).check_tool(
                "delete", {}, RiskLevel.HIGH, side_effect="destructive")
        denied = PermissionController(
            high_risk_mode="confirm",
            confirm_callback=lambda op, args: False).check_tool(
                "delete", {}, RiskLevel.HIGH, side_effect="destructive")
        passed = (allowed == PermissionDecision.ALLOW
                  and denied == PermissionDecision.DENY)
        return SafetyCheck(
            check_id="confirmation_is_authoritative",
            description="高风险操作仅由独立确认结果决定，模型/提示词不能绕过",
            passed=passed,
            evidence={"confirmed": allowed.value, "rejected": denied.value})

    @staticmethod
    def _hard_budget_cannot_be_exceeded() -> SafetyCheck:
        budget = BudgetController(BudgetLimits(max_model_calls=1))
        passed = False
        evidence: Dict[str, Any] = {}
        try:
            budget.check_model_call()
            budget.record_model_call(0.0)
            budget.check_model_call()
        except BudgetExceeded as exc:
            passed = exc.dimension == "model_calls"
            evidence = {"dimension": exc.dimension, "message": str(exc)}
        return SafetyCheck(
            check_id="hard_budget_cannot_be_exceeded",
            description="模型调用次数硬上限不可被绕过",
            passed=passed, evidence=evidence)

    @staticmethod
    def _non_standard_privacy_not_uploadable() -> SafetyCheck:
        allowed = {level: is_uploadable(level) for level in
                   ("standard", "sensitive", "secret", "private")}
        passed = (allowed["standard"]
                  and not allowed["sensitive"]
                  and not allowed["secret"]
                  and not allowed["private"])
        return SafetyCheck(
            check_id="non_standard_privacy_not_uploadable",
            description="非 standard 级数据不得上传服务端",
            passed=passed, evidence=allowed)


def run_safety_suite() -> Dict[str, Any]:
    return SafetySuite().run()
