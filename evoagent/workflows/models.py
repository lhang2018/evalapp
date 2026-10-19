"""工作流模型（PRD 13.2 / Architecture 16.1）。"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class WorkflowNodeState:
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
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
    # 运行时状态
    state: str = WorkflowNodeState.PENDING
    attempt: int = 0
    output: Any = None
    error: Optional[str] = None


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
    compensation_actions: Dict[str, str] = field(default_factory=dict)
    # 7 种工作流状态（PRD 13.3）
    status: str = "pending"
    created_from_task: Optional[str] = None   # 固化来源（PRD 13.4 步骤1）

    def node(self, node_id: str) -> Optional[WorkflowNode]:
        return next((n for n in self.nodes if n.node_id == node_id), None)

    def validate(self) -> List[str]:
        problems = []
        ids = {n.node_id for n in self.nodes}
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
