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

从 ToolResult 到状态的映射（`finish` / legacy `record_from_tool_result`）：
- 工具契约 side_effect_level in (none,) 不参与账本；
- ok/executed → confirmed（外部世界已变更且结果明确）
- 未 ok 但 side_effect_uncertain（注册表对 external/local/destructive
  失败置位）→ **unknown**（已发生或可能发生，未收到确认）
- 未 ok、未执行、非不确定 → failed（无副作用发生）

工具注册表在调用前通过 begin 持久化操作意图，调用后 finish 落结果；
ReAct/DAG 共用该边界。legacy 结果映射只作兼容，不替代执行前记账。
"""

import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional
from .leases import guard_write, assert_connection_lease

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

    def begin(self, task_id: str, tool_id: str, key: str,
              summary: str, *, run_id: str = "", args=None, tool_version=""):
        """Claim a logical operation durably before invoking its runner.

        The primary key arbitrates concurrent callers, including separate
        registry instances after restart. Only a proven failed/compensated
        attempt permits a new attempt; pending and confirmed never replay.
        """
        attempt = 0
        for row in self.history(task_id):
            if (len(row["effect_id"]) == 16 and row["tool_id"] == tool_id
                    and row["idempotency_key"] == key
                    and row["status"] not in _TERMINAL):
                # Existing post-hoc records from the previous format must
                # not disappear just because operation IDs now use a hash.
                return row["effect_id"], False
        while True:
            identity = f"{task_id}\0{tool_id}\0{key}\0{attempt}"
            effect_id = "eff_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
            claimed = self.db.claim_side_effect(
                effect_id, task_id, run_id or task_id, tool_id, key, summary,
                args_json=json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                    if args is not None else None, tool_version=tool_version)
            if not claimed:
                row = self.get(effect_id)
                if row is None or row["effect_summary"] != summary:
                    raise ValueError("副作用幂等键与操作参数不匹配")
                if row["status"] in _TERMINAL:
                    attempt += 1
                    continue
                return effect_id, False
            if not self.transition(effect_id, "submitted"):
                raise RuntimeError("副作用提交状态未持久化")
            return effect_id, True

    def finish(self, effect_id: str, result) -> None:
        """Persist the result before releasing an operation as confirmed."""
        if result.side_effect_uncertain:
            status = "unknown"
        elif result.ok and result.executed:
            # A crash before the confirmed transition remains submitted and
            # blocks replay. A confirmed operation always has its result.
            self.db.save_tool_execution(
                "effect:" + effect_id, result.evidence.get("tool", ""),
                result.to_dict())
            status = "confirmed"
        elif result.side_effect_uncertain or result.executed:
            result.side_effect_uncertain = True
            status = "unknown"
        else:
            status = "failed"
        with self.db._lock:
            guard_write(self.db)
            self.db._conn.execute("UPDATE effect_receipts SET result_json=?,updated_at=? WHERE effect_id=?",
                (json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False, default=str), time.time(), effect_id))
            self.db._conn.commit()
        if not self.transition(effect_id, status):
            raise RuntimeError("副作用结果状态未持久化")

    def bind_operation(self, effect_id, args, tool_version):
        encoded = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.db._lock:
            guard_write(self.db)
            self.db._conn.execute("INSERT OR IGNORE INTO effect_receipts VALUES (?,?,?,NULL,NULL,?)",
                                 (effect_id, encoded, tool_version, time.time()))
            row = self.db._conn.execute("SELECT args_json,tool_version FROM effect_receipts WHERE effect_id=?", (effect_id,)).fetchone()
            if tuple(row) != (encoded, tool_version):
                self.db._conn.rollback()
                raise ValueError("副作用参数或工具版本已改变")
            self.db._conn.commit()

    def operation(self, effect_id):
        with self.db._lock:
            row = self.db._conn.execute("SELECT * FROM effect_receipts WHERE effect_id=?", (effect_id,)).fetchone()
        effect = self.get(effect_id)
        return {**effect, "args": json.loads(row["args_json"]), "tool_version": row["tool_version"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None} if row and effect else None

    def accept_receipt(self, effect_id, result, receipt):
        """Atomically publish verified result + receipt + reconciliation verdict."""
        if not result.independently_verified or result.side_effect_uncertain:
            return False
        ref = str(result.evidence.get("external_receipt_ref") or "").strip()
        if not ref:
            return False
        status = "confirmed" if result.ok and result.executed else "failed" if not result.ok and not result.executed else None
        if status is None:
            return False
        with self.db._lock:
            self.db._conn.execute("BEGIN IMMEDIATE")
            try:
                assert_connection_lease(self.db._conn, self.db)
                row = self.db._conn.execute("SELECT status FROM task_side_effects WHERE effect_id=?", (effect_id,)).fetchone()
                if not row or row["status"] not in ("planned", "submitted", "unknown"):
                    self.db._conn.rollback()
                    return False
                data = json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False)
                self.db._conn.execute("UPDATE effect_receipts SET result_json=?,receipt_json=?,updated_at=? WHERE effect_id=?",
                    (data, json.dumps(receipt, ensure_ascii=False, allow_nan=False), time.time(), effect_id))
                if status == "confirmed":
                    self.db._conn.execute("INSERT OR REPLACE INTO tool_executions VALUES (?,?,?,?)",
                        ("effect:" + effect_id, result.evidence["tool"], time.time(), data))
                self.db._conn.execute("UPDATE task_side_effects SET status=?,external_receipt_ref=?,last_reconciled_at=?,updated_at=? WHERE effect_id=?",
                    (status, ref, time.time(), time.time(), effect_id))
                self.db._conn.commit()
            except BaseException:
                self.db._conn.rollback()
                raise
        return True

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
        existing = (result.evidence or {}).get("effect_id")
        if existing:
            row = self.get(existing)
            if row and row["task_id"] == task_id and row["tool_id"] == tool_id:
                return existing
        eff_id = self.record(task_id, tool_id, run_id=run_id,
                             idempotency_key=result.idempotency_key,
                             summary=summary or f"tool={tool_id}",
                             status="submitted")
        result.evidence["effect_id"] = eff_id
        result.evidence.setdefault("tool", tool_id)
        self.finish(eff_id, result)
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
            reconciled_at=(time.time() if reconciled else None), expected_status=current)

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
            reconciled_at=time.time(), expected_status="unknown")

    # ---- 查询 / 投影 ----

    def get(self, effect_id: str) -> Optional[Dict[str, Any]]:
        return self.db.get_side_effect(effect_id)

    def pending(self, task_id: str) -> List[Dict[str, Any]]:
        """未决副作用投影（planned/submitted/unknown）——resume 直读。"""
        return self.db.list_side_effects_by_task(
            task_id, statuses=["planned", "submitted", "unknown"])

    def history(self, task_id: str) -> List[Dict[str, Any]]:
        return self.db.list_side_effects_by_task(task_id, statuses=None)
