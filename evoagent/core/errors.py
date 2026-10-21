"""统一错误对象（契约 §10，由 F-01 落地）。

契约 09 卷 §10：错误返回至少包含 `error_code` / `message` / `retryable` /
`correlation_id` / `related_event_id` / `required_action` / `safe_to_repeat`
七字段；**`safe_to_repeat=unknown` 时必须先对账或人工确认**，禁止自动重试。

**字段语义（实现定义，契约未细化的部分）**：
- `error_code`：稳定蛇形码；缺省由 HTTP 状态映射，见 `DEFAULT_SEMANTICS`。
- `retryable`：立即重发同样的请求能否出现不同结果（罕见抖动类）。
- `correlation_id`：响应级关联 ID（uuid）；当前仅用于排障串联，
  未来升级为 run/event 追溯 ID。
- `related_event_id`：错误关联的事件 ID；调用方已知时显式提供，缺省 None。
- `required_action`：取值 contracts.REQUIRED_ACTIONS。
- `safe_to_repeat`：取值 contracts.SAFE_TO_REPEAT。**按副作用安全性判**：
  请求未执行 → yes（重复无害）；副作用结果未知 → unknown（须先对账）。

不变量：`safe_to_repeat="unknown"` 时 `required_action` 必须为 `reconcile`。
"""

import uuid
from typing import Any, Dict, Optional

from .contracts import ERROR_FIELDS, REQUIRED_ACTIONS, SAFE_TO_REPEAT

__all__ = ["ApiError", "error_payload", "normalize_error_payload"]


# HTTP 状态 →（error_code, retryable, required_action, safe_to_repeat）缺省。
# 原则：**不确定就 unknown**——unknown 只增加一次对账成本，误报 yes 可能
# 造成副作用蚕食；本表只对「确定未执行」的状态给 yes。
_DEFAULT_TABLE: Dict[int, Dict[str, Any]] = {
    400: {"error_code": "invalid_request", "retryable": False,
          "required_action": "fix_request", "safe_to_repeat": "yes"},
    401: {"error_code": "auth_failed", "retryable": False,
          "required_action": "none", "safe_to_repeat": "yes"},
    403: {"error_code": "forbidden", "retryable": False,
          "required_action": "none", "safe_to_repeat": "yes"},
    404: {"error_code": "not_found", "retryable": False,
          "required_action": "fix_request", "safe_to_repeat": "yes"},
    409: {"error_code": "conflict", "retryable": False,
          "required_action": "reconcile", "safe_to_repeat": "unknown"},
    413: {"error_code": "payload_too_large", "retryable": False,
          "required_action": "fix_request", "safe_to_repeat": "yes"},
    415: {"error_code": "unsupported_media_type", "retryable": False,
          "required_action": "fix_request", "safe_to_repeat": "yes"},
}

#: 任何未listed状态的兜底（含 5xx）：一律按未知处理。
_FALLBACK: Dict[str, Any] = {
    "error_code": "internal_error", "retryable": False,
    "required_action": "reconcile", "safe_to_repeat": "unknown",
}

#: 调用方可能显式给出的语义键（其余 payload 键原样透传）。
_SEMANTIC_KEYS = frozenset(
    {"error_code", "retryable", "required_action", "safe_to_repeat",
     "correlation_id", "related_event_id"})


def error_payload(status: int, message: str, *,
                  correlation_id: Optional[str] = None) -> Dict[str, Any]:
    """按契约 §10 构造七字段错误对象（含向后兼容的 `error` 键）。"""
    if correlation_id is None:
        correlation_id = uuid.uuid4().hex
    base = _DEFAULT_TABLE.get(status, dict(_FALLBACK))
    return {
        "error_code": base["error_code"],
        "message": message,
        "retryable": base["retryable"],
        "correlation_id": correlation_id,
        "related_event_id": None,
        "required_action": base["required_action"],
        "safe_to_repeat": base["safe_to_repeat"],
        # 向后兼容：存量客户端/测试断言 {"error": ...}
        "error": message,
    }


def normalize_error_payload(status: int, payload: Dict[str, Any],
                            ) -> Dict[str, Any]:
    """把裸 `{"error": ...}` 补齐为七字段对象并保留调用方附加键。

    调用方在 payload 里显式给出的语义键**优先**于缺省表；其余键
    （如 `detail`）原样透传。错误码/状态里的非法值不会被静默接受——
    越界值按缺省处理，防止调用方误传破坏契约不变量。
    """
    message = str(payload.get("error")
                  or payload.get("message")
                  or payload.get("detail")
                  or payload)
    out = error_payload(status, message)
    extras: Dict[str, Any] = {}
    for key, value in payload.items():
        # 语义键不入 extras：只在下方校验通过后才采纳，非法值丢弃
        if key in ("error", "message") or key in _SEMANTIC_KEYS:
            continue
        extras[key] = value
    for key in sorted(_SEMANTIC_KEYS & set(payload)):
        value = payload[key]
        if key == "error_code" and isinstance(value, str) and value:
            out["error_code"] = value
        elif key == "retryable" and isinstance(value, bool):
            out["retryable"] = value
        elif key == "required_action" and value in REQUIRED_ACTIONS:
            out["required_action"] = value
        elif key == "safe_to_repeat" and value in SAFE_TO_REPEAT:
            out["safe_to_repeat"] = value
        elif key == "correlation_id" and isinstance(value, str) and value:
            out["correlation_id"] = value
        elif key == "related_event_id":
            out["related_event_id"] = value
    # 契约不变量（最后执行）：unknown ⇒ 必须对账/人工确认
    if out["safe_to_repeat"] == "unknown" and \
            out["required_action"] not in ("reconcile", "human_confirm"):
        out["required_action"] = "reconcile"
    out.update(extras)
    return out


class ApiError(Exception):
    """可抛出的契约错误；一致地携带七字段语义。

    用法（未来逐步替换 handler 中的裸 `self._json(4xx, {...})`）：

        raise ApiError(409, "存在未结束任务", safe_to_repeat="unknown",
                       required_action="reconcile")
    """

    def __init__(self, status: int, message: str, *,
                 error_code: Optional[str] = None,
                 retryable: Optional[bool] = None,
                 required_action: Optional[str] = None,
                 safe_to_repeat: Optional[str] = None,
                 related_event_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.error_code = error_code or ""
        self.retryable = retryable
        self.required_action = required_action
        self.safe_to_repeat = safe_to_repeat
        self.related_event_id = related_event_id

    def payload(self) -> Dict[str, Any]:
        out = error_payload(self.status, self.message)
        if self.error_code:
            out["error_code"] = self.error_code
        if self.retryable is not None:
            out["retryable"] = self.retryable
        if self.required_action is not None:
            out["required_action"] = self.required_action
        if self.safe_to_repeat is not None:
            out["safe_to_repeat"] = self.safe_to_repeat
        if self.related_event_id is not None:
            out["related_event_id"] = self.related_event_id
        if out["safe_to_repeat"] == "unknown" and \
                out["required_action"] not in ("reconcile", "human_confirm"):
            out["required_action"] = "reconcile"
        return out
