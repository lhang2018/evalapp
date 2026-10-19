"""统一任务模型（Architecture 3.1）。

所有任务使用统一任务实体，字段与架构文档一一对应。
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskStatus(str, Enum):
    """/task_status 状态见 state_machine.py 的转移表"""

    CREATED = "CREATED"
    CLASSIFYING = "CLASSIFYING"
    PLANNING = "PLANNING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    RECOVERING = "RECOVERING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


TERMINAL_STATES = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


@dataclass
class Task:
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_task_id: Optional[str] = None
    user_goal: str = ""
    intent: str = "general"
    domain: str = "general"
    constraints: List[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    acceptance_criteria: List[Dict[str, Any]] = field(default_factory=list)
    budget_policy: Dict[str, Any] = field(default_factory=dict)
    execution_plan_ref: Optional[str] = None
    workflow_ref: Optional[str] = None
    runtime_manifest: Dict[str, Any] = field(default_factory=dict)
    task_status: TaskStatus = TaskStatus.CREATED
    checkpoint_ref: Optional[str] = None
    # 执行产物（PRD 7.4：最终结果与内部步骤分开记录）
    result: Optional[str] = None
    result_evidence: List[Dict[str, Any]] = field(default_factory=list)
    failure_reason: Optional[str] = None
    confidence: float = 0.9
    version_manifest: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def is_terminal(self) -> bool:
        return self.task_status in TERMINAL_STATES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "user_goal": self.user_goal,
            "intent": self.intent,
            "domain": self.domain,
            "constraints": self.constraints,
            "risk_level": self.risk_level.value,
            "acceptance_criteria": self.acceptance_criteria,
            "budget_policy": self.budget_policy,
            "execution_plan_ref": self.execution_plan_ref,
            "workflow_ref": self.workflow_ref,
            "runtime_manifest": self.runtime_manifest,
            "task_status": self.task_status.value,
            "checkpoint_ref": self.checkpoint_ref,
            "result": self.result,
            "result_evidence": self.result_evidence,
            "failure_reason": self.failure_reason,
            "confidence": self.confidence,
            "version_manifest": self.version_manifest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        d = dict(d)
        d["task_status"] = TaskStatus(d.get("task_status", "CREATED"))
        d["risk_level"] = RiskLevel(d.get("risk_level", "low"))
        t = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        return t
