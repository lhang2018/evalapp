"""ReAct 执行引擎（PRD 12 / Architecture 3.5）。

- ReAct 是运行时内部的决策机制，不是系统总控制器。
- 模型只能提出行动建议，实际执行由运行时控制器校验后完成（PRD 12.4）。
- 防循环机制（PRD 12.3）：最大循环/重复行动检测/重复失败识别/总任务预算/无进展检测/强制终止。
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.budget import BudgetController, BudgetExceeded
from ..core.events import EventBus
from ..models.base import Action, ChatMessage, ModelAdapter
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
    stop_reason: str = ""            # normal | max_iter | no_progress | budget | cancel
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
            cancel_flag: Optional[Any] = None) -> ReactOutcome:
        """执行 ReAct 循环直到 finish / 触发防循环条件。

        cancel_flag: 可被外部线程置位的对象（getattr(obj, 'cancelled')）。
        返回 ReactOutcome；工具结果的**处理与验收**由运行时控制器负责。
        """
        outcome = ReactOutcome()
        messages = list(base_messages)
        tool_schemas = self.tools.schema_for_model(allowed_tools)
        fail_signature_counts: Dict[str, int] = {}
        last_actions: List[str] = []

        started = time.time()
        for i in range(1, self.max_iterations + 1):
            if cancel_flag is not None and getattr(cancel_flag, "cancelled", False):
                outcome.stop_reason = "cancel"
                return outcome

            try:
                budget.check_alive()
                suggestion = self.model.suggest_action(messages, tool_schemas)
            except BudgetExceeded as e:
                outcome.stop_reason = f"budget:{e.dimension}"
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
                budget.check_tool_call()
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
                self.bus.emit(event_type="tool_call", run_id=run_id,
                              payload={"tool": tool, "ok": result.ok,
                                       "error_class": result.error_class})

            # ---- 防循环检测（PRD 12.3）----
            if not last_success(result):
                fail_signature_counts[_fail_sig(tool, result)] = \
                    fail_signature_counts.get(_fail_sig(tool, result), 0) + 1
                outcome.repeated_failures = max(
                    fail_signature_counts.values())
                if fail_signature_counts[_fail_sig(tool, result)] >= self.max_repeat_failures:
                    outcome.stop_reason = "repeated_failure"
                    return outcome
            if last_actions[-3:] == [signature] * 3:
                outcome.stop_reason = "no_progress"
                return outcome

        outcome.stop_reason = "max_iter"
        return outcome


def last_success(result: Optional[ToolResult]) -> bool:
    return bool(result and result.ok)


def _sig(args: Dict[str, Any]) -> str:
    try:
        return _json_compact(args) if args else ""
    except Exception:
        return str(args)


def _fail_sig(tool: Optional[str], result: Optional[ToolResult]) -> str:
    ec = result.error_class if result else "unknown"
    return f"{tool}:{ec}"


def _json_compact(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)
