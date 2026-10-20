"""运行时控制器（Architecture 3.2 / 3.6 — PRD 4-6, 11-16）。

本组件是整个执行架构中拥有最终控制权的组件：
- 校验状态转换、验证模型行动、检查权限/预算、控制超时、管理并发/取消。
- 任务状态由运行时控制器管理（状态所有权，Arch 3.4）。
- Planning/ReAct/工作流不得绕过它直接获得不受约束的执行能力。
"""

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..acceptance.verify import Verdict, Verifier
from ..experts.registry import Expert, ExpertRegistry
from ..memory.context import AssembledContext, ContextAssembler
from ..memory.store import Credibility, MemoryItem, MemoryKind, MemoryStore, Scope
from ..models.base import ChatMessage, ModelAdapter
from ..models.router import ModelRouter
from ..planning.planner import ExecutionPlan, PlanningEngine
from ..react.engine import ReactEngine, ReactOutcome
from ..release.manager import ReleaseManager
from ..tools.contract import ToolResult
from ..tools.registry import ToolRegistry
from ..workflows.engine import WorkflowEngine
from ..workflows.models import WorkflowDef, WorkflowNodeState
from ..config import Config
from .budget import (BudgetController, BudgetExceeded, BudgetLimits,
                     BudgetUsage)
from .checkpoint import Checkpoint, CheckpointManager
from .events import EventBus, RunEvent
from .persistence import Database
from .state_machine import IllegalTransition, transition
from .task import RiskLevel, Task, TaskStatus


class RuntimeController:
    def __init__(self, config: Config,
                 db: Database, bus: EventBus,
                 tools: ToolRegistry, experts: ExpertRegistry,
                 planner: PlanningEngine, react: ReactEngine,
                 workflows: WorkflowEngine, models: ModelRouter,
                 memory: MemoryStore, assembler: ContextAssembler,
                 checkpoints: CheckpointManager,
                 permissions: Any, releases: Optional[ReleaseManager] = None,
                 wf_lookup: Optional[Callable[[str], Optional[WorkflowDef]]] = None,
                 diagnostics: Any = None, classifier: Any = None,
                 ledger: Optional[Any] = None):
        self.cfg = config
        self.db = db
        self.bus = bus
        self.tools = tools
        self.experts = experts
        self.planner = planner
        self.react = react
        self.workflows = workflows
        self.router = models
        self.memory = memory
        self.assembler = assembler
        self.cp_mgr = checkpoints
        self.permissions = permissions
        self.releases = releases
        self.wf_lookup = wf_lookup or (lambda wid: None)
        self.diagnostics = diagnostics
        self.classifier = classifier
        self.ledger = ledger
        # 并发管理（Arch 3.2 管理并发和取消）
        self._active: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._results: Dict[str, Any] = {}
        self._slots = threading.BoundedSemaphore(
            max(1, int(getattr(config.budget, "max_concurrent_tasks", 2))))

    # ================= 任务提交 =================
    def submit_task(self, goal: str, *,
                    intent: Optional[str] = None,
                    domain: Optional[str] = None,
                    constraints: Optional[List[str]] = None,
                    acceptance: Optional[List[Dict[str, Any]]] = None,
                    risk_level: RiskLevel = RiskLevel.LOW,
                    budget_overrides: Optional[Dict[str, Any]] = None,
                    wait: bool = True) -> Task:
        task = Task(user_goal=goal,
                    constraints=list(constraints or []),
                    acceptance_criteria=list(acceptance or []),
                    risk_level=risk_level,
                    budget_policy=dict(budget_overrides or {}))
        self._emit(task, "task_created", {"goal": goal})
        if wait:
            self.execute(task, intent_hint=intent, domain_hint=domain)
        else:
            threading.Thread(target=self.execute,
                             args=(task,), kwargs={"intent_hint": intent,
                                                   "domain_hint": domain},
                             daemon=True).start()
        return task

    # ================= 主执行编排骨架 =================
    def execute(self, task: Task, *, intent_hint: Optional[str] = None,
                domain_hint: Optional[str] = None) -> Task:
        run_id = f"run_{task.task_id}"
        self._register_active(task)
        started = time.time()
        version_manifest = self._version_manifest()
        task.version_manifest = version_manifest
        self._slots.acquire()   # 并发上限由控制器执行，不依赖调用方自觉（Arch 3.2）
        try:
            # 单任务硬预算在分类前建立；分类/规划/ReAct 的模型调用统一计量（PRD 55/56）
            budget = self._make_budget(task)
            # 日预算池：单任务合法但整体失控时仍应拒绝（PRD 55-58）
            if self.ledger is not None:
                try:
                    self.ledger.check(
                        "runtime",
                        max_model_calls=self.cfg.budget.runtime_daily_model_calls,
                        max_cost=self.cfg.budget.runtime_daily_cost)
                except BudgetExceeded as e:
                    task = self._move(task, TaskStatus.CLASSIFYING)
                    task.failure_reason = f"预算耗尽[{e.dimension}]: {e}"
                    task = self._move(task, TaskStatus.FAILED)
                    self._emit(task, "task_budget_exhausted",
                               {"dimension": e.dimension}, severity="warn")
                    self.db.save_task(task)
                    return task
            # 1) 分类（CREATED -> CLASSIFYING）
            task = self._classify(task, intent_hint, domain_hint, budget)
            if task.is_terminal() or task.task_status == TaskStatus.WAITING_USER:
                self.db.save_task(task)
                return task
            self._apply_expert_budget(task, budget)
            # 2) 规划（CLASSIFYING -> PLANNING 或 READY）
            task = self._plan(task, budget)
            if task.is_terminal():
                if task.task_status == TaskStatus.FAILED:
                    self._record_diagnostic(task, Verdict(passed=False), [], "")
                self._emit(task, "task_finalized", {"status": task.task_status.value})
                return task
            # 3) 执行（READY -> RUNNING -> VERIFYING）
            task = self._run(task, budget)
            # 4) 验收产出的最终状态写回持久层
            self.db.save_task(task)
        except IllegalTransition as e:
            task.task_status = TaskStatus.FAILED
            task.failure_reason = f"状态机冲突: {e}"
            self.db.save_task(task)
            self._record_diagnostic(task, Verdict(passed=False), [], "controller_error")
            self._emit(task, "task_failed", {"reason": task.failure_reason},
                       severity="error")
        except Exception as e:  # noqa: BLE001
            task.task_status = TaskStatus.FAILED
            task.failure_reason = f"控制器异常: {type(e).__name__}: {e}"
            self.db.save_task(task)
            self._record_diagnostic(task, Verdict(passed=False), [], "controller_error")
            self._emit(task, "task_failed", {"reason": task.failure_reason},
                       severity="error")
        finally:
            finished = time.time()
            self.db.save_run(run_id, task.task_id, started, finished,
                             task.task_status.value, version_manifest)
            self._unregister_active(task.task_id)
            self._slots.release()
        return task

    # ---- Phase 1: 分类 ----
    def _classify(self, task: Task, intent_hint: Optional[str],
                  domain_hint: Optional[str],
                  budget: BudgetController) -> Task:
        task = self._move(task, TaskStatus.CLASSIFYING)
        if self.classifier is not None:
            # 显式 hint 短路不调用模型；无 hint 时先做预算闸门
            if not intent_hint:
                try:
                    budget.check_model_call()
                except BudgetExceeded as e:
                    task.failure_reason = f"预算耗尽[{e.dimension}]"
                    self._emit(task, "task_budget_exhausted",
                               {"dimension": e.dimension}, severity="warn")
                    return self._move(task, TaskStatus.FAILED)
            intent, domain, conf, method = self.classifier.classify(
                task.user_goal, hint=intent_hint, domain_hint=domain_hint)
            if method in ("model", "heuristic_fallback"):
                budget.record_model_call(0.0)
            task.confidence = conf
            task.runtime_manifest["intent_classification"] = {
                "method": method, "confidence": conf}
        else:
            intent, domain, conf, method = intent_hint or "", domain_hint or "", \
                task.confidence, "route"
        expert: Expert = self.experts.route(intent or "general", domain or "")
        task.intent = intent or expert.expert_id
        task.domain = domain or domain_hint or expert.expert_id
        # PRD 12.3: 意图不明确时按风险采取不同策略
        risk = task.risk_level.value
        conf = task.confidence
        if conf < self.cfg.clarify_threshold and risk == "critical":
            task = self._move(task, TaskStatus.WAITING_USER)
            task.result = "关键歧义未消除，暂停等待用户澄清"
            return task
        return task

    # ---- Phase 2: 规划 ----
    def _plan(self, task: Task, budget: BudgetController) -> Task:
        # 已注册工作流直接就绪（PRD 11.3：固定流程不必重新发明计划）
        expert = self.experts.get(task.domain) or self.experts.route(task.intent)
        if task.workflow_ref is None and expert and expert.workflow_ids \
                and task.intent in expert.applicable_intents:
            task.workflow_ref = expert.workflow_ids[0]
        if task.workflow_ref:
            task = self._move(task, TaskStatus.READY)
            return task
        task = self._move(task, TaskStatus.PLANNING)
        model = self._route_model(task)
        try:
            plan = self.planner.make_plan(
                task.user_goal, intent=task.intent,
                constraints=task.constraints,
                acceptance=task.acceptance_criteria,
                model=model, budget=budget)
        except BudgetExceeded as e:
            task.failure_reason = f"预算耗尽[{e.dimension}]"
            self._emit(task, "task_budget_exhausted",
                       {"dimension": e.dimension}, severity="warn")
            return self._move(task, TaskStatus.FAILED)
        problems = plan.validate()
        if problems:
            task.failure_reason = "规划验收未通过: " + "; ".join(problems)
            task = self._move(task, TaskStatus.FAILED)
            return task
        task.execution_plan_ref = plan.plan_id
        task.runtime_manifest["execution_plan"] = plan.to_dict()
        task = self._move(task, TaskStatus.READY)
        return task

    # ---- Phase 3: 执行 ----
    def _run(self, task: Task, budget: BudgetController,
             *, start: bool = True) -> Task:
        if start:
            task = self._move(task, TaskStatus.RUNNING)
        if task.workflow_ref:
            return self._run_workflow(task, budget)
        return self._run_react(task, budget)

    def _run_react(self, task: Task, budget: BudgetController) -> Task:
        expert = self.experts.get(task.domain) or self.experts.route(task.intent)
        model = self._route_model(task)
        whitelist = expert.tool_whitelist if expert else None
        ctx = self.assembler.assemble(
            task.user_goal, intent=task.intent, domain=task.domain,
            constraints=task.constraints, acceptance=task.acceptance_criteria,
            status=task.task_status.value,
            tool_docs=self._tool_docs(whitelist),
            scope_key=task.task_id,
            max_chars=self._context_limit_chars(model))
        task.runtime_manifest["context"] = ctx.stats()
        quality = ctx.quality or {}
        if quality.get("overflow") or quality.get("untrusted_instructions") \
                or quality.get("constraint_retention", 1.0) < 1.0:
            self._emit(task, "context_quality_warning", quality,
                       severity="warn")
        messages = ctx.to_messages()
        if expert and expert.system_prompt:
            messages.insert(0, ChatMessage(role="system",
                                           content=expert.system_prompt))
        cancel_flag = self._active.get(task.task_id)
        plan = self._plan_from_task(task)
        outcome = self.react.run(
            messages, budget,
            allowed_tools=expert.tool_whitelist if expert else None,
            run_id=task.task_id, cancel_flag=cancel_flag,
            plan=plan, model=model)
        if plan is not None:
            task.runtime_manifest["execution_plan"] = plan.to_dict()
            task.runtime_manifest["plan_progress"] = _plan_progress(plan)
        if outcome.stop_reason == "cancel":
            if task.task_status != TaskStatus.CANCELLED:
                task = self._move(task, TaskStatus.CANCELLED)
            return task
        if outcome.stop_reason == "paused":
            task = self._move(task, TaskStatus.SUSPENDED)
            self.checkpoint(task, "paused", extras={
                "budget": budget.snapshot(),
                "plan": plan.to_dict() if plan else None})
            self._emit(task, "task_paused", {
                "plan_progress": task.runtime_manifest.get("plan_progress")})
            return task
        # 验收：Plan 完成 ≠ 实际完成（PRD 7.4），必须走独立验收
        task = self._move(task, TaskStatus.VERIFYING)
        # 收集本轮全部工具结果作为验收证据（PRD 15.4.8）
        tool_results = [s.tool_result for s in outcome.steps if s.tool_result]
        # 预算耗尽 → 明确失败原因（PRD 8：不得通过无限重试掩盖）
        budget_err = next((rt.error for rt in tool_results
                           if rt.error_class == "budget_exceeded"), None)
        if outcome.stop_reason.startswith("budget:"):
            budget_err = budget_err or f"预算停止: {outcome.stop_reason}"
        verdict = self._verify(task, outcome, tool_results)
        return self._finalize(task, verdict, outcome.answer, budget=budget,
                              failure_override=(f"预算耗尽: {budget_err}"
                                                if budget_err and not verdict.passed else None),
                              tool_results=tool_results,
                              stop_reason=outcome.stop_reason)

    def _run_workflow(self, task: Task, budget: BudgetController) -> Task:
        wf = self.wf_lookup(task.workflow_ref or "")
        if wf is None:
            task = self._move(task, TaskStatus.VERIFYING)
            verdict = Verdict(passed=False, checks=[{"type": "workflow",
                            "passed": False,
                            "error": f"工作流不存在: {task.workflow_ref}"}])
            return self._finalize(task, verdict, None, budget=budget)
        wf.created_from_task = task.task_id

        def _confirm(node_id: str) -> bool:
            decision = self.permissions.check_op("workflow_node_confirm",
                                                 {"node": node_id})
            return str(decision.value) == "allow"

        # 断点续跑：同一任务存在本版本工作流检查点时复用已完成节点输出
        completed = None
        cp = self.cp_mgr.latest(task.task_id)
        if (cp is not None and cp.workflow_version == wf.version
                and cp.task_state.get("workflow_outputs")):
            completed = cp.task_state["workflow_outputs"]

        result = self.workflows.run(
            wf, {"goal": task.user_goal}, budget=budget,
            cancel_flag=self._active.get(task.task_id), confirm=_confirm,
            completed=completed)
        if result.status == WorkflowNodeState.PAUSED:
            task = self._move(task, TaskStatus.SUSPENDED)
            self.db.save_task(task)
            self._emit(task, "task_paused", {
                "workflow": task.workflow_ref, "reason": result.error})
            return task
        task = self._move(task, TaskStatus.VERIFYING)
        if result.outputs:
            answer = "工作流完成: " + "; ".join(
                f"{k}={str(v)[:80]}" for k, v in result.outputs.items())
        elif result.error:
            answer = f"工作流失败: {result.error}"
        else:
            answer = "工作流无输出"
        verdict = Verdict(passed=result.ok,
                          checks=[{"type": "node", "node": s["node"],
                                   "passed": s["state"] == WorkflowNodeState.SUCCESS}
                                  for s in result.node_stats])
        if result.error:
            verdict.checks.append({"type": "workflow", "passed": False,
                                   "error": result.error})
        return self._finalize(task, verdict, answer, budget=budget)

    # ---- Phase 4: 验收与收尾 ----
    def _verify(self, task: Task, outcome: ReactOutcome,
                tool_results: List[ToolResult]) -> Verdict:
        """结果验收 + 独立验证信息（PRD 15.4.8）。"""
        if outcome.stop_reason == "needs_user":
            return Verdict(passed=False,
                           checks=[{"type": "user_clarification",
                                    "passed": False,
                                    "error": "需要用户澄清，任务挂起"}])
        return Verifier().check(task.acceptance_criteria,
                                answer=outcome.answer,
                                tool_results=tool_results)

    def _finalize(self, task: Task, verdict: Verdict, answer: Optional[str],
                  *, budget: BudgetController,
                  failure_override: Optional[str] = None,
                  tool_results: Optional[List[ToolResult]] = None,
                  stop_reason: str = "") -> Task:
        task.result = answer
        task.result_evidence = [{
            "verdict": verdict.summary(), "passed": verdict.passed,
            "checks": verdict.checks, "budget": budget.snapshot()}]
        if verdict.passed:
            task = self._move(task, TaskStatus.SUCCEEDED)
            self._remember(task, verified=True)
        else:
            failed_reason = failure_override or (
                verdict.checks[0].get("error", "验收未通过")
                if verdict.checks else "验收未通过")
            task.failure_reason = failed_reason
            task = self._move(task, TaskStatus.RECOVERING)
            # 故障处理：区分运行时恢复 vs 长期修复（PRD 45）
            task = self._move(task, TaskStatus.FAILED)
            self._remember(task, verified=False)
            self._record_diagnostic(task, verdict, tool_results or [], stop_reason)
        self.db.save_task(task)
        self._emit(task, "task_finalized", {
            "status": task.task_status.value, "verdict": verdict.summary(),
            "diagnostic_case_id": task.runtime_manifest.get("diagnostic_case_id")})
        return task

    def _record_diagnostic(self, task: Task, verdict: Verdict,
                           tool_results: List[ToolResult],
                           stop_reason: str) -> None:
        """失败任务自动沉淀为诊断案例（PRD 41-45）。诊断故障不得影响主流程。"""
        if self.diagnostics is None:
            return
        try:
            case = self.diagnostics.record_failure(
                task, verdict, tool_results, stop_reason=stop_reason)
        except Exception as e:  # noqa: BLE001
            self._emit(task, "diagnostic_failed",
                       {"error": f"{type(e).__name__}: {e}"}, severity="warn")
            return
        task.runtime_manifest["diagnostic_case_id"] = case.case_id
        self.db.save_task(task)

    # ================= 检查点 / 恢复 / 取消 =================
    def checkpoint(self, task: Task, stage: str,
                   extras: Optional[Dict[str, Any]] = None) -> str:
        cp = Checkpoint(task_id=task.task_id, current_stage=stage,
                        task_state=task.to_dict())
        if extras:
            cp.pending_side_effects = extras.get("pending_side_effects", [])
            cp.budget_state = extras.get("budget", {}) or {}
        # 已完成节点与下一步候选（PRD 34）
        cp.next_candidate_actions = [{"action": "resume"}]
        return self.cp_mgr.save(cp)

    def pause_task(self, task_id: str) -> Dict[str, Any]:
        """请求暂停运行中的 ReAct 任务；在下一个决策边界转入 SUSPENDED（PRD 36）。"""
        task = self.db.get_task(task_id)
        if task is None:
            return {"ok": False, "error": "任务不存在"}
        if task.workflow_ref:
            return {"ok": False, "error": "工作流任务暂不支持暂停"}
        flag = self._active.get(task_id)
        if flag is None:
            return {"ok": False, "error": "任务未在执行（仅运行中的任务可暂停）"}
        if task.task_status != TaskStatus.RUNNING:
            return {"ok": False, "error": f"任务状态 {task.task_status.value} 不可暂停"}
        flag.paused = True
        self._emit(task, "pause_requested", {})
        return {"ok": True, "task_id": task_id, "status": "PAUSE_REQUESTED"}

    def resume_task(self, task_id: str, *, force: bool = False,
                    wait: bool = True) -> Optional[Task]:
        """恢复暂停任务，或把失败任务作为新任务重跑（PRD 34-36）。

        恢复前检查副作用状态：存在结果未知的外部操作时必须人工核验并用
        force=True 显式确认，不盲目重放（PRD 35）。
        wait=False 时在后台执行，供 HTTP 接口立即返回。
        """
        task = self.db.get_task(task_id)
        if task is None:
            return None
        if task.task_status == TaskStatus.SUSPENDED:
            cp = self.cp_mgr.latest(task_id)
            pending = cp.pending_side_effects if cp else []
            if pending and not force:
                self._emit(task, "side_effect_state_unknown",
                           {"side_effects": pending}, severity="warn")
                raise PermissionError(
                    "存在结果未知的副作用；需人工核验后以 force=true 恢复")
            usage = None
            state = (cp.budget_state if cp else {}) or {}
            if state.get("usage"):
                u = state["usage"]
                usage = BudgetUsage(
                    model_calls=int(u.get("model_calls", 0)),
                    tool_calls=int(u.get("tool_calls", 0)),
                    cost=float(u.get("cost", 0.0)),
                    seconds=float(u.get("seconds", 0.0)))
            budget = self._make_budget(task, initial_usage=usage)
            self._apply_expert_budget(task, budget)
            if wait:
                return self._resume_suspended(task, budget, force)
            threading.Thread(target=self._resume_suspended,
                             args=(task, budget, force), daemon=True).start()
            return task
        if task.task_status == TaskStatus.FAILED:
            retry = self._build_retry(task)
            if wait:
                return self.execute(retry, intent_hint=task.intent or None,
                                    domain_hint=task.domain or None)
            threading.Thread(
                target=self.execute, args=(retry,),
                kwargs={"intent_hint": task.intent or None,
                        "domain_hint": task.domain or None},
                daemon=True).start()
            return retry
        if task.task_status in (TaskStatus.SUCCEEDED, TaskStatus.CANCELLED):
            raise ValueError(f"任务已是终态 {task.task_status.value}，不可恢复")
        if task.task_status == TaskStatus.RUNNING:
            return task
        raise ValueError(f"任务状态 {task.task_status.value} 不支持恢复")

    def _resume_suspended(self, task: Task, budget: BudgetController,
                          force: bool) -> Task:
        self._register_active(task)
        started = time.time()
        self._slots.acquire()
        try:
            task.task_status = transition(TaskStatus.SUSPENDED,
                                          TaskStatus.RUNNING)
            task.updated_at = time.time()
            self.db.save_task(task)
            self._emit(task, "task_resumed", {"forced": bool(force)})
            task = self._run(task, budget, start=False)
            self.db.save_task(task)
        except Exception as e:  # noqa: BLE001
            task.task_status = TaskStatus.FAILED
            task.failure_reason = f"恢复异常: {type(e).__name__}: {e}"
            self.db.save_task(task)
            self._record_diagnostic(task, Verdict(passed=False), [],
                                    "resume_error")
            self._emit(task, "task_failed",
                       {"reason": task.failure_reason}, severity="error")
        finally:
            self.db.save_run(f"run_{task.task_id}", task.task_id, started,
                             time.time(), task.task_status.value,
                             task.version_manifest)
            self._unregister_active(task.task_id)
            self._slots.release()
        return task

    def _build_retry(self, task: Task) -> Task:
        """失败任务重跑创建新任务（终态不可改写，PRD 36 / Arch 3.3）。"""
        retry = Task(user_goal=task.user_goal,
                     constraints=list(task.constraints),
                     acceptance_criteria=list(task.acceptance_criteria),
                     risk_level=task.risk_level,
                     budget_policy=dict(task.budget_policy),
                     intent=task.intent, domain=task.domain)
        retry.parent_task_id = task.task_id
        retry.runtime_manifest["resumed_from"] = task.task_id
        self._emit(retry, "task_created",
                   {"goal": retry.user_goal, "resumed_from": task.task_id})
        return retry

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        task = self.db.get_task(task_id)
        if task is None:
            return {"ok": False, "error": "任务不存在"}
        # 阻止后续非必要动作（PRD 36）
        flag = self._active.get(task_id)
        if flag is not None:
            flag.cancelled = True  # type: ignore[attr-defined]
        if task.is_terminal():
            return {"ok": False, "error": f"任务已是终态 {task.task_status.value}: 不可取消"}
        try:
            task.task_status = transition(task.task_status, TaskStatus.CANCELLED)
        except IllegalTransition as e:
            return {"ok": False, "error": str(e)}
        self.db.save_task(task)
        self._emit(task, "task_cancelled", {})
        return {"ok": True, "task_id": task_id, "status": task.task_status.value}

    # ================= 内部工具 =================
    def _make_budget(self, task: Task,
                     initial_usage: Optional[BudgetUsage] = None
                     ) -> BudgetController:
        return BudgetController(BudgetLimits(), dict(task.budget_policy or {}),
                                initial_usage=initial_usage,
                                ledger=self.ledger, ledger_scope="runtime")

    def _apply_expert_budget(self, task: Task,
                             budget: BudgetController) -> None:
        scope = self.experts.get(task.domain) or self.experts.route(task.intent)
        if scope and scope.budget_class == "generous":
            budget.limits.max_model_calls *= 2
            budget.limits.max_cost *= 2

    def _route_model(self, task: Task) -> Any:
        """按意图/领域选择模型并记录路由结果（PRD 19）。"""
        try:
            model = self.router.route(task.intent or "general",
                                      task.domain or "general")
        except Exception:  # noqa: BLE001 —— 路由失败回退默认适配器
            model = self.react.model
        try:
            profile = model.profile()
        except Exception:  # noqa: BLE001
            profile = None
        task.runtime_manifest["model_route"] = {
            "model_id": getattr(profile, "model_id", None),
            "intent": task.intent, "domain": task.domain}
        return model

    def _context_limit_chars(self, model: Any) -> int:
        """上下文预算同时受配置与模型窗口约束（Arch 8.5）。"""
        limit = int(getattr(self.cfg, "context_max_chars", 6000))
        try:
            context_limit = int(model.profile().context_limit)
            # 粗换算：1 token 约 2 字符，留出输出余量
            limit = min(limit, max(800, context_limit * 2))
        except Exception:  # noqa: BLE001 —— 无档案时用配置默认值
            pass
        return limit

    def _tool_docs(self, whitelist: Optional[List[str]]) -> List[str]:
        """必要工具说明按权限白名单进入上下文（Arch 8.5 第 5 优先级）。"""
        docs: List[str] = []
        for spec in self.tools.schema_for_model(whitelist):
            schema = spec.get("input_schema") or {}
            props = schema.get("properties") or {}
            args = ", ".join(props.keys()) or "无参数"
            docs.append(f"{spec['tool_id']}({args}): {spec.get('description', '')}")
        return docs

    @staticmethod
    def _plan_from_task(task: Task) -> Optional[ExecutionPlan]:
        raw = task.runtime_manifest.get("execution_plan")
        if not isinstance(raw, dict):
            return None
        try:
            return ExecutionPlan.from_dict(raw)
        except Exception:  # noqa: BLE001 —— 旧任务/损坏计划不影响执行
            return None

    def _version_manifest(self) -> Dict[str, Any]:
        if self.releases and self.releases.current_release_id:
            m = self.releases.manifests.get(self.releases.current_release_id)
            if m:
                return {"release_id": m.release_id, "version": m.version,
                        "artifact_digest": m.artifact_digest}
        return {"release_id": None, "version": "unreleased"}

    def _move(self, task: Task, target: TaskStatus) -> Task:
        task.task_status = transition(task.task_status, target)
        task.updated_at = time.time()
        if target in (TaskStatus.RUNNING, TaskStatus.VERIFYING):
            self.checkpoint(task, target.value)
        self.db.save_task(task)
        self._emit(task, "state_transition", {"to": target.value})
        return task

    def _remember(self, task: Task, verified: bool) -> None:
        """执行结果沉淀为记忆（凭证据决定可信状态；原始日志不自动成为经验）。"""
        content = f"任务<{task.intent}>: {task.user_goal} -> " + \
                  (task.result or task.failure_reason or "")
        item = MemoryItem(
            content=content[:300],
            kind=MemoryKind.RUNTIME_EXPERIENCE,
            scope=Scope.EXPERT,
            scope_key=task.domain,
            status=Credibility.VERIFIED if verified else Credibility.CANDIDATE,
            source="runtime",
            applicable_domains=[task.domain],
            evidence={"task_id": task.task_id,
                      "verdict": verdict_of(task)},
            valid_until=(time.time() + 7 * 86400) if not verified else None,
        )
        self.memory.write(item)

    def _emit(self, task: Task, event_type: str,
              payload: Dict[str, Any], severity: str = "info") -> RunEvent:
        return self.bus.emit(event_type=event_type, task_id=task.task_id,
                             run_id=f"run_{task.task_id}",
                             component_id="runtime_controller",
                             severity=severity, payload=payload)

    def register_active(self, task_id: str) -> None:
        self._register_active(Task(task_id=task_id))

    def _register_active(self, task: Task) -> None:
        class _Flag:
            cancelled = False
            paused = False
        with self._lock:
            self._active[task.task_id] = _Flag()

    def _unregister_active(self, task_id: str) -> None:
        with self._lock:
            self._active.pop(task_id, None)


def verdict_of(task: Task) -> Dict[str, Any]:
    for ev in task.result_evidence:
        if "verdict" in ev:
            return ev
    return {}


def _plan_progress(plan: ExecutionPlan) -> Dict[str, Any]:
    states = {s.step_id: s.state for s in plan.steps}
    return {
        "steps": states,
        "done": [k for k, v in states.items() if v == "done"],
        "failed": [k for k, v in states.items() if v == "failed"],
        "pending": [k for k, v in states.items() if v in ("pending", "running")],
        "change_reasons": list(plan.change_reasons),
    }
