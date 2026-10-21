"""审批记录（A-01 五项绑定 + 09 卷 §1 `approval_id`，F-04）。

**A-01 五项缺一不记**：
1. 动作摘要 `action_summary`（绑具体动作，不是裸 op 名）
2. 目标对象 `target`（节点/任务/工具参数的规范摘要）
3. 范围 `scope`（op 调用类别）
4. 有效期 `expires_at`（None= indefinite）
5. 一次性语义：一个 approval 只授权一次执行——授即用（synchronous
   confirm 流），再次执行须重新授权；`used` 后不可复用。

状态机：approved → used（消费）；approved → revoked（吊销）；
        approved → expired（到期）；denied / revoked / expired 终态。
读侧惰性过期（`_age` 处理 expired 标记）。

接线策略（GAP 第一批第 3 项）：`PermissionController._decide_high_risk`
是 check_tool（高风险工具）与 check_op（工作流审批节点）的**单一收口**，
三种放行路径（用户确认 / policy:allow / policy:deny）全部落账——
**在高风险决策处不存在不落账的放行**。
resume 的 `force` 明示授权同样落账（与侧结果未知→对账链相接，F-03）。
"""

import json
import time
import uuid
from typing import Any, Dict, List, Optional

__all__ = ["APPROVAL_STATUS", "ApprovalStore"]


#: 状态域。pending 预留异步审批（本批仍走同步 confirm），
#: used/denied/revoked/expired 为已决记录（审计可见）。
APPROVAL_STATUS = ("pending", "approved", "used", "denied", "revoked",
                   "expired")


class ApprovalStore:
    """持久化审批记录（approvals 表，见 core/schema/approvals.py）。

    db 由宿主注入（与 SideEffectLedger 同风格）。
    """

    def __init__(self, db):
        self.db = db

    # ---- 记录 ----

    def grant(self, op: str, action_summary: str, *, target: str = "",
              scope: str = "", task_id: str = "", granted_by: str = "",
              ttl_seconds: Optional[float] = None) -> str:
        """授权即落账（approved 即决；expires_at=None 表示不设有效期）。"""
        approval_id = "apr_" + uuid.uuid4().hex[:12]
        return self.db.save_approval(
            approval_id, task_id=task_id, op=op,
            target=(target or "")[:300],
            scope=scope or op,
            action_summary=(action_summary or op)[:500],
            status="approved", granted_by=granted_by,
            expires_at=(time.time() + ttl_seconds)
            if ttl_seconds is not None else None)

    def deny(self, op: str, action_summary: str, *, target: str = "",
             scope: str = "", task_id: str = "", granted_by: str = ""
             ) -> str:
        approval_id = "apr_" + uuid.uuid4().hex[:12]
        return self.db.save_approval(
            approval_id, task_id=task_id, op=op,
            target=(target or "")[:300], scope=scope or op,
            action_summary=(action_summary or op)[:500],
            status="denied", granted_by=granted_by, expires_at=None)

    # ---- 消费 / 吊销 / 过期 ----

    def consume(self, approval_id: str) -> bool:
        """一次性语义：approved → used（且仅一次）。"""
        return self.db.consume_approval(approval_id)

    def revoke(self, approval_id: str) -> bool:
        return self.db.revoke_approval(approval_id)

    def expire_stale(self, now: Optional[float] = None) -> int:
        """把已过 有效期 的 approved 记录标 expired，返回数量。"""
        return self.db.expire_approvals(now if now is not None
                                        else time.time())

    # ---- 查询 ----

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.get_approval(approval_id)
        if row is None:
            return None
        if row["status"] == "approved" and row["expires_at"] and \
                row["expires_at"] < time.time():
            return {**row, "status": "expired"}
        return row

    def list_for_task(self, task_id: str,
                      limit: int = 100) -> List[Dict[str, Any]]:
        return self.db.list_approvals_by_task(task_id, limit=limit)

    def list_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.list_approvals_recent(limit=limit)

    # ---- 高风险决策便利封装 ----

    def note_decision(self, op: str, args: Optional[Dict[str, Any]] = None, *,
                      task_id: str = "", granted_by: str = "",
                      approved: bool, ttl_seconds: Optional[float] = None
                      ) -> str:
        """按 A-01 五项绑定生成一次决策记录并**立即消费**。

        action_summary = op + 规范化参数摘要；target = args["target"] 或
        全参数 JSON（截断）；scope = op 调用类别。
        """
        args = dict(args or {})
        target = str(args.get("target") or args.get("target_id")
                     or args.get("node") or "")
        summary_args = json.dumps(args, ensure_ascii=False,
                                  sort_keys=True, default=str)
        summary = f"{op} {summary_args}" if summary_args else op
        bindings = dict(target=target or summary_args[:280], scope=op,
                        task_id=task_id, granted_by=granted_by)
        if approved:
            approval_id = self.grant(
                op, summary,
                ttl_seconds=ttl_seconds, **bindings)
            self.consume(approval_id)  # 一次性：授即用（同步流）
        else:
            approval_id = self.deny(op, summary, **bindings)
        return approval_id
