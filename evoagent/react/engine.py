"""ReAct 执行引擎（PRD 12 / Architecture 3.5）。

- ReAct 是运行时内部的决策机制，不是系统总控制器。
- 模型只能提出行动建议，实际执行由运行时控制器校验后完成（PRD 12.4）。
- 防循环机制（PRD 12.3）：最大循环/重复行动检测/重复失败识别/总任务预算/无进展检测/强制终止。
- 计划驱动（PRD 11.1）：计划注入上下文并按步骤推进；连续失败时保留原目标并追加恢复步骤。
"""

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.budget import BudgetController, BudgetExceeded
from ..core.events import EventBus
from ..core.leases import LeaseLost, assert_current_lease
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
    continuation: Dict[str, Any] = field(default_factory=dict)
    approval_ids: List[str] = field(default_factory=list)


class ReactEngine:
    def __init__(self, model: ModelAdapter, tools: ToolRegistry,
                 event_bus: Optional[EventBus] = None,
                 max_iterations: int = 20,
                 max_repeat_failures: int = 3,
                 effects: Any = None,
                 bindings: Any = None):
        self.model = model
        self.tools = tools
        self.bus = event_bus
        self.max_iterations = max_iterations
        self.max_repeat_failures = max_repeat_failures
        # Optional ledger is passed to the tool gateway, which writes before
        # execution. ReAct must not create a second, post-hoc ledger entry.
        self.effects = effects
        # Optional action-binding ledger (ALN-07-B): records which plan step
        # triggered each side effect. Evidence only — never an authorization
        # input, and it never changes what tools may run.
        self.bindings = bindings

    def run(self, base_messages: List[ChatMessage],
            budget: BudgetController,
            allowed_tools: Optional[List[str]] = None,
            run_id: str = "",
            cancel_flag: Optional[Any] = None,
            plan: Optional[ExecutionPlan] = None,
            model: Optional[ModelAdapter] = None,
            task_id: Optional[str] = None,
            restored_state: Optional[Dict[str, Any]] = None,
            user_replies: Optional[List[Dict[str, Any]]] = None,
            on_checkpoint: Optional[Callable[[Dict[str, Any]], None]] = None) -> ReactOutcome:
        """执行 ReAct 循环直到 finish / 触发防循环条件。

        cancel_flag: 可被外部线程置位的对象（getattr(obj, 'cancelled'/'paused')）。
        plan: 已通过验收的执行计划；步骤状态在运行中原地推进。
        model: 本次任务路由选定的适配器；未提供时使用构造时的默认模型。
        返回 ReactOutcome；工具结果的**处理与验收**由运行时控制器负责。
        """
        adapter = model or self.model
        logical_task_id = task_id or run_id
        outcome = ReactOutcome()
        if plan is not None:
            plan.validate_execution_state()
        restored = restored_state or {}
        messages = ([ChatMessage(**m) for m in restored["messages"]]
                    if restored.get("messages") else list(base_messages))
        for step in restored.get("steps", []):
            outcome.steps.append(TraceStep(
                step["iteration"], Action(**step["action"]),
                ToolResult(**step["tool_result"]) if step.get("tool_result") else None))
        if plan is not None:
            _ensure_current_running(plan)
            if restored.get("messages"):
                # Refresh this engine-owned context slot without growing it on
                # every resume. Execution traces remain the audit history.
                messages[:] = [m for m in messages if not (
                    m.role == "system" and m.content.startswith("execution_plan:"))]
                messages.append(ChatMessage(role="system", content=_plan_message(plan)))
            else:
                _insert_plan_message(messages, plan)
        tool_schemas = self.tools.schema_for_model(allowed_tools)
        fail_signature_counts: Dict[str, int] = dict(restored.get("failure_counts") or {})
        last_actions: List[str] = list(restored.get("last_actions") or [])
        delivered = set(restored.get("delivered_reply_ids") or [])
        for reply in user_replies or []:
            if reply["request_id"] not in delivered:
                messages.append(ChatMessage(role="user", content=reply["answer"]))
                delivered.add(reply["request_id"])
        next_iteration = int(restored.get("next_iteration") or 1)
        pending_action = restored.get("pending_action")
        model_call_inflight = False

        def capture():
            outcome.continuation = {
                "messages": [asdict(m) for m in messages],
                "steps": [asdict(s) for s in outcome.steps],
                "failure_counts": dict(fail_signature_counts),
                "last_actions": list(last_actions),
                "delivered_reply_ids": sorted(delivered),
                "next_iteration": next_iteration,
                "pending_action": pending_action,
                "model_call_inflight": model_call_inflight,
                "stop_reason": outcome.stop_reason,
            }
            if on_checkpoint:
                on_checkpoint(outcome.continuation)
            return outcome

        capture()

        for i in range(next_iteration, self.max_iterations + 1):
            assert_current_lease()
            if _flag(cancel_flag, "cancelled"):
                outcome.stop_reason = "cancel"
                return capture()
            if _flag(cancel_flag, "paused"):
                outcome.stop_reason = "paused"
                return capture()
            if plan is not None:
                plan.validate_execution_state()
                if _current_step(plan) is None and any(s.state not in ("done", "skipped") for s in plan.steps):
                    outcome.stop_reason = "plan_blocked"
                    return capture()

            if pending_action:
                suggestion = Action(**pending_action)
                pending_action = None
            else:
                try:
                    budget.check_model_call()
                    try:
                        model_id = adapter.profile().model_id
                    except Exception:
                        model_id = None
                    def checkpoint_dispatch(inflight):
                        nonlocal model_call_inflight
                        model_call_inflight = inflight
                        capture()
                    from ..core.runtime_budget import model_call
                    suggestion = model_call(budget, adapter, "suggest_action", messages,
                        tool_schemas, phase="react", on_pre_dispatch=checkpoint_dispatch)
                except BudgetExceeded as e:
                    outcome.stop_reason = f"budget:{e.dimension}"
                    return capture()
                assert_current_lease()
                model_call_inflight = False
            next_iteration = i + 1

            if _flag(cancel_flag, "cancelled"):
                outcome.stop_reason = "cancel"
                return capture()

            # 暂停在模型返回后生效：不执行新的工具调用，避免暂停后仍有副作用
            if _flag(cancel_flag, "paused"):
                outcome.steps.append(TraceStep(i, suggestion))
                outcome.stop_reason = "paused"
                return capture()

            # ---- 行动确认/推进 ----
            if suggestion.kind == "finish":
                outcome.finished = True
                outcome.answer = suggestion.answer or ""
                outcome.stop_reason = "normal"
                outcome.steps.append(TraceStep(i, suggestion))
                return capture()

            if suggestion.kind == "request_user":
                outcome.stop_reason = "needs_user"
                outcome.answer = suggestion.answer or ""
                outcome.steps.append(TraceStep(i, suggestion))
                messages.append(ChatMessage(role="assistant", content=outcome.answer))
                return capture()

            if plan is not None and _current_step(plan) is None:
                # A complete plan can still request a summary/clarification,
                # but provides no unfinished step for another tool operation.
                outcome.stop_reason = "plan_complete"
                return capture()
            if plan is not None and _tool_requires_unready_branch(plan, suggestion.tool):
                # Another independent branch being ready must not make a tool
                # from a branch with unmet prerequisites executable.
                outcome.stop_reason = "plan_blocked"
                return capture()

            # tool_call: 交由控制器执行（这里仅执行，验收在控制器）
            tool = suggestion.tool
            active_step = _current_step(plan) if plan is not None else None
            signature = f"{tool}:{_sig(suggestion.args)}"
            last_actions.append(signature)
            step = TraceStep(i, suggestion)
            outcome.steps.append(step)

            try:
                budget.check_tool_call(tool)
                result = self.tools.execute(tool or "", suggestion.args,
                                            budget=budget,
                                            allowed_tools=allowed_tools,
                                            task_id=logical_task_id, run_id=run_id,
                                            authorization_context={"scope": f"react:{i}"},
                                            effects=self.effects)
            except BudgetExceeded as e:
                outcome.stop_reason = f"budget:{e.dimension}"
                return capture()

            if result.error_class == "approval_required":
                pending_action, next_iteration = asdict(suggestion), i
                outcome.steps.pop()
                last_actions.pop()
                outcome.approval_ids = [result.evidence["approval_id"]]
                outcome.stop_reason = "approval_required"
                return capture()
            step.tool_result = result
            self._record_step_binding(result, active_step, task_id, run_id)
            payload = result.to_dict()
            messages.append(ChatMessage(
                role="assistant",
                content=_json_compact({"kind": suggestion.kind, "tool": tool,
                                       "args": suggestion.args})))
            messages.append(ChatMessage(
                role="tool", content=f"{tool}::{_json_compact(payload)}"))
            if self.bus:
                self.bus.emit(event_type="tool_call", task_id=logical_task_id,
                              run_id=run_id,
                              payload={"tool": tool, "ok": result.ok,
                                       "error_class": result.error_class})

            if result.side_effect_uncertain:
                outcome.stop_reason = "side_effect_unknown"
                return capture()

            if last_success(result):
                fail_signature_counts.clear()
                outcome.repeated_failures = 0
            if plan is not None and last_success(result) and _advance_plan(plan, tool):
                messages.append(ChatMessage(
                    role="system", content=_plan_progress_message(plan)))
                _ensure_current_running(plan)

            # ---- 防循环检测（PRD 12.3）----
            if not last_success(result):
                fail_sig = _fail_sig(tool, result, active_step.step_id if active_step else None)
                fail_signature_counts[fail_sig] = \
                    fail_signature_counts.get(fail_sig, 0) + 1
                outcome.repeated_failures = fail_signature_counts[fail_sig]
                if fail_signature_counts[fail_sig] >= self.max_repeat_failures:
                    if plan is not None:
                        _mark_step_failed(plan, tool)
                    outcome.stop_reason = "repeated_failure"
                    return capture()
            if last_actions[-3:] == [signature] * 3:
                outcome.stop_reason = "no_progress"
                return capture()
            capture()

        outcome.stop_reason = "max_iter"
        return capture()

    def _record_step_binding(self, result, active_step, task_id, run_id):
        """Bind verified gateway evidence; a cached observation is not a trigger."""
        if self.bindings is None or active_step is None:
            return
        evidence = result.evidence
        effect_id = evidence.get("effect_id")
        if not effect_id:
            return
        if not task_id:
            evidence["step_binding_skipped"] = "no_task_scope"
            return
        actual_tool = evidence.get("tool")
        if not active_step.tool_hint or active_step.tool_hint != actual_tool:
            evidence["step_binding_skipped"] = "step_tool_unmatched"
            return
        try:
            ledger = self.effects if self.effects is not None else self.tools.effects
            record = ledger.get(effect_id) if ledger is not None else None
            contract = self.tools.contract(actual_tool)
            if (not record or record["task_id"] != task_id
                    or record["tool_id"] != actual_tool or contract is None
                    or contract.side_effect_level == "none"):
                evidence["step_binding_skipped"] = "unverified_effect"
                return
            if evidence.get("replayed"):
                existing = self.bindings.get(effect_id)
                if (not existing or existing["task_id"] != task_id
                        or existing["step_id"] != active_step.step_id):
                    evidence["step_binding_skipped"] = "replayed_without_matching_binding"
                return
            if record["run_id"] != (run_id or task_id):
                evidence["step_binding_skipped"] = "effect_from_other_run"
                return
            self.bindings.bind(effect_id, active_step.step_id,
                               task_id=task_id, tool_id=actual_tool)
        except LeaseLost:
            raise  # Fencing is mandatory, not an optional evidence write.
        except Exception as exc:  # The external result must remain truthful.
            evidence["step_binding_error"] = f"{type(exc).__name__}: {exc}"


def last_success(result: Optional[ToolResult]) -> bool:
    return bool(result and result.ok and result.executed and result.contract_valid)


# ---------- 计划驱动 ----------

def _flag(obj: Optional[Any], name: str) -> bool:
    return bool(obj is not None and getattr(obj, name, False))


def _current_step(plan: ExecutionPlan) -> Optional[PlanStep]:
    by_id = {s.step_id: s for s in plan.steps}
    ready = [s for s in plan.steps if s.state in ("pending", "running")
             and all(dep in by_id and by_id[dep].state == "done" for dep in s.deps)]
    return next((s for s in ready if s.state == "running"), ready[0] if ready else None)


def _ensure_current_running(plan: ExecutionPlan) -> None:
    cur = _current_step(plan)
    if cur is not None and cur.state == "pending":
        cur.state = "running"


def _tool_requires_unready_branch(plan: ExecutionPlan, tool: Optional[str]) -> bool:
    by_id = {s.step_id: s for s in plan.steps}
    candidates = [s for s in plan.steps if s.tool_hint == tool
                  and s.state in ("pending", "running") and tool]

    if not candidates or any(
            all(by_id[dep].state == "done" for dep in step.deps)
            for step in candidates):
        return False
    current = _current_step(plan)
    if current is not None and current.tool_hint:
        return True

    # Legacy generic preparation steps have no tool binding and may legitimately
    # collect context with a tool also mentioned later. Do not infer their
    # identity from tool names, but never unlock a failed prerequisite branch.
    def failed_ancestor(step):
        todo, seen = list(step.deps), set()
        while todo:
            sid = todo.pop()
            if sid in seen:
                continue
            seen.add(sid)
            parent = by_id[sid]
            if parent.state in ("failed", "skipped"):
                return True
            if parent.state != "done":
                todo.extend(parent.deps)
        return False

    return all(failed_ancestor(step) for step in candidates)


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
    if cur.tool_hint and cur.tool_hint != tool:
        return  # An unrelated tool failure is not evidence this step failed.
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
                reason=f"步骤 {cur.step_id} 连续失败（{tool or 'unknown'}）",
                level="append")


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


def _settle_model_cost(budget: BudgetController, model: ModelAdapter,
                       messages: List[ChatMessage], suggestion: Action,
                       model_id: Optional[str]) -> None:
    """按本次调用已产生的计量结果结算，预登记不重复计费。

    - 实测/预估成本可用：按该金额结算；
    - 成本未知（价格档案缺失）：只记未知计数，**绝不按 0 元结算**；
    - 适配器未暴露计量结果（旧适配器）：退回字符数预估。
    """
    usage = getattr(model, "last_usage", None)
    if usage is not None and hasattr(usage, "cost") \
            and getattr(usage, "call_id", None):
        if usage.cost is None:
            marker = getattr(budget, "mark_unknown_model_cost", None)
            if callable(marker):
                marker(model_id)
            return
        budget.settle_model_cost(float(usage.cost), model_id)
        return
    budget.settle_model_cost(_estimate_cost(model, messages, suggestion),
                             model_id)


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


def _fail_sig(tool: Optional[str], result: Optional[ToolResult], step_id: Optional[str] = None) -> str:
    ec = result.error_class if result else "unknown"
    return _json_compact([step_id, tool, ec]) if step_id is not None else f"{tool}:{ec}"


def _json_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
