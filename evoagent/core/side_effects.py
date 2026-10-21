"""副作用账本（契约 09 卷 §8 + 01 卷 R-12，F-03）。

六态状态机：planned → submitted → confirmed | failed | unknown；
unknown 必须**先对账**（reconcile）才能继续（PRD 35 / GAP §7 第 2 项）：
- unknown → confirmed / failed / compensated（对账裁决之一）
- confirmed → compensated（后续补偿可达）
- failed / compensated 为终态

不变量：
- `last_reconciled_at` 只有在对账时被写入；
- 处于 submitted/unknown 的副作用不能被静默丢弃；
- 待定副作用（planned/submitted/unknown）是 resume 的投影源
  （GAP §7：「pending_side_effects 由账本投影而来而非检查点自持」）。

从 ToolResult 到状态的映射（`record_command_result`）：
- 工具契约 side_effect_level in (none,) 不参与账本；
- ok/executed → confirmed（外部世界已变更且结果明确）
- 未 ok 但 side_effect_uncertain（注册表对 external/local/destructive
  失败置位）→ **unknown**（已发生或可能发生，未收到确认）
- 未 ok、未执行、非不确定 → failed（无副作用发生）

**运行期从注册表回传**：ReactEngine 通过可选的 `effects` 回调在每次
工具结果返回时记账（run_id 即 task_id，与控制器一致）。
"""

import time
import uuid
from typing import Any, Dict, List, Optional

__all__ = ["EFFECT_STATUS", "SideEffectLedger"]

#: 契约 §8 的六态。
EFFECT_STATUS = ("planned", "submitted", "confirmed", "failed", "unknown",
                 "compensated")

#: 终态。
_TERMINAL = frozenset({"failed", "compensated"})

#: 允许的状态转移。
TRANSITIONS = {
    "planned": {"submitted", "failed", "unknown"},
    "submitted": {"confirmed", "failed", "unknown", "compensated"},
    "unknown": {"confirmed", "failed", "compensated"},
    "confirmed": {"compensated"},
    "failed": set(),
    "compensated": set(),
}

#: 记入账本的最小副作用等级（tool contract 侧）。
LEDGER_LEVELS = frozenset({"external", "destructive", "local"})


class SideEffectLedger:
    """持久化副作用账本（task_side_effects 表，见 core/schema/effects.py）。"""

    def __init__(self, db):
        self.db = db

    # ---- 记账 ----

    def record(self, task_id: str, tool_id: str, *,
               run_id: Optional[str] = None,
               idempotency_key: Optional[str] = None,
               summary: str = "",
               status: str = "planned") -> str:
        """新副作用入账（effect_id 即记录主键）。"""
        assert status in EFFECT_STATUS, f"非法状态 {status!r}"
        effect_id = "eff_" + uuid.uuid4().hex[:12]
        return self.db.save_side_effect(
            effect_id, task_id, run_id or "", tool_id, idempotency_key,
            summary, status)

    def record_from_tool_result(self, task_id: str, tool_id: str,
                                result, *,
                                run_id: Optional[str] = None,
                                summary: str = "") -> Optional[str]:
        """按注册表回传的 ToolResult 记账并落初态。

        result 为 tools.contract.ToolResult；契约 level 取自
        result.evidence["side_effect_level"]（registry 在执行前落键）。
        返回 effect_id；无副作用（level=none/未记 level 的纯本地失败）→ None。
        """
        level = (result.evidence or {}).get("side_effect_level")
        if level not in LEDGER_LEVELS:
            return None
        eff_id = self.record(task_id, tool_id, run_id=run_id,
                             idempotency_key=result.idempotency_key,
                             summary=summary or f"tool={tool_id}",
                             status="submitted")
        if result.ok and result.executed:
            self.transition(eff_id, "confirmed")
        elif result.side_effect_uncertain:
            self.transition(eff_id, "unknown")
        elif result.executed:
            # 已执行但未成功：结果不确定程度未知 → 走 unknown 保守口径
            self.transition(eff_id, "unknown")
        else:
            self.transition(eff_id, "failed")
        return eff_id

    # ---- 状态机 ----

    def transition(self, effect_id: str, to_status: str, *,
                   receipt_ref: Optional[str] = None,
                   reconciled: bool = False) -> bool:
        """合法转移才写入；非法返回 False（调用方自行决定告警口径）。

        reconciled=True 时同时盖 last_reconciled_at（对账动作）。
        """
        if to_status not in EFFECT_STATUS:
            return False
        reap = "最后" if reconciled else ""
        row = self.get(effect_id)
        if row is None:
            return False
        current = row["status"]
        if current == to_status:
            return True  # 幂等重复转移
        if to_status not in TRANSITIONS[current]:
            return False  # 非法转移拒绝（状态机不变量）
        return self.db.update_side_effect_status(
            effect_id, to_status, receipt_ref=receipt_ref,
            reconciled_at=(time.time() if reconciled else None))

    def reconcile(self, effect_id: str, verdict: str, *,
                  receipt_ref: Optional[str] = None) -> bool:
        """对账：把 unknown 裁决为 confirmed/failed/compensated 之一，
        并盖 last_reconciled_at。一步完成（校验 + 写库）。"""
        row = self.get(effect_id)
        if not row or row["status"] != "unknown":
            return False
        if verdict not in TRANSITIONS["unknown"]:
            return False
        return self.db.update_side_effect_status(
            effect_id, verdict, receipt_ref=receipt_ref,
            reconciled_at=time.time())

    # ---- 查询 / 投影 ----

    def get(self, effect_id: str) -> Optional[Dict[str, Any]]:
        return self.db.get_side_effect(effect_id)

    def pending(self, task_id: str) -> List[Dict[str, Any]]:
        """未决副作用投影（planned/submitted/unknown）——resume 直读。"""
        return self.db.list_side_effects_by_task(
            task_id, statuses=["planned", "submitted", "unknown"])

    def history(self, task_id: str) -> List[Dict[str, Any]]:
        return self.db.list_side_effects_by_task(task_id, statuses=None)
