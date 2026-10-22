"""工作流模型（PRD 13.2 / Architecture 16.1）。"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class WorkflowNodeState:
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    WAITING_AUTH = "waiting_authorization"
    WAITING_EXTERNAL = "waiting_external"
    PAUSED = "paused"
    RECOVERING = "recovering"
    CANCELLED = "cancelled"


@dataclass
class WorkflowNode:
    node_id: str
    description: str = ""
    deps: List[str] = field(default_factory=list)
    # 三选一：直接调工具 / 自定义函数 / 子任务标记
    tool: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    fn: Optional[Callable[[Dict[str, Any]], Any]] = None
    max_retries: int = 1
    timeout: float = 30.0
    requires_confirmation: bool = False     # 高风险节点等待授权
    # 条件执行：返回 False 时节点标记 skipped，不阻断下游（PRD 13.2）
    condition: Optional[Callable[[Dict[str, Any]], bool]] = None
    # 输出契约：非空时校验节点输出
    output_schema: Dict[str, Any] = field(default_factory=dict)
    # 运行时状态
    state: str = WorkflowNodeState.PENDING
    attempt: int = 0
    output: Any = None
    error: Optional[str] = None
    approval_id: Optional[str] = None
    tool_result: Optional[Dict[str, Any]] = None


@dataclass
class WorkflowDef:
    workflow_id: str
    version: str = "1.0.0"
    name: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    preconditions: List[str] = field(default_factory=list)
    nodes: List[WorkflowNode] = field(default_factory=list)
    acceptance_criteria: List[Dict[str, Any]] = field(default_factory=list)
    budget: Dict[str, Any] = field(default_factory=dict)
    compensation_actions: Dict[str, Any] = field(default_factory=dict)
    # 7 种工作流状态（PRD 13.3）
    status: str = "pending"
    created_from_task: Optional[str] = None   # 固化来源（PRD 13.4 步骤1）

    def node(self, node_id: str) -> Optional[WorkflowNode]:
        return next((n for n in self.nodes if n.node_id == node_id), None)

    def to_dict(self) -> Dict[str, Any]:
        """可持久化的定义（函数/条件不可序列化，由代码侧重新注册）。"""
        return {
            "workflow_id": self.workflow_id, "version": self.version,
            "name": self.name, "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "preconditions": self.preconditions,
            "acceptance_criteria": self.acceptance_criteria,
            "budget": self.budget,
            "compensation_actions": self.compensation_actions,
            "status": self.status,
            "created_from_task": self.created_from_task,
            "nodes": [{
                "node_id": n.node_id, "description": n.description,
                "deps": n.deps, "tool": n.tool, "args": n.args,
                "max_retries": n.max_retries, "timeout": n.timeout,
                "requires_confirmation": n.requires_confirmation,
                "output_schema": n.output_schema,
            } for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WorkflowDef":
        nodes = [WorkflowNode(**{k: v for k, v in n.items()
                                 if k in WorkflowNode.__dataclass_fields__
                                 and k not in ("fn", "condition", "state",
                                               "attempt", "output", "error")})
                 for n in d.get("nodes") or []]
        return cls(
            workflow_id=d["workflow_id"], version=d.get("version", "1.0.0"),
            name=d.get("name", ""), input_schema=d.get("input_schema") or {},
            output_schema=d.get("output_schema") or {},
            preconditions=list(d.get("preconditions") or []),
            nodes=nodes,
            acceptance_criteria=list(d.get("acceptance_criteria") or []),
            budget=dict(d.get("budget") or {}),
            compensation_actions=dict(d.get("compensation_actions") or {}),
            status=d.get("status", "pending"),
            created_from_task=d.get("created_from_task"))

    def validate(self) -> List[str]:
        problems = []
        ids = {n.node_id for n in self.nodes}
        if len(ids) != len(self.nodes):
            problems.append("节点ID重复")
        for n in self.nodes:
            for d in n.deps:
                if d not in ids:
                    problems.append(f"节点 {n.node_id} 依赖不存在的 {d}")
            if n.tool is None and n.fn is None:
                problems.append(f"节点 {n.node_id} 未定义可执行体")
        # 环检测
        visited, stack = set(), set()
        def dfs(nid: str) -> None:
            if nid in stack:
                problems.append(f"存在依赖环: {nid}")
                return
            if nid in visited:
                return
            stack.add(nid)
            node = self.node(nid)
            if node:
                for d in node.deps:
                    dfs(d)
            stack.discard(nid)
            visited.add(nid)
        for n in self.nodes:
            dfs(n.node_id)
        return problems
