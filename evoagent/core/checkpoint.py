"""检查点管理（PRD 34 / Architecture 15.2, 15.5）。

复杂任务保存结构化检查点；恢复时必须重新验证外部状态，
对副作用结果未知的操作不能盲目重试（PRD 35）。
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Checkpoint:
    checkpoint_id: str = field(default_factory=lambda: "cp_" + uuid.uuid4().hex[:10])
    task_id: str = ""
    created_at: float = field(default_factory=time.time)
    # PRD 34 检查点字段
    current_stage: str = ""
    completed_nodes: List[str] = field(default_factory=list)
    pending_nodes: List[str] = field(default_factory=list)
    task_state: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[str] = field(default_factory=list)
    key_constraints: List[str] = field(default_factory=list)
    workflow_version: Optional[str] = None
    last_verification: Optional[Dict[str, Any]] = None
    next_candidate_actions: List[Dict[str, Any]] = field(default_factory=list)
    # 副作用操作记录：结果未知的外部操作，恢复前必须先查证（PRD 35）
    pending_side_effects: List[Dict[str, Any]] = field(default_factory=list)
    # 暂停时的累计预算用量，恢复后继续计入同一任务（PRD 55/56）
    budget_state: Dict[str, Any] = field(default_factory=dict)
    react_state: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Checkpoint":
        cp = cls()
        for k, v in d.items():
            if hasattr(cp, k):
                setattr(cp, k, v)
        return cp


class CheckpointManager:
    def __init__(self, database=None):
        self.db = database

    def save(self, cp: Checkpoint) -> str:
        if self.db is not None:
            self.db.save_checkpoint(cp.checkpoint_id, cp.task_id,
                                    cp.created_at, cp.to_dict())
        return cp.checkpoint_id

    def latest(self, task_id: str) -> Optional[Checkpoint]:
        if self.db is None:
            return None
        snap = self.db.get_latest_checkpoint(task_id)
        if snap is None:
            return None
        return Checkpoint.from_dict(snap)
