"""工具统一契约与执行结果（Architecture 6.1-6.2 / PRD 14-15）。

核心设计：HTTP 200 / 退出码 0 / 模型说“成功”都不代表业务目标达成，
结果必须区分「提交成功 / 执行成功 / 契约校验 / 业务达成 / 独立验证」。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class ToolContract:
    tool_id: str
    description: str = ""
    tool_version: str = "1.0.0"
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    permission_requirements: list = field(default_factory=list)
    side_effect_level: str = "none"          # none | local | external | destructive
    timeout_policy: float = 10.0
    retry_policy: int = 1
    idempotency_policy: str = "none"         # none | key | state-check
    error_mapping: Dict[str, str] = field(default_factory=dict)
    health_check: Optional[Callable[[], bool]] = None


@dataclass
class ToolResult:
    ok: bool                                 # 阶段1：请求提交+执行是否成功
    data: Any = None
    error: Optional[str] = None
    error_class: Optional[str] = None
    # PRD 15 五层区分
    submitted: bool = False                  # 请求是否成功提交
    executed: bool = False                   # 工具是否成功执行
    contract_valid: bool = False             # 返回数据是否符合契约
    goal_reached: Optional[bool] = None      # 业务目标是否真正达到（可为未知）
    independently_verified: bool = False     # 是否经过独立验证
    side_effect_uncertain: bool = False      # 副作用已发生但未收到确认
    evidence: Dict[str, Any] = field(default_factory=dict)
    idempotency_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "data": self.data, "error": self.error,
            "error_class": self.error_class, "submitted": self.submitted,
            "executed": self.executed, "contract_valid": self.contract_valid,
            "goal_reached": self.goal_reached,
            "independently_verified": self.independently_verified,
            "side_effect_uncertain": self.side_effect_uncertain,
            "evidence": self.evidence, "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def error_result(cls, err: str, err_class: str = "execution_error") -> "ToolResult":
        return cls(ok=False, error=err, error_class=err_class,
                   submitted=False, executed=False, contract_valid=False,
                   independently_verified=False)


def class_error(exc: Exception) -> str:
    """错误分类（Architecture 6.3）：不同错误类型走不同恢复策略。"""
    import socket
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionError, socket.error)):
        return "network_or_unavailable"
    if isinstance(exc, PermissionError):
        return "auth_or_permission"
    if isinstance(exc, ValueError):
        return "invalid_arguments"
    if isinstance(exc, OSError):
        return "tool_defect_or_env"
    return "unknown"


def validate_against_schema(value: Any, schema: Dict[str, Any]) -> Optional[str]:
    """轻量 JSON-Schema 校验（仅支持 type/required/properties，够用且零依赖）。"""
    t = schema.get("type")
    type_map = {"object": dict, "array": list, "string": str, "number": (int, float),
                "integer": int, "boolean": bool, "null": type(None)}
    if t and not isinstance(value, type_map.get(t, object)):
        return f"类型不符: 期望 {t}, 实际 {type(value).__name__}"
    if isinstance(value, dict) and (schema.get("properties")
                                    or schema.get("required")):
        for req in schema.get("required", []):
            if req not in value:
                return f"缺少必填字段: {req}"
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                err = validate_against_schema(value[k], sub)
                if err:
                    return f"字段 {k}: {err}"
    return None
