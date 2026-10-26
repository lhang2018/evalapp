"""主动任务准入执行与真实资源非干扰（ALN-25-C）。

把 25-B 的 `may_start/defer/stop` 接到**实际主动任务启动**：高价值机会 + 策略
允许 + 有效资源采样 + 显式已知成本 + 原子预算预留成功后才调用提交入口。
提交前拒绝可证明零新增；派发后丢失回执只能登记未知，保留预算等待对账。

复用（不复制权威）：
- `ProactivityGovernor.may_start`：档位/并发/无进展停止/资源压力（含 15-D 真实决策）；
- 预算预留对象（`core/reservations.BudgetReservations` 协议）：reserve →
  mark_dispatched → pending/unknown；只有未派发预留可直接 refund；
- 提交入口由宿主注入（controller/既有执行通道），本模块不新建执行权威。
"""

import math
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
        try:
            result = refund(reservation_id, actor=actor, reason=reason)
            return isinstance(result, dict) and result.get("state") == "refunded"
        except Exception:
            # A concurrent dispatcher may have claimed it; never pretend it is free.
            return False

    def _unknown(self, reservation_id: str, *, actor: str, reason: str,
                 detail: str = "") -> Dict[str, Any]:
        error = None
        try:
            self.reservations.mark_unknown(reservation_id, actor=actor, reason=reason,
                evidence_ref="proactive:receipt-unknown:" + reservation_id)
        except Exception as exc:
            # Keep the dispatched hold even if recording unknown fails. Expiry
            # recovery also treats dispatched work as unknown, never as a refund.
            error = f"{type(exc).__name__}: {exc}"
        return self._result(reason=reason, detail=detail, reservation_id=reservation_id,
            submission_state="unknown", reconciliation_required=True,
            reconciliation_error=error, zero_new_non_essential_tasks=False)

    # ---- 准入执行 ----
    def attempt(self, *, actor: str, reason: str,
                opportunity_value: Optional[float] = None,
                opportunity_id: str = "",
                concurrent_proactive: int = 0,
                estimated_cost_known: bool = False,
                amounts: Optional[Dict[str, int]] = None,
                ttl_seconds: float = 300.0,
                idempotency_key: str = "",
                submit_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        actor = self._text(actor, "actor")
        reason = self._text(reason, "reason")
        value = opportunity_value if opportunity_value is not None else 0.0
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ProactiveExecutionError("opportunity_value 必须是有限数值")
        if type(concurrent_proactive) is not int or concurrent_proactive < 0:
            raise ProactiveExecutionError("concurrent_proactive 必须是非负整数")
        if type(estimated_cost_known) is not bool:
            raise ProactiveExecutionError("estimated_cost_known 必须是布尔值")
        if submit_payload is not None and not isinstance(submit_payload, dict):
            raise ProactiveExecutionError("submit_payload 必须是对象")
        if value <= 0:
            return self._result(reason="no_high_value_opportunity",
                                zero_new_non_essential_tasks=True)

        decision = self.governor.may_start(
            concurrent_proactive=concurrent_proactive)
        if not decision.allowed:
            return self._result(
                reason="; ".join(decision.reasons) or "proactivity_denied",
                safety_notice=decision.safety_notice,
                zero_new_non_essential_tasks=True)

        if not estimated_cost_known:
            return self._result(reason="cost_unknown",
                                zero_new_non_essential_tasks=True,
                                note="未知成本不按 0 处理，拒绝启动")

        # Fail before acquiring any hold if a required host dependency is absent.
        # The App's default unconfigured service must not silently bypass gates.
        if not callable(self.submit):
            return self._result(reason="submit_entry_unavailable", zero_new_non_essential_tasks=True)
        if getattr(self.governor, "resource_governor", None) is None:
            return self._result(reason="resource_governor_unavailable", zero_new_non_essential_tasks=True)
        methods = ("reserve", "get", "mark_dispatched", "mark_unknown", "refund")
        if self.reservations is None or not all(
                callable(getattr(self.reservations, name, None)) for name in methods):
            return self._result(reason="reservation_store_unavailable", zero_new_non_essential_tasks=True)
        if not isinstance(amounts, dict) or not any(amounts.values()):
            raise ProactiveExecutionError("预留必须显式给出 amounts")
        key = self._text(idempotency_key, "idempotency_key")
        try:
            reservation = self.reservations.reserve(
                self.account_id, key, dict(amounts), ttl_seconds=ttl_seconds,
                actor=actor, reason=reason)
        except Exception as exc:
            return self._result(reason=f"reservation_failed:{type(exc).__name__}",
                                detail=str(exc), zero_new_non_essential_tasks=True)
        if not isinstance(reservation, dict) or not isinstance(
                reservation.get("reservation_id"), str) or not reservation["reservation_id"].strip():
            return self._result(reason="reservation_receipt_invalid", reconciliation_required=True,
                                zero_new_non_essential_tasks=True)
        reservation_id = reservation["reservation_id"]
        if reservation.get("state") != "reserved" or reservation.get("dispatched") is not False:
            return self._result(reason="reservation_replay_requires_reconciliation",
                reservation_id=reservation_id, reconciliation_required=True,
                zero_new_non_essential_tasks=True)
        try:
            claim = self.reservations.mark_dispatched(reservation_id, actor=actor, reason=reason)
            if (not isinstance(claim, dict) or claim.get("reservation_id") != reservation_id
                    or claim.get("state") != "reserved" or claim.get("dispatched") is not True):
                raise ProactiveExecutionError("dispatch receipt does not confirm the durable claim")
        except Exception as exc:
            refunded = self._release(reservation_id, actor=actor, reason="dispatch_failed")
            return self._result(reason=f"reservation_dispatch_failed:{type(exc).__name__}",
                detail=str(exc), reservation_id=reservation_id, refunded=refunded,
                reconciliation_required=not refunded, zero_new_non_essential_tasks=True)

        try:
            receipt = self.submit(actor=actor, reason=reason,
                                  idempotency_key=key,
                                  opportunity_id=opportunity_id,
                                  payload=dict(submit_payload or {}))
        except Exception as exc:
            return self._unknown(reservation_id, actor=actor,
                                 reason=f"submit_failed:{type(exc).__name__}", detail=str(exc))
        if (not isinstance(receipt, dict) or not isinstance(receipt.get("task_id"), str)
                or not receipt["task_id"].strip() or ("ok" in receipt and receipt["ok"] is not True)):
            return self._unknown(reservation_id, actor=actor, reason="submit_receipt_invalid")
        task_id = receipt["task_id"].strip()
        return self._result(started=True, reason="", task_id=task_id,
                            reservation_id=reservation_id,
                            submission_state="acknowledged", reconciliation_required=True,
                            core_state_touched=False)

    def status(self) -> Dict[str, Any]:
        return {"governor": self.governor.status(),
                "last_attempt": self.last_attempt,
                "submit_configured": self.submit is not None,
                "reservations_configured": self.reservations is not None}
