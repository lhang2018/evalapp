"""工作流引擎（PRD 13 / Architecture 16.2-16.3）。

- 工作流负责稳定执行骨架，ReAct 负责局部动态判断，Planning 负责任务级分解。
- 节点级状态、有限重试、补偿动作、检查点恢复。
- 单次成功不构成工作流已经可靠的充分证据（固化需门禁，PRD 13.4）。
"""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..acceptance.verify import Verifier
from ..core.budget import BudgetController, BudgetExceeded
from ..core.checkpoint import Checkpoint, CheckpointManager
from ..core.events import EventBus
from ..tools.contract import ToolResult, validate_against_schema
from ..tools.registry import ToolRegistry
from .models import WorkflowDef, WorkflowNode, WorkflowNodeState


@dataclass
class WorkflowRunResult:
    ok: bool = False
    status: str = "pending"
    outputs: Dict[str, Any] = field(default_factory=dict)
    node_stats: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    error: Optional[str] = None
    retries_used: int = 0


class NodeBudgetSignal(RuntimeError):
    """工具层上报的预算耗尽（error_class=budget_exceeded）。"""


class NodeEffectUnknown(RuntimeError):
    """An external result is unknown; retry/compensation must await reconciliation."""


class WorkflowEngine:
    def __init__(self, tools: ToolRegistry,
                 checkpoint_mgr: Optional[CheckpointManager] = None,
                 event_bus: Optional[EventBus] = None,
                 max_parallel: int = 4):
        self.tools = tools
        self.cp = checkpoint_mgr
        self.bus = event_bus
        self.max_parallel = max(1, int(max_parallel))

    def run(self, wf: WorkflowDef, ctx_input: Dict[str, Any],
            budget: Optional[BudgetController] = None,
            cancel_flag: Optional[Any] = None,
            confirm: Optional[Callable[[str], bool]] = None,
            completed: Optional[Dict[str, Any]] = None,
            allowed_tools: Optional[List[str]] = None
            ) -> WorkflowRunResult:
        problems = wf.validate()
        if problems:
            return WorkflowRunResult(ok=False, status=WorkflowNodeState.FAILED,
                                     error="定义错误: " + "; ".join(problems))
        wf.status = WorkflowNodeState.RUNNING
        result = WorkflowRunResult()
        execution_context = {"task_id": wf.created_from_task or wf.workflow_id,
                             "run_id": wf.created_from_task or wf.workflow_id,
                             "allowed_tools": allowed_tools}

        done: Dict[str, Any] = {}
        for nid, output in (completed or {}).items():
            node = wf.node(nid)
            done[nid] = output
            if node is not None and node.state == WorkflowNodeState.PENDING:
                node.state = WorkflowNodeState.SUCCESS
                node.output = output
        retries_used = 0

        for batch in _batches(wf):
            if cancel_flag is not None and getattr(cancel_flag, "cancelled", False):
                wf.status = WorkflowNodeState.CANCELLED
                result.status = wf.status
                return result

            runnable: List[WorkflowNode] = []
            for node_id in batch:
                node = wf.node(node_id)
                assert node is not None
                if node.state == WorkflowNodeState.WAITING_AUTH:
                    node.state = WorkflowNodeState.PENDING
                if node.state != WorkflowNodeState.PENDING:
                    continue
                # 高风险节点 → 等待授权（PRD 13.3 waiting_authorization）
                if node.requires_confirmation:
                    if not (confirm and confirm(node.node_id)):
                        node.state = WorkflowNodeState.WAITING_AUTH
                        result.error = f"节点 {node_id} 等待授权"
                        wf.status = WorkflowNodeState.WAITING_AUTH
                        result.status = wf.status
                        self._snapshot(wf, result, done, budget)
                        return result
                # 条件不满足 → 跳过，不阻断下游
                if node.condition is not None:
                    try:
                        allowed = bool(node.condition(
                            {"ctx": ctx_input, **done}))
                    except Exception as e:  # noqa: BLE001
                        allowed = False
                        node.error = f"条件求值失败: {e}"
                    if not allowed:
                        node.state = WorkflowNodeState.SKIPPED
                        result.skipped.append(node_id)
                        result.node_stats.append({
                            "node": node_id, "state": node.state,
                            "attempts": 0, "output": None,
                        })
                        continue
                runnable.append(node)

            outcomes = self._run_batch(runnable, ctx_input, dict(done),
                                       budget, wf.workflow_id, execution_context)
            paused_msg: Optional[str] = None
            unknown_msg: Optional[str] = None
            failed: Optional[Tuple[WorkflowNode, str]] = None
            for node, (state, output, error, retries) in zip(runnable, outcomes):
                retries_used += retries
                result.node_stats.append({
                    "node": node.node_id, "state": state,
                    "attempts": node.attempt + 1, "output": _clip(output),
                    "node_run_id": node.node_run_id,
                })
                if state == WorkflowNodeState.SUCCESS:
                    done[node.node_id] = output
                elif state == WorkflowNodeState.PAUSED:
                    paused_msg = error or "预算耗尽"
                elif state == WorkflowNodeState.WAITING_EXTERNAL:
                    unknown_msg = error or "副作用结果待对账"
                elif state == WorkflowNodeState.FAILED:
                    failed = (node, error or "节点失败")

            if unknown_msg is not None:
                wf.status = WorkflowNodeState.WAITING_EXTERNAL
                result.status = wf.status
                result.error = unknown_msg
                result.outputs = dict(done)
                result.retries_used = retries_used
                self._snapshot(wf, result, done, budget)
                return result
            if paused_msg is not None:
                wf.status = WorkflowNodeState.PAUSED
                play_result_budget(None, result, paused_msg)
                result.retries_used = retries_used
                self._snapshot(wf, result, done, budget)
                return result
            if failed is not None:
                node, error = failed
                comp = wf.compensation_actions.get(node.node_id)
                if comp:
                    self._try_compensate(comp, ctx_input, execution_context)
                wf.status = WorkflowNodeState.FAILED
                result.ok = False
                result.status = wf.status
                result.error = f"节点 {node.node_id} 失败: {error}"
                result.retries_used = retries_used
                self._snapshot(wf, result, done, budget)
                return result
            self._snapshot(wf, result, done, budget)

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
        self._snapshot(wf, result, done, budget)
        return result

    def _run_batch(self, nodes: List[WorkflowNode], ctx_input: Dict,
                   snapshot: Dict, budget: Optional[BudgetController],
                   workflow_id: str = "", execution_context: Optional[Dict] = None
                   ) -> List[Tuple[str, Any, Optional[str], int]]:
        if not nodes:
            return []
        if len(nodes) == 1 or self.max_parallel == 1:
            return [self._run_node(n, ctx_input, snapshot, budget,
                                   workflow_id, execution_context)
                    for n in nodes]
        workers = min(self.max_parallel, len(nodes))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._run_node, n, ctx_input, snapshot,
                                   budget, workflow_id, execution_context) for n in nodes]
            return [f.result() for f in futures]

    def _run_node(self, node: WorkflowNode, ctx_input: Dict,
                  done: Dict, budget: Optional[BudgetController],
                  workflow_id: str = "", execution_context: Optional[Dict] = None
                  ) -> Tuple[str, Any, Optional[str], int]:
        # GAP ⑤d：节点执行实体 ID（一次 _run_node = 一个 node_run，
        # 覆盖其全部重试），入运行事件 payload 与任务清单（契约统一标识）
        node.node_run_id = f"nod_{uuid.uuid4().hex[:16]}"
        state, output, error, retries = self._node_attempt_loop(
            node, ctx_input, done, budget, execution_context)
        self._emit_node_run(workflow_id, node, state)
        return state, output, error, retries

    def _node_attempt_loop(self, node: WorkflowNode, ctx_input: Dict,
                           done: Dict, budget: Optional[BudgetController],
                           execution_context: Optional[Dict] = None
                           ) -> Tuple[str, Any, Optional[str], int]:
        node.state = WorkflowNodeState.RUNNING
        attempt = 0
        retries = 0
        while attempt <= node.max_retries:
            node.attempt = attempt
            try:
                if budget:
                    budget.check_alive()
                output = self._exec_node_with_timeout(node, ctx_input, done,
                                                      budget, execution_context)
                if node.output_schema:
                    problem = validate_against_schema(output, node.output_schema)
                    if problem:
                        raise RuntimeError(f"输出契约校验失败: {problem}")
                node.state = WorkflowNodeState.SUCCESS
                node.output = output
                return WorkflowNodeState.SUCCESS, output, None, retries
            except NodeEffectUnknown as e:
                node.state = WorkflowNodeState.WAITING_EXTERNAL
                node.error = str(e)
                return node.state, None, node.error, retries
            except (BudgetExceeded, NodeBudgetSignal) as e:
                # 预算耗尽：暂停而非失败（PRD 8：根据任务情况停止/降级）
                node.state = WorkflowNodeState.PAUSED
                return WorkflowNodeState.PAUSED, None, str(e), retries
            except Exception as e:  # noqa: BLE001
                node.error = f"{type(e).__name__}: {e}"
                # A timed-out Python thread may still be running. Never
                # overlap another attempt, even for an opaque function node.
                if isinstance(e, TimeoutError):
                    node.state = WorkflowNodeState.FAILED
                    return node.state, None, node.error, retries
                attempt += 1
                retries += 1
                if attempt > node.max_retries:
                    node.state = WorkflowNodeState.FAILED
                    return WorkflowNodeState.FAILED, None, node.error, retries
        node.state = WorkflowNodeState.FAILED
        return WorkflowNodeState.FAILED, None, node.error, retries

    def _emit_node_run(self, workflow_id: str, node: WorkflowNode,
                       state: str) -> None:
        """节点执行运行事件（GAP ⑤d：node_run_id 入事件 payload）。

        事件自身故障不影响执行（与事件总线口径一致）。
        """
        if self.bus is None:
            return
        try:
            self.bus.emit(event_type="workflow_node_run",
                          component_id="workflow",
                          payload={"workflow_id": workflow_id,
                                   "node_id": node.node_id,
                                   "node_run_id": node.node_run_id,
                                   "state": state,
                                   "attempts": node.attempt + 1})
        except Exception:  # noqa: BLE001
            pass

    def _exec_node_with_timeout(self, node: WorkflowNode, ctx_input: Dict,
                                done: Dict, budget,
                                execution_context: Optional[Dict] = None) -> Any:
        timeout = node.timeout
        # Tool timeout is owned by the gateway, which also records unknown
        # effects. An outer thread would lose that result and race a retry.
        if node.tool or not timeout or timeout <= 0:
            return self._exec_node(node, ctx_input, done, budget,
                                   execution_context)
        holder: Dict[str, Any] = {}

        def target() -> None:
            try:
                holder["result"] = self._exec_node(node, ctx_input, done, budget,
                                                   execution_context)
            except BaseException as e:  # noqa: BLE001
                holder["error"] = e

        th = threading.Thread(target=target, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            raise TimeoutError(f"节点 {node.node_id} 超时({timeout}s)")
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")

    def _exec_node(self, node: WorkflowNode, ctx_input: Dict, done: Dict,
                   budget: Optional[BudgetController],
                   execution_context: Optional[Dict] = None) -> Any:
        # 工具节点：填入依赖输出（形如 "$node_id" 的参数引用上游结果）
        if node.tool:
            args = dict(node.args)
            for k, v in args.items():
                if isinstance(v, str) and v.startswith("$"):
                    args[k] = done.get(v[1:], v)
            result = self.tools.execute(
                node.tool, args, budget=budget,
                idempotency_key=(str((execution_context or {}).get("task_id", ""))
                                 + ":node:" + node.node_id),
                timeout_seconds=node.timeout, **(execution_context or {}))
            if result.side_effect_uncertain:
                raise NodeEffectUnknown(result.error or "副作用结果待对账")
            if not result.ok:
                if result.error_class == "budget_exceeded":
                    raise NodeBudgetSignal(result.error or "budget")
                raise RuntimeError(result.error or "tool failed")
            return result.data
        if node.fn:
            return node.fn({"ctx": ctx_input, **done})
        raise RuntimeError("无可执行体")

    def _try_compensate(self, comp_tool: str, ctx: Dict,
                        execution_context: Optional[Dict] = None) -> None:
        try:
            self.tools.execute(comp_tool, {}, **(execution_context or {}))
        except Exception:
            pass

    def _snapshot(self, wf: WorkflowDef, result: WorkflowRunResult,
                  done: Optional[Dict[str, Any]] = None,
                  budget: Optional[BudgetController] = None) -> None:
        if self.cp is None:
            return
        cp = Checkpoint(current_stage="workflow_done" if wf.status == "success"
                        else "workflow_" + wf.status)
        cp.task_id = wf.created_from_task or wf.workflow_id
        cp.completed_nodes = [n.node_id for n in wf.nodes
                              if n.state == WorkflowNodeState.SUCCESS]
        cp.pending_nodes = [n.node_id for n in wf.nodes
                            if n.state == WorkflowNodeState.PENDING]
        cp.workflow_version = wf.version
        if self.tools.effects is not None:
            cp.pending_side_effects = self.tools.effects.pending(cp.task_id)
        # 已完成节点输出用于断点续跑（避免重复副作用）
        cp.task_state = {"workflow_outputs": dict(done or {})}
        if budget is not None:
            cp.budget_state = budget.snapshot()
        self.cp.save(cp)


def play_result_budget(node, result, msg):
    if node is not None:
        node.state = WorkflowNodeState.PAUSED
    result.status = WorkflowNodeState.PAUSED
    result.error = f"预算耗尽: {msg}"


def _batches(wf: WorkflowDef) -> List[List[str]]:
    """按依赖分层：同一层内节点互不依赖，可并行执行。"""
    idmap = {n.node_id: n for n in wf.nodes}
    remaining = _topo(wf)
    satisfied: set = set()
    batches: List[List[str]] = []
    while remaining:
        ready = [nid for nid in remaining
                 if all(d in satisfied or d not in idmap
                        for d in idmap[nid].deps)]
        if not ready:
            ready = [remaining[0]]
        batches.append(ready)
        for nid in ready:
            remaining.remove(nid)
            satisfied.add(nid)
    return batches


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
        if n.state == WorkflowNodeState.SKIPPED:
            continue
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
