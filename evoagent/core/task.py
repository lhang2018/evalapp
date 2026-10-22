"""统一任务模型（Architecture 3.1）。

所有任务使用统一任务实体，字段与架构文档一一对应。
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class GoalOrigin(str, Enum):
    """目标来源（契约 §2 Task.origin，G-01 目标注册表前置）。

    user        用户直接下达
    scheduled   定时/维护计划触发
    derived     从父目标派生的子目标
    opportunity AGIG 主动机会发现产生的候选
    """

    USER = "user"
    SCHEDULED = "scheduled"
    DERIVED = "derived"
    OPPORTUNITY = "opportunity"


ORIGIN_VALUES = {g.value for g in GoalOrigin}


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
    current_run_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    waiting_request: Optional[Dict[str, Any]] = None
    waiting_approvals: List[str] = field(default_factory=list)
    user_answers: List[Dict[str, Any]] = field(default_factory=list)
    # G-01 前置（契约 §2）：任务与长期 Goal 通过 goal_id 引用关联，不共享万能状态字段
    goal_id: Optional[str] = None
    authorization_ref: Optional[str] = None
    origin: str = GoalOrigin.USER.value
    user_goal: str = ""
    intent: str = "general"
    domain: str = "general"
    # PRD 7.3 任务解析结果：目标对象、预期输出、复杂度、所需工具/权限、
    # 网络依赖、工作流适用性、尚未确定的关键信息
    target_object: str = ""
    expected_output: str = ""
    complexity: str = ""                  # simple | moderate | complex
    required_tools: List[str] = field(default_factory=list)
    required_permissions: List[str] = field(default_factory=list)
    requires_network: Optional[bool] = None
    workflow_suitable: Optional[bool] = None
    open_questions: List[str] = field(default_factory=list)
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
            "current_run_id": self.current_run_id,
            "parent_run_id": self.parent_run_id,
            "waiting_request": self.waiting_request,
            "waiting_approvals": list(self.waiting_approvals),
            "user_answers": list(self.user_answers),
            "goal_id": self.goal_id,
            "authorization_ref": self.authorization_ref,
            "origin": self.origin,
            "user_goal": self.user_goal,
            "intent": self.intent,
            "domain": self.domain,
            "target_object": self.target_object,
            "expected_output": self.expected_output,
            "complexity": self.complexity,
            "required_tools": self.required_tools,
            "required_permissions": self.required_permissions,
            "requires_network": self.requires_network,
            "workflow_suitable": self.workflow_suitable,
            "open_questions": self.open_questions,
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
        # goal_id/origin 兼容旧数据：缺省按用户直接下达处理；非法值兜底为 user
        origin = d.get("origin") or GoalOrigin.USER.value
        if origin not in ORIGIN_VALUES:
            origin = GoalOrigin.USER.value
        d["origin"] = origin
        t = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        return t
