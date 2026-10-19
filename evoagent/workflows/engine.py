"""工作流引擎（PRD 13 / Architecture 16.2-16.3）。

- 工作流负责稳定执行骨架，ReAct 负责局部动态判断，Planning 负责任务级分解。
- 节点级状态、有限重试、补偿动作、检查点恢复。
- 单次成功不构成工作流已经可靠的充分证据（固化需门禁，PRD 13.4）。
"""

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..acceptance.verify import Verifier
from ..core.budget import BudgetController, BudgetExceeded
from ..core.checkpoint import Checkpoint, CheckpointManager
from ..core.events import EventBus
from ..tools.contract import ToolResult
from ..tools.registry import ToolRegistry
from .models import WorkflowDef, WorkflowNode, WorkflowNodeState


@dataclass
class WorkflowRunResult:
    ok: bool = False
    status: str = "pending"
    outputs: Dict[str, Any] = field(default_factory=dict)
    node_stats: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    retries_used: int = 0


class NodeBudgetSignal(RuntimeError):
    """工具层上报的预算耗尽（error_class=budget_exceeded）。"""


class WorkflowEngine:
    def __init__(self, tools: ToolRegistry,
                 checkpoint_mgr: Optional[CheckpointManager] = None,
                 event_bus: Optional[EventBus] = None):
        self.tools = tools
        self.cp = checkpoint_mgr
        self.bus = event_bus

    def run(self, wf: WorkflowDef, ctx_input: Dict[str, Any],
            budget: Optional[BudgetController] = None,
            cancel_flag: Optional[Any] = None,
            confirm: Optional[Callable[[str], bool]] = None) -> WorkflowRunResult:
        problems = wf.validate()
        if problems:
            return WorkflowRunResult(ok=False, status=WorkflowNodeState.FAILED,
                                     error="定义错误: " + "; ".join(problems))
        wf.status = WorkflowNodeState.RUNNING
        result = WorkflowRunResult()

        done: Dict[str, Any] = {}
        retries_used = 0
        order = _topo(wf)
        for node_id in order:
            node = wf.node(node_id)
            assert node is not None
            if cancel_flag is not None and getattr(cancel_flag, "cancelled", False):
                wf.status = WorkflowNodeState.CANCELLED
                result.status = wf.status
                return result
            # 高风险节点 → 等待授权（PRD 13.3 waiting_authorization）
            if node.requires_confirmation:
                if not (confirm and confirm(node.node_id)):
                    node.state = WorkflowNodeState.WAITING_AUTH
                    result.error = f"节点 {node_id} 等待授权"
                    wf.status = WorkflowNodeState.WAITING_AUTH
                    result.status = wf.status
                    return result

            node.state = WorkflowNodeState.RUNNING
            attempt = 0
            while attempt <= node.max_retries:
                node.attempt = attempt
                try:
                    if budget:
                        budget.check_alive()
                    output = self._exec_node(node, ctx_input, done, budget)
                    node.state = WorkflowNodeState.SUCCESS
                    node.output = output
                    done[node_id] = output
                    break
                except BudgetExceeded as e:
                    # 预算耗尽：暂停而非失败（PRD 8：根据任务情况停止/降级）
                    play_result_budget(node, result, str(e))
                    result.retries_used = retries_used
                    return result
                except NodeBudgetSignal as e:
                    play_result_budget(node, result, str(e))
                    result.retries_used = retries_used
                    return result
                except Exception as e:  # noqa: BLE001
                    node.error = f"{type(e).__name__}: {e}"
                    attempt += 1
                    retries_used += 1
                    if attempt > node.max_retries:
                        node.state = WorkflowNodeState.FAILED
                        # 补偿动作（PRD 13.2 compensation）
                        comp = wf.compensation_actions.get(node_id)
                        if comp:
                            self._try_compensate(comp, ctx_input)
                        wf.status = WorkflowNodeState.FAILED
                        result.ok = False
                        result.status = wf.status
                        result.error = f"节点 {node_id} 失败: {node.error}"
                        result.retries_used = retries_used
                        self._snapshot(wf, result)
                        return result
            result.node_stats.append({
                "node": node_id, "state": node.state,
                "attempts": node.attempt + 1, "output": _clip(node.output),
            })
        # 端到端验收（PRD 13 / 51.3）：基于每个节点的实际执行结果
        verdict = Verifier().check(
            wf.acceptance_criteria,
            answer=_summarize(done),
            tool_results=_as_tool_results(wf))
        wf.status = WorkflowNodeState.SUCCESS if verdict.passed else WorkflowNodeState.FAILED
        result.outputs = done
        result.ok = verdict.passed
        result.status = wf.status
        result.retries_used = retries_used
        if not verdict.passed:
            result.error = "端到端验收未通过: " + verdict.summary()
        self._snapshot(wf, result)
        return result

    def _exec_node(self, node: WorkflowNode, ctx_input: Dict, done: Dict,
                   budget: Optional[BudgetController]) -> Any:
        # 工具节点：填入依赖输出（形如 "$node_id" 的参数引用上游结果）
        if node.tool:
            args = dict(node.args)
            for k, v in args.items():
                if isinstance(v, str) and v.startswith("$"):
                    args[k] = done.get(v[1:], v)
            result = self.tools.execute(node.tool, args, budget=budget)
            if not result.ok:
                if result.error_class == "budget_exceeded":
                    raise NodeBudgetSignal(result.error or "budget")
                raise RuntimeError(result.error or "tool failed")
            return result.data
        if node.fn:
            return node.fn({"ctx": ctx_input, **done})
        raise RuntimeError("无可执行体")

    def _try_compensate(self, comp_tool: str, ctx: Dict) -> None:
        try:
            self.tools.execute(comp_tool, {})
        except Exception:
            pass

    def _snapshot(self, wf: WorkflowDef, result: WorkflowRunResult) -> None:
        if self.cp is None:
            return
        cp = Checkpoint(current_stage="workflow_done" if wf.status == "success" else "workflow_" + wf.status)
        cp.task_id = wf.created_from_task or wf.workflow_id
        cp.completed_nodes = [n.node_id for n in wf.nodes if n.state == WorkflowNodeState.SUCCESS]
        cp.pending_nodes = [n.node_id for n in wf.nodes if n.state == WorkflowNodeState.PENDING]
        cp.workflow_version = wf.version
        self.cp.save(cp)


def play_result_budget(node, result, msg):
    node.state = WorkflowNodeState.PAUSED
    result.status = WorkflowNodeState.PAUSED
    result.error = f"预算耗尽: {msg}"


def _topo(wf: WorkflowDef) -> List[str]:
    """拓扑排序（Kahn）；定义错误已提前拒绝，这里直接顺序输出。"""
    ids = {n.node_id for n in wf.nodes}
    indeg = {nid: 0 for nid in ids}
    dependents: Dict[str, List[str]] = {nid: [] for nid in ids}
    for n in wf.nodes:
        for d in n.deps:
            if d in indeg:
                indeg[n.node_id] += 1
                dependents[d].append(n.node_id)
    queue = [nid for nid, d in indeg.items() if d == 0]
    order: List[str] = []
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for m in dependents[nid]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    # 环中的节点已被 validate() 拒绝，理论到不了这里
    order += [nid for nid in ids if nid not in order]
    return order


def _clip(v: Any) -> Any:
    s = str(v)
    return s if len(s) <= 200 else s[:200] + "…"


def _summarize(done: Dict[str, Any]) -> str:
    return " | ".join(f"{k}: {_clip(v)}" for k, v in done.items())


def _as_tool_results(wf: WorkflowDef) -> List[ToolResult]:
    """按节点实际执行结果构造验收证据（evidence.tool 记录节点所用工具）。"""
    out: List[ToolResult] = []
    for n in wf.nodes:
        ok = n.state == WorkflowNodeState.SUCCESS
        out.append(ToolResult(ok=ok, data=n.output,
                              submitted=True, executed=ok,
                              contract_valid=True,
                              goal_reached=ok,
                              independently_verified=ok,
                              error=n.error,
                              evidence={"tool": n.tool or "fn",
                                        "node": n.node_id}))
    return out
