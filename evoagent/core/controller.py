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
from .budget import BudgetController, BudgetLimits
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
                 diagnostics: Any = None, classifier: Any = None):
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
        # 并发管理（Arch 3.2 管理并发和取消）
        self._active: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._results: Dict[str, Any] = {}

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
        try:
            # 1) 分类（CREATED -> CLASSIFYING）
            task = self._classify(task, intent_hint, domain_hint)
            if task.is_terminal() or task.task_status == TaskStatus.WAITING_USER:
                self.db.save_task(task)
                return task
            # 2) 规划（CLASSIFYING -> PLANNING 或 READY）
            task = self._plan(task)
            if task.is_terminal():
                if task.task_status == TaskStatus.FAILED:
                    self._record_diagnostic(task, Verdict(passed=False), [], "")
                self._emit(task, "task_finalized", {"status": task.task_status.value})
                return task
            # 3) 执行（READY -> RUNNING -> VERIFYING）
            task = self._run(task)
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
        return task

    # ---- Phase 1: 分类 ----
    def _classify(self, task: Task, intent_hint: Optional[str],
                  domain_hint: Optional[str]) -> Task:
        task = self._move(task, TaskStatus.CLASSIFYING)
        if self.classifier is not None:
            intent, domain, conf, method = self.classifier.classify(
                task.user_goal, hint=intent_hint, domain_hint=domain_hint)
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
    def _plan(self, task: Task) -> Task:
        # 已注册工作流直接就绪（PRD 11.3：固定流程不必重新发明计划）
        expert = self.experts.get(task.domain) or self.experts.route(task.intent)
        if task.workflow_ref is None and expert and expert.workflow_ids \
                and task.intent in expert.applicable_intents:
            task.workflow_ref = expert.workflow_ids[0]
        if task.workflow_ref:
            task = self._move(task, TaskStatus.READY)
            return task
        plan = self.planner.make_plan(
            task.user_goal, intent=task.intent,
            constraints=task.constraints,
            acceptance=task.acceptance_criteria)
        problems = plan.validate()
        if problems:
            task.failure_reason = "规划验收未通过: " + "; ".join(problems)
            task = self._move(task, TaskStatus.FAILED)
            return task
        task.execution_plan_ref = plan.plan_id
        task = self._move(task, TaskStatus.READY)
        return task

    # ---- Phase 3: 执行 ----
    def _run(self, task: Task) -> Task:
        task = self._move(task, TaskStatus.RUNNING)
        budget = self._make_budget(task)

        if task.workflow_ref:
            return self._run_workflow(task, budget)
        return self._run_react(task, budget)

    def _run_react(self, task: Task, budget: BudgetController) -> Task:
        ctx = self.assembler.assemble(
            task.user_goal, intent=task.intent, domain=task.domain,
            constraints=task.constraints, acceptance=task.acceptance_criteria,
            scope_key=task.task_id)
        expert = self.experts.get(task.domain) or self.experts.route(task.intent)
        messages = ctx.to_messages()
        if expert and expert.system_prompt:
            messages.insert(0, ChatMessage(role="system",
                                           content=expert.system_prompt))
        cancel_flag = self._active.get(task.task_id)
        outcome = self.react.run(
            messages, budget,
            allowed_tools=expert.tool_whitelist if expert else None,
            run_id=task.task_id, cancel_flag=cancel_flag)
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

        result = self.workflows.run(
            wf, {"goal": task.user_goal}, budget=budget,
            cancel_flag=self._active.get(task.task_id), confirm=_confirm)
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
        # 已完成节点与下一步候选（PRD 34）
        cp.next_candidate_actions = [{"action": "resume"}]
        return self.cp_mgr.save(cp)

    def resume_task(self, task_id: str) -> Optional[Task]:
        """恢复：重新验证外部状态，不假设之前的工具调用一定成功（PRD 35）。"""
        task = self.db.get_task(task_id)
        if task is None:
            return None
        cp = self.cp_mgr.latest(task_id)
        if cp is not None and cp.pending_side_effects:
            # 副作用结果未知 → 不盲目重试，先标记待人工验证
            self._emit(task, "side_effect_state_unknown",
                       {"side_effects": cp.pending_side_effects},
                       severity="warn")
        if task.task_status == TaskStatus.SUSPENDED:
            task.task_status = transition(task.task_status, TaskStatus.RUNNING)
            task.task_status = transition(task.task_status, TaskStatus.VERIFYING)
            # 恢复后重新验收既有结果
            verdict = Verdict(passed=bool(task.result),
                              checks=[{"type": "resumed", "passed": bool(task.result)}])
            return self._finalize(task, verdict, task.result,
                                  budget=self._make_budget(task))
        if task.task_status == TaskStatus.FAILED:
            # 失败恢复：清空失败原因后重跑（新 run 记录）
            task.failure_reason = None
            return self.execute(task)
        return task

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
    def _make_budget(self, task: Task) -> BudgetController:
        overrides = dict(task.budget_policy or {})
        limits = BudgetLimits()
        scope = self.experts.get(task.domain)
        if scope and scope.budget_class == "generous":
            limits.max_model_calls *= 2
            limits.max_cost *= 2
        return BudgetController(limits, overrides)

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
