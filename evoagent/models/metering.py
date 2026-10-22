"""模型调用计量（PRD 40 / Arch 20.1 / ALN-08）。

计量口径
--------
- **实测（provider）**：提供方响应里有 token 用量时，按档案价格计算；
- **预估（estimate）**：提供方未返回用量时，按字符数估算，明确标记为预估；
- **已知免费（free）**：仅当档案显式声明 `pricing_known=True` 且价格全为 0
  （如内置 Mock）；
- **未知（unknown）**：价格缺失时 **cost=None**，绝不按 0 元记账。

只观测调用结果、用量与时延；不改变适配器行为，也不把成功率当质量结论。
本模块不是预算权威（预算执行见 core/budget.py），只提供可对账的计量证据。
"""

import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.leases import CURRENT_LEASE
from .base import Action, ChatMessage, ModelProfile

# 用量来源
TOKEN_PROVIDER = "provider"
TOKEN_ESTIMATE = "estimate"
TOKEN_UNKNOWN = "unknown"

# 价格档案状态
PRICING_KNOWN = "known"
PRICING_FREE = "free"
PRICING_UNKNOWN = "unknown"

# 成本来源
COST_PROVIDER = "provider"
COST_ESTIMATE = "estimate"
COST_FREE = "free"
COST_UNKNOWN = "unknown"

#: 粗略字符→token 估算；只在提供方未返回真实用量时使用
CHARS_PER_TOKEN = 4


def pricing_status(profile: Optional[ModelProfile]) -> str:
    """档案价格状态：known / free / unknown。

    `pricing_known=False` 表示价格缺失，不能据此推断免费。
    """
    if profile is None or not bool(getattr(profile, "pricing_known", False)):
        return PRICING_UNKNOWN
    price_in = float(getattr(profile, "price_per_1k_input", 0.0) or 0.0)
    price_out = float(getattr(profile, "price_per_1k_output", 0.0) or 0.0)
    if price_in == 0.0 and price_out == 0.0:
        return PRICING_FREE
    return PRICING_KNOWN


def tokens_from_chars(chars: int) -> int:
    """字符数→token 估算（向上取整，0 字符为 0）。"""
    return max(0, int(math.ceil(max(0, int(chars)) / CHARS_PER_TOKEN)))


def cost_from_tokens(profile: Optional[ModelProfile],
                     input_tokens: Optional[int],
                     output_tokens: Optional[int]) -> Optional[float]:
    """按档案价格计算成本；价格未知返回 None，显式免费返回 0.0。"""
    status = pricing_status(profile)
    if status == PRICING_UNKNOWN:
        return None
    if status == PRICING_FREE:
        return 0.0
    if input_tokens is None or output_tokens is None:
        return None
    price_in = float(profile.price_per_1k_input)
    price_out = float(profile.price_per_1k_output)
    return (int(input_tokens) * price_in
            + int(output_tokens) * price_out) / 1000.0


def estimate_cost_optional(profile: Optional[ModelProfile],
                           input_chars: int,
                           output_chars: int) -> Optional[float]:
    """调用前/无实测用量时的成本预估；价格未知返回 None。"""
    status = pricing_status(profile)
    if status == PRICING_UNKNOWN:
        return None
    if status == PRICING_FREE:
        return 0.0
    price_in = float(profile.price_per_1k_input)
    price_out = float(profile.price_per_1k_output)
    return (max(0, int(input_chars)) * price_in
            + max(0, int(output_chars)) * price_out) / 1000.0


@dataclass
class ModelUsage:
    """单次模型调用的计量记录（可序列化为事件负载）。"""

    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    model_id: str = "unknown"
    provider: str = "unknown"
    op: str = "suggest_action"
    ok: bool = True
    error_class: Optional[str] = None
    latency_s: float = 0.0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    #: None = 成本未知（绝不是 0 元）
    cost: Optional[float] = None
    token_source: str = TOKEN_UNKNOWN
    cost_source: str = COST_UNKNOWN
    request_id: Optional[str] = None
    #: 调用关联；运行时接线后填入，当前可能为空
    task_id: Optional[str] = None
    run_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "model_id": self.model_id,
            "provider": self.provider,
            "op": self.op,
            "ok": self.ok,
            "error_class": self.error_class,
            "latency_s": self.latency_s,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost": self.cost,
            "token_source": self.token_source,
            "cost_source": self.cost_source,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
        }


class UsageLedger:
    """计量对账账本：预估与实测分离，重复记录不重复计费。

    - `begin_estimate()` 预登记一次调用；
    - `record()` 写入实测结果；同一 `call_id` 覆盖预估值，不叠加；
    - `totals()` 的 `actual_cost` 只汇总**已知**实测成本，
      未知成本的调用单独计入 `unknown_calls`。
    """

    def __init__(self) -> None:
        self._calls: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def begin_estimate(self, call_id: str, cost: Optional[float], *,
                       model_id: str = "", provider: str = "",
                       op: str = "", input_tokens: Optional[int] = None,
                       output_tokens: Optional[int] = None) -> None:
        with self._lock:
            entry = self._calls.setdefault(call_id, {})
            entry["estimate_cost"] = cost
            entry["model_id"] = model_id or entry.get("model_id", "")
            entry["provider"] = provider or entry.get("provider", "")
            entry["op"] = op or entry.get("op", "")
            entry["input_tokens"] = input_tokens
            entry["output_tokens"] = output_tokens
            entry.setdefault("usage", None)

    def record(self, usage: ModelUsage) -> None:
        with self._lock:
            entry = self._calls.setdefault(usage.call_id, {})
            entry["usage"] = usage
            entry["model_id"] = usage.model_id
            entry["provider"] = usage.provider
            entry["op"] = usage.op
            entry.setdefault("estimate_cost", None)

    def totals(self) -> Dict[str, Any]:
        with self._lock:
            calls = len(self._calls)
            actual_cost = 0.0
            unknown_calls = 0
            free_calls = 0
            provider_usage_calls = 0
            estimated_usage_calls = 0
            pending_estimates = 0
            estimated_cost_pending = 0.0
            by_model: Dict[str, Dict[str, Any]] = {}
            for entry in self._calls.values():
                usage = entry.get("usage")
                model_id = (usage.model_id if usage is not None
                            else entry.get("model_id")) or "unknown"
                bucket = by_model.setdefault(
                    model_id, {"calls": 0, "actual_cost": 0.0,
                               "unknown_calls": 0})
                bucket["calls"] += 1
                if usage is None:
                    pending_estimates += 1
                    estimate = entry.get("estimate_cost")
                    if estimate is not None:
                        estimated_cost_pending += float(estimate)
                    continue
                if usage.cost is None:
                    unknown_calls += 1
                    bucket["unknown_calls"] += 1
                else:
                    actual_cost += float(usage.cost)
                    bucket["actual_cost"] += float(usage.cost)
                if usage.cost_source == COST_FREE:
                    free_calls += 1
                if usage.token_source == TOKEN_PROVIDER:
                    provider_usage_calls += 1
                elif usage.token_source == TOKEN_ESTIMATE:
                    estimated_usage_calls += 1
            for bucket in by_model.values():
                bucket["actual_cost"] = round(bucket["actual_cost"], 6)
            return {
                "calls": calls,
                "actual_cost": round(actual_cost, 6),
                "unknown_calls": unknown_calls,
                "free_calls": free_calls,
                "provider_usage_calls": provider_usage_calls,
                "estimated_usage_calls": estimated_usage_calls,
                "pending_estimates": pending_estimates,
                "estimated_cost_pending": round(estimated_cost_pending, 6),
                "by_model": by_model,
            }


class MeteredModel:
    """适配器包装：记录调用结果、真实/预估用量与成本来源。

    适配器可通过 `last_usage` 暴露提供方返回的用量
    （如 `OpenAICompatAdapter`），本类读取后按档案价格计算成本。
    """

    def __init__(self, inner: Any, bus: Any = None,
                 component_id: str = "model_meter",
                 ledger: Optional[UsageLedger] = None):
        self.inner = inner
        self.bus = bus
        self.component_id = component_id
        self.ledger = ledger
        self.last_usage: Optional[ModelUsage] = None

    def profile(self):
        return self.inner.profile()

    def estimate_cost(self, input_chars: int, output_chars: int) -> float:
        return self.inner.estimate_cost(input_chars, output_chars)

    def generate(self, messages: List[ChatMessage]) -> str:
        return self._observe("generate", messages,
                             lambda: self.inner.generate(messages))

    def suggest_action(self, messages: List[ChatMessage],
                       available_tools: List[Dict[str, Any]]) -> Action:
        return self._observe(
            "suggest_action",
            messages,
            lambda: self.inner.suggest_action(messages, available_tools))

    def _observe(self, op: str, messages: List[ChatMessage], fn):
        start = time.time()
        ok = True
        error_class: Optional[str] = None
        result: Any = None
        try:
            result = fn()
            return result
        except Exception as exc:  # noqa: BLE001
            ok = False
            error_class = type(exc).__name__
            raise
        finally:
            usage = self._build_usage(
                op, messages, result if ok else None,
                round(time.time() - start, 4), ok, error_class)
            self.last_usage = usage
            if self.ledger is not None:
                try:
                    self.ledger.record(usage)
                except Exception:  # noqa: BLE001 —— 计量失败不影响调用
                    pass
            if self.bus is not None:
                try:
                    self.bus.emit(
                        event_type="model_call",
                        component_id=self.component_id,
                        severity="info" if ok else "error",
                        task_id=usage.task_id, run_id=usage.run_id,
                        payload=usage.to_dict())
                except Exception:  # noqa: BLE001 —— 观测失败不影响调用
                    pass

    def _build_usage(self, op: str, messages: List[ChatMessage],
                     result: Any, latency_s: float, ok: bool,
                     error_class: Optional[str]) -> ModelUsage:
        try:
            profile = self.inner.profile()
        except Exception:  # noqa: BLE001
            profile = None
        model_id = getattr(profile, "model_id", None) or "unknown"
        provider = getattr(profile, "provider", None) or "unknown"
        request_id = getattr(self.inner, "last_request_id", None)
        # 执行租约就是当前运行身份：有租约时把调用归因到 task/run；
        # 无租约（如独立分类端点）保持 None，不臆造关联。
        lease = CURRENT_LEASE.get()
        usage = ModelUsage(
            model_id=model_id, provider=provider, op=op, ok=ok,
            error_class=error_class, latency_s=latency_s,
            request_id=request_id, cost=None,
            cost_source=COST_UNKNOWN, token_source=TOKEN_UNKNOWN,
            task_id=getattr(lease, "task_id", None),
            run_id=getattr(lease, "run_id", None))

        provider_usage = getattr(self.inner, "last_usage", None)
        if (ok and isinstance(provider_usage, dict)
                and provider_usage.get("input_tokens") is not None
                and provider_usage.get("output_tokens") is not None):
            usage.input_tokens = int(provider_usage["input_tokens"])
            usage.output_tokens = int(provider_usage["output_tokens"])
            usage.total_tokens = int(provider_usage.get(
                "total_tokens",
                usage.input_tokens + usage.output_tokens))
            usage.token_source = TOKEN_PROVIDER
            usage.cost = cost_from_tokens(
                profile, usage.input_tokens, usage.output_tokens)
            usage.cost_source = _cost_source(profile, usage.cost,
                                             fallback=COST_PROVIDER)
            return usage

        input_chars = sum(len(m.content or "") for m in messages)
        output_text = _output_text(result)
        usage.input_tokens = tokens_from_chars(input_chars)
        usage.output_tokens = tokens_from_chars(len(output_text))
        usage.total_tokens = usage.input_tokens + usage.output_tokens
        usage.token_source = TOKEN_ESTIMATE
        usage.cost = estimate_cost_optional(
            profile, input_chars, len(output_text))
        usage.cost_source = _cost_source(profile, usage.cost,
                                         fallback=COST_ESTIMATE)
        return usage


def _cost_source(profile: Optional[ModelProfile], cost: Optional[float],
                 *, fallback: str) -> str:
    if pricing_status(profile) == PRICING_FREE:
        return COST_FREE
    if cost is None:
        return COST_UNKNOWN
    return fallback


def _output_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    return (getattr(result, "answer", None)
            or getattr(result, "reasoning", None) or "")
