"""ReAct 执行引擎（PRD 12 / Architecture 3.5）。

- ReAct 是运行时内部的决策机制，不是系统总控制器。
- 模型只能提出行动建议，实际执行由运行时控制器校验后完成（PRD 12.4）。
- 防循环机制（PRD 12.3）：最大循环/重复行动检测/重复失败识别/总任务预算/无进展检测/强制终止。
- 计划驱动（PRD 11.1）：计划注入上下文并按步骤推进；连续失败时保留原目标并追加恢复步骤。
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.budget import BudgetController, BudgetExceeded
from ..core.events import EventBus
from ..models.base import Action, ChatMessage, ModelAdapter
from ..planning.planner import ExecutionPlan, PlanStep
from ..tools.contract import ToolResult
from ..tools.registry import ToolRegistry


@dataclass
class TraceStep:
    iteration: int
    action: Action
    tool_result: Optional[ToolResult] = None
    error: Optional[str] = None


@dataclass
class ReactOutcome:
    finished: bool = False
    answer: str = ""
    steps: List[TraceStep] = field(default_factory=list)
    # normal | max_iter | no_progress | repeated_failure | budget | cancel | paused | needs_user
    stop_reason: str = ""
    repeated_failures: int = 0


class ReactEngine:
    def __init__(self, model: ModelAdapter, tools: ToolRegistry,
                 event_bus: Optional[EventBus] = None,
                 max_iterations: int = 20,
                 max_repeat_failures: int = 3):
        self.model = model
        self.tools = tools
        self.bus = event_bus
        self.max_iterations = max_iterations
        self.max_repeat_failures = max_repeat_failures

    def run(self, base_messages: List[ChatMessage],
            budget: BudgetController,
            allowed_tools: Optional[List[str]] = None,
            run_id: str = "",
            cancel_flag: Optional[Any] = None,
            plan: Optional[ExecutionPlan] = None,
            model: Optional[ModelAdapter] = None) -> ReactOutcome:
        """执行 ReAct 循环直到 finish / 触发防循环条件。

        cancel_flag: 可被外部线程置位的对象（getattr(obj, 'cancelled'/'paused')）。
        plan: 已通过验收的执行计划；步骤状态在运行中原地推进。
        model: 本次任务路由选定的适配器；未提供时使用构造时的默认模型。
        返回 ReactOutcome；工具结果的**处理与验收**由运行时控制器负责。
        """
        adapter = model or self.model
        outcome = ReactOutcome()
        messages = list(base_messages)
        if plan is not None:
            _ensure_current_running(plan)
            _insert_plan_message(messages, plan)
        tool_schemas = self.tools.schema_for_model(allowed_tools)
        fail_signature_counts: Dict[str, int] = {}
        last_actions: List[str] = []

        for i in range(1, self.max_iterations + 1):
            if _flag(cancel_flag, "cancelled"):
                outcome.stop_reason = "cancel"
                return outcome
            if _flag(cancel_flag, "paused"):
                outcome.stop_reason = "paused"
                return outcome

            try:
                budget.check_model_call()
                suggestion = adapter.suggest_action(messages, tool_schemas)
            except BudgetExceeded as e:
                outcome.stop_reason = f"budget:{e.dimension}"
                return outcome
            try:
                model_id = adapter.profile().model_id
            except Exception:  # noqa: BLE001
                model_id = None
            budget.record_model_call(
                _estimate_cost(adapter, messages, suggestion), model_id)

            # 暂停在模型返回后生效：不执行新的工具调用，避免暂停后仍有副作用
            if _flag(cancel_flag, "paused"):
                outcome.steps.append(TraceStep(i, suggestion))
                outcome.stop_reason = "paused"
                return outcome

            # ---- 行动确认/推进 ----
            if suggestion.kind == "finish":
                outcome.finished = True
                outcome.answer = suggestion.answer or ""
                outcome.stop_reason = "normal"
                outcome.steps.append(TraceStep(i, suggestion))
                return outcome

            if suggestion.kind == "request_user":
                outcome.stop_reason = "needs_user"
                outcome.answer = suggestion.answer or ""
                outcome.steps.append(TraceStep(i, suggestion))
                return outcome

            # tool_call: 交由控制器执行（这里仅执行，验收在控制器）
            tool = suggestion.tool
            signature = f"{tool}:{_sig(suggestion.args)}"
            last_actions.append(signature)
            step = TraceStep(i, suggestion)
            outcome.steps.append(step)

            try:
                budget.check_tool_call(tool)
                result = self.tools.execute(tool or "", suggestion.args,
                                            budget=budget)
            except BudgetExceeded as e:
                outcome.stop_reason = f"budget:{e.dimension}"
                return outcome

            step.tool_result = result
            payload = result.to_dict()
            messages.append(ChatMessage(
                role="assistant",
                content=_json_compact({"kind": suggestion.kind, "tool": tool,
                                       "args": suggestion.args})))
            messages.append(ChatMessage(
                role="tool", content=f"{tool}::{_json_compact(payload)}"))
            if self.bus:
                self.bus.emit(event_type="tool_call", task_id=run_id,
                              run_id=run_id,
                              payload={"tool": tool, "ok": result.ok,
                                       "error_class": result.error_class})

            if plan is not None and result.ok and _advance_plan(plan, tool):
                messages.append(ChatMessage(
                    role="system", content=_plan_progress_message(plan)))
                _ensure_current_running(plan)

            # ---- 防循环检测（PRD 12.3）----
            if not last_success(result):
                fail_sig = _fail_sig(tool, result)
                fail_signature_counts[fail_sig] = \
                    fail_signature_counts.get(fail_sig, 0) + 1
                outcome.repeated_failures = max(
                    fail_signature_counts.values())
                if fail_signature_counts[fail_sig] >= self.max_repeat_failures:
                    if plan is not None:
                        _mark_step_failed(plan, tool)
                    outcome.stop_reason = "repeated_failure"
                    return outcome
            if last_actions[-3:] == [signature] * 3:
                outcome.stop_reason = "no_progress"
                return outcome

        outcome.stop_reason = "max_iter"
        return outcome


def last_success(result: Optional[ToolResult]) -> bool:
    return bool(result and result.ok)


# ---------- 计划驱动 ----------

def _flag(obj: Optional[Any], name: str) -> bool:
    return bool(obj is not None and getattr(obj, name, False))


def _current_step(plan: ExecutionPlan) -> Optional[PlanStep]:
    return next((s for s in plan.steps if s.state in ("pending", "running")),
                None)


def _ensure_current_running(plan: ExecutionPlan) -> None:
    cur = _current_step(plan)
    if cur is not None and cur.state == "pending":
        cur.state = "running"


def _advance_plan(plan: ExecutionPlan, tool: Optional[str]) -> bool:
    cur = _current_step(plan)
    if cur is None:
        return False
    if cur.tool_hint and cur.tool_hint != tool:
        return False
    cur.state = "done"
    if cur.step_id not in plan.completed_work:
        plan.completed_work.append(cur.step_id)
    return True


def _mark_step_failed(plan: ExecutionPlan, tool: Optional[str]) -> None:
    """连续失败：标记当前步骤，并通过 adjust 追加恢复步骤（PRD 11.2）。"""
    cur = _current_step(plan)
    if cur is None:
        return
    cur.state = "failed"
    used = {s.step_id for s in plan.steps}
    n = len(plan.steps) + 1
    while f"r{n}" in used:
        n += 1
    recovery = PlanStep(
        step_id=f"r{n}",
        description=f"失败恢复：核验 {tool or '当前工具'} 的副作用，安全降级或结束",
        deps=[cur.step_id])
    plan.adjust(list(plan.steps) + [recovery],
                reason=f"步骤 {cur.step_id} 连续失败（{tool or 'unknown'}）")


def _insert_plan_message(messages: List[ChatMessage],
                         plan: ExecutionPlan) -> None:
    pos = 0
    while pos < len(messages) and messages[pos].role == "system":
        pos += 1
    messages.insert(pos, ChatMessage(role="system",
                                     content=_plan_message(plan)))


def _plan_message(plan: ExecutionPlan) -> str:
    lines = [f"execution_plan: {plan.goal}"]
    for s in plan.steps:
        hint = f" tool={s.tool_hint}" if s.tool_hint else ""
        lines.append(f"- [{s.state}] {s.step_id}: {s.description}{hint}")
    cur = _current_step(plan)
    if cur is not None:
        lines.append(f"current_step: {cur.step_id}: {cur.description}")
        lines.append("按计划执行当前步骤；完成或确认无法完成后再进入下一步。")
    return "\n".join(lines)


def _plan_progress_message(plan: ExecutionPlan) -> str:
    cur = _current_step(plan)
    done = [s.step_id for s in plan.steps if s.state == "done"]
    if cur is None:
        return f"plan_update: 已完成步骤={done}；没有待执行步骤。"
    return (f"plan_update: 已完成步骤={done}；"
            f"当前步骤={cur.step_id}: {cur.description}")


def _estimate_cost(model: ModelAdapter, messages: List[ChatMessage],
                   suggestion: Action) -> float:
    fn = getattr(model, "estimate_cost", None)
    if not callable(fn):
        return 0.0
    try:
        input_chars = sum(len(m.content or "") for m in messages)
        output_chars = len(suggestion.answer or suggestion.reasoning or "")
        return float(fn(input_chars, output_chars))
    except Exception:  # noqa: BLE001 —— 计量失败不阻断执行
        return 0.0


def _sig(args: Dict[str, Any]) -> str:
    try:
        return _json_compact(args) if args else ""
    except Exception:
        return str(args)


def _fail_sig(tool: Optional[str], result: Optional[ToolResult]) -> str:
    ec = result.error_class if result else "unknown"
    return f"{tool}:{ec}"


def _json_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
