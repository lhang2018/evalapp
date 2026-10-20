"""安全审计（PRD 84 / Architecture 18.2）。

高风险操作、权限决议、发布/回滚与候选状态变化必须可追溯；
审计记录只追加，不参与业务判断。
"""

import uuid
from typing import Any, Dict, List, Optional


class AuditLog:
    def __init__(self, db: Any = None, bus: Any = None):
        self.db = db
        self.bus = bus

    def record(self, actor: str, action: str, *, target: str = "",
               outcome: str = "ok",
               detail: Optional[Dict[str, Any]] = None) -> str:
        audit_id = "aud_" + uuid.uuid4().hex[:12]
        if self.db is not None:
            self.db.save_audit(audit_id, actor or "anonymous", action,
                               target, outcome, detail or {})
        if self.bus is not None:
            self.bus.emit(event_type="audit", component_id="audit",
                          severity="warn" if outcome != "ok" else "info",
                          payload={"audit_id": audit_id, "actor": actor,
                                   "action": action, "target": target,
                                   "outcome": outcome,
                                   "detail": detail or {}})
        return audit_id

    def list(self, limit: int = 100) -> List[Dict[str, Any]]:
        if self.db is None:
            return []
        return self.db.list_audit(limit=limit)
