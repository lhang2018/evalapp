"""主动任务准入执行与真实资源非干扰（ALN-25-C）。

把 25-B 的 `may_start/defer/stop` 接到**实际主动任务启动**：高价值机会 + 策略
允许 + 已知成本 + 原子预算预留成功后才调用提交入口；任一门失败 → **零新增
非必要任务/零外部动作**，已持有的预留按失败路径释放（未启动即释放）。

复用（不复制权威）：
- `ProactivityGovernor.may_start`：档位/并发/无进展停止/资源压力（含 15-D 真实决策）；
- 预算预留对象（`core/reservations.BudgetReservations` 协议）：reserve →
  mark_dispatched →（失败）refund；未知成本不得按 0 处理；
- 提交入口由宿主注入（controller/既有执行通道），本模块不新建执行权威。
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


class ProactiveExecutionError(ValueError):
    """主动执行请求非法或依赖不可用。"""


class ProactiveExecutionService:
    def __init__(self, governor: Any, *, submit: Optional[Callable[..., Any]] = None,
                 reservations: Any = None, account_id: str = "",
                 clock: Optional[Callable[[], float]] = None):
        self.governor = governor
        self.submit = submit
        self.reservations = reservations
        self.account_id = account_id
        self.clock = clock or getattr(governor, "_clock", None)
        self.last_attempt: Optional[Dict[str, Any]] = None

    # ---- 内部 ----
    def _result(self, **payload: Any) -> Dict[str, Any]:
        result = {"started": False, "task_id": None, "reservation_id": None,
                  "refunded": False, "core_state_touched": False, **payload}
        self.last_attempt = result
        return result

    def _text(self, value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ProactiveExecutionError(label + " 不得为空")
        return value.strip()

    def _release(self, reservation_id: str, *, actor: str, reason: str) -> bool:
        if not reservation_id or self.reservations is None:
            return False
        refund = getattr(self.reservations, "refund", None)
        if not callable(refund):
            return False
        refund(reservation_id, actor=actor, reason=reason)
        return True

    # ---- 准入执行 ----
    def attempt(self, *, actor: str, reason: str,
                opportunity_value: Optional[float] = None,
                opportunity_id: str = "",
                concurrent_proactive: int = 0,
                estimated_cost_known: bool = True,
                amounts: Optional[Dict[str, int]] = None,
                ttl_seconds: float = 300.0,
                idempotency_key: str = "",
                submit_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        actor = self._text(actor, "actor")
        reason = self._text(reason, "reason")
        try:
            value = float(opportunity_value) if opportunity_value is not None else 0.0
        except (TypeError, ValueError):
            raise ProactiveExecutionError("opportunity_value 必须是数值") from None
        if value <= 0:
            return self._result(reason="no_high_value_opportunity",
                                zero_new_non_essential_tasks=True)

        decision = self.governor.may_start(
            concurrent_proactive=int(concurrent_proactive))
        if not decision.allowed:
            return self._result(
                reason="; ".join(decision.reasons) or "proactivity_denied",
                safety_notice=decision.safety_notice,
                zero_new_non_essential_tasks=True)

        if not estimated_cost_known:
            return self._result(reason="cost_unknown",
                                zero_new_non_essential_tasks=True,
                                note="未知成本不按 0 处理，拒绝启动")

        reservation = None
        reservation_id = ""
        if self.reservations is not None:
            if not isinstance(amounts, dict) or not any(amounts.values()):
                raise ProactiveExecutionError("预留必须显式给出 amounts")
            key = idempotency_key.strip() or ("proactive-" + actor)
            reserve = getattr(self.reservations, "reserve", None)
            if not callable(reserve):
                raise ProactiveExecutionError("预留对象缺少 reserve")
            try:
                reservation = reserve(
                    self.account_id, key, dict(amounts), ttl_seconds=ttl_seconds,
                    actor=actor, reason=reason)
            except Exception as exc:  # BudgetExceeded/Conflict 等均拒绝启动
                return self._result(reason=f"reservation_failed:{type(exc).__name__}",
                                    detail=str(exc),
                                    zero_new_non_essential_tasks=True)
            reservation_id = str(reservation.get("reservation_id") or "")
            dispatcher = getattr(self.reservations, "mark_dispatched", None)
            if callable(dispatcher):
                try:
                    dispatcher(reservation_id, actor=actor, reason=reason)
                except Exception as exc:
                    self._release(reservation_id, actor=actor,
                                  reason="dispatch_failed")
                    return self._result(
                        reason=f"reservation_dispatch_failed:{type(exc).__name__}",
                        detail=str(exc), refunded=True,
                        zero_new_non_essential_tasks=True)

        if self.submit is None:
            if reservation_id:
                self._release(reservation_id, actor=actor, reason="no_submit_entry")
            return self._result(reason="submit_entry_unavailable", refunded=bool(reservation_id))

        try:
            receipt = self.submit(actor=actor, reason=reason,
                                  idempotency_key=idempotency_key,
                                  opportunity_id=opportunity_id,
                                  payload=dict(submit_payload or {}))
        except Exception as exc:
            refunded = self._release(reservation_id, actor=actor,
                                     reason="submit_failed")
            return self._result(reason=f"submit_failed:{type(exc).__name__}",
                                detail=str(exc), refunded=refunded,
                                zero_new_non_essential_tasks=True)
        task_id = ""
        if isinstance(receipt, dict):
            task_id = str(receipt.get("task_id") or "")
        return self._result(started=True, reason="", task_id=task_id,
                            reservation_id=reservation_id,
                            core_state_touched=False)

    def status(self) -> Dict[str, Any]:
        return {"governor": self.governor.status(),
                "last_attempt": self.last_attempt,
                "submit_configured": self.submit is not None,
                "reservations_configured": self.reservations is not None}
