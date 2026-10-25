"""运行时控制器（Architecture 3.2 / 3.6 — PRD 4-6, 11-16）。

本组件是整个执行架构中拥有最终控制权的组件：
- 校验状态转换、验证模型行动、检查权限/预算、控制超时、管理并发/取消。
- 任务状态由运行时控制器管理（状态所有权，Arch 3.4）。
- Planning/ReAct/工作流不得绕过它直接获得不受约束的执行能力。
"""

import copy
import datetime
import hashlib
import json
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .. import __version__ as CORE_VERSION
from ..acceptance.verify import Verdict, Verifier
from ..experts.registry import Expert, ExpertRegistry
from ..memory.context import AssembledContext, ContextAssembler
from ..memory.store import Credibility, MemoryItem, MemoryKind, MemoryStore, Scope
from ..models.base import ChatMessage, ModelAdapter
from ..models.metering import adapter_model_id, settle_adapter_cost
from ..models.router import ModelRouter
from ..planning.planner import ExecutionPlan, PlanningEngine, PlanRevisionError
from ..react.engine import ReactEngine, ReactOutcome
from ..release.manager import ReleaseManager
from ..tools.contract import ToolResult
from ..tools.registry import ToolRegistry
from ..assets.entity import AssetError
from ..workflows.engine import WorkflowEngine
from ..workflows.models import WorkflowDef, WorkflowNodeState
from ..config import Config
from .budget import (BudgetController, BudgetExceeded, BudgetLimits,
                     BudgetUsage)
from .approvals import ApprovalStore  # F-04 审批记录
from .checkpoint import Checkpoint, CheckpointManager
from .side_effects import SideEffectLedger  # F-03 副作用账本
from .events import EventBus, RunEvent
from .persistence import Database
from .state_machine import IllegalTransition, transition
from .runs import RunManager
from .leases import LeaseLost, RecoveryBlocked
from .task import GoalOrigin, ORIGIN_VALUES, RiskLevel, Task, TaskStatus

# 依赖网络的工具：未在契约里声明网络权限时用它推断 PRD 7.3 的 requires_network
_NETWORK_TOOLS = {"http_get"}


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
                 ledger: Optional[Any] = None,
                 audit: Any = None, memory_bindings=None,
                 asset_binder: Any = None):
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
        from ..memory.binding import MemoryBindingAuthority
        self.memory_bindings = memory_bindings or MemoryBindingAuthority(db, config.memory_bindings)
        self.cp_mgr = checkpoints
        self.runs = RunManager(db)
        self.side_fx = self.tools.effects or SideEffectLedger(db)
        self.tools.effects = self.side_fx
        self.react.effects = self.side_fx  # ReAct/DAG 共用执行前记账网关
        self.permissions = permissions
        self.releases = releases
        self.wf_lookup = wf_lookup or (lambda wid: None)
        self.diagnostics = diagnostics
        self.classifier = classifier
        self.ledger = ledger
        self.runtime_budgets = None
        if ledger is not None and ledger.db is not None:
            from .runtime_budget import RuntimeBudgets
            self.runtime_budgets = RuntimeBudgets(ledger, config.budget, bus=bus)
            self.runtime_budgets.sweep()
        self.audit = audit  # S-05 执行环节审计（可选）
        # ALN-06-R：运行时精确消费绑定（可选）。注入后执行步冻结清单时
        # 绑定专家/工作流的精确资产版本（ref+digest），恢复/续跑只验证
        # 不重绑；未注入时行为与既有完全一致。
        self.asset_binder = asset_binder
        # 并发管理（Arch 3.2 管理并发和取消）
        self._active: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._results: Dict[str, Any] = {}
        self._slots = threading.BoundedSemaphore(
            max(1, int(getattr(config.budget, "max_concurrent_tasks", 2))))
        self.tools.expert_scope = self._expert_tools_for_task

    def _expert_tools_for_task(self, task_id):
        task = self.db.get_task(task_id)
        if task is None:
            return None
        expert = self.experts.get(task.domain) or self.experts.route(task.intent)
        if expert and expert.tool_whitelist is None:
            return None
        return list(expert.tool_whitelist) if expert else []

    # ================= 任务提交 =================
    def submit_task(self, goal: str, *,
                    intent: Optional[str] = None,
                    domain: Optional[str] = None,
                    constraints: Optional[List[str]] = None,
                    acceptance: Optional[List[Dict[str, Any]]] = None,
                    risk_level: RiskLevel = RiskLevel.LOW,
                    budget_overrides: Optional[Dict[str, Any]] = None,
                    wait: bool = True,
                    source: str = "user",
                    model_override: Optional[str] = None,
                    target_object: str = "",
                    expected_output: str = "",
                    complexity: str = "",
                    required_tools: Optional[List[str]] = None,
                    required_permissions: Optional[List[str]] = None,
                    requires_network: Optional[bool] = None,
                    workflow_suitable: Optional[bool] = None,
                    open_questions: Optional[List[str]] = None,
                    device_id: Optional[str] = None,
                    installation_id: Optional[str] = None,
                    app_version: Optional[str] = None,
                    goal_id: Optional[str] = None,
                    origin: str = GoalOrigin.USER.value,
                    goal_action: str = "execute",
                    goal_command_id: Optional[str] = None,
                    authorization_scope: Optional[Dict[str, Any]] = None,
                    manifest_extras: Optional[Dict[str, Any]] = None,
                    principal_id: str = "local:host",
                    memory_context: Optional[Dict[str, str]] = None,
                    memory_grant_ref: Optional[str] = None) -> Task:
        if origin not in ORIGIN_VALUES:
            raise ValueError(
                f"非法 origin: {origin}（契约 §2: user|scheduled|derived|opportunity）")
        task = Task(user_goal=goal,
                    goal_id=goal_id,
                    origin=origin,
                    target_object=target_object,
                    expected_output=expected_output,
                    complexity=complexity,
                    required_tools=list(required_tools or []),
                    required_permissions=list(required_permissions or []),
                    requires_network=requires_network,
                    workflow_suitable=workflow_suitable,
                    open_questions=list(open_questions or []),
                    constraints=list(constraints or []),
                    acceptance_criteria=list(acceptance or []),
                    risk_level=risk_level,
                    budget_policy=dict(budget_overrides or {}))
        # 评测任务与真实运行分离：发布后监测不得把评测样本计入（PRD 82）
        task.runtime_manifest["source"] = source
        # 迁移回归需固定目标模型执行，不能用常规路由结果代替（PRD 20）
        if model_override:
            task.runtime_manifest["model_override"] = model_override
        # 04-V：提交身份进入运行清单——授权发放与消费都以它为准
        if principal_id and principal_id != "local:host":
            task.runtime_manifest["principal_id"] = principal_id
        # 运行可追溯到安装实例与客户端版本（双端协同 §3.1）
        if device_id or installation_id or app_version:
            task.runtime_manifest["device"] = {
                "device_id": device_id or None,
                "installation_id": installation_id or None,
                "app_version": app_version or None,
            }
        # GAP ⑤c：调用方预置的运行时清单事实（如评测套件引用），随任务
        # 保存并在 execute 快照 merge 时并入 run manifest
        if manifest_extras:
            task.runtime_manifest.update(manifest_extras)
        self.memory_bindings.bind_new(task, principal_id, memory_context, memory_grant_ref)
        if goal_id:
            from ..goals import GoalStore
            bound = GoalStore(self.db).bind_task(task, goal_action,
                command_id=goal_command_id, request_policy=authorization_scope)
            if bound.task_id != task.task_id:
                self.memory_bindings.check_replay(bound, principal_id, memory_context, memory_grant_ref)
                return bound
        elif origin != GoalOrigin.USER.value:
            raise PermissionError("自主来源任务必须关联已授权Goal")
        self._emit(task, "task_created",
                   {"goal": goal, "goal_id": task.goal_id,
                    "origin": task.origin})
        # 后台执行时接口会立即返回 202；任务必须先落库，避免客户端紧接着查询 404
        self.db.save_task(task)
        if authorization_scope is not None:
            # Caller-specified scope only narrows the expert/system intersection
            # enforced later; parsed required_* fields never grant authority.
            task.authorization_ref = self.tools.authorizations.issue(
                task.task_id, authorization_scope, actor="task:submit")
            self.db.save_task(task)
        if wait:
            task = self.execute(task, intent_hint=intent, domain_hint=domain)
        else:
            threading.Thread(target=self.execute,
                             args=(task,), kwargs={"intent_hint": intent,
                                                   "domain_hint": domain},
                             daemon=True).start()
        return task

    # ================= 主执行编排骨架 =================
    def execute(self, task: Task, *, intent_hint: Optional[str] = None,
                domain_hint: Optional[str] = None) -> Task:
        if task.task_status != TaskStatus.CREATED:
            raise ValueError("仅新任务可执行；已有任务请使用恢复入口")
        self.memory_bindings.resolve(task)
        self._register_active(task)
        version_manifest = self._version_manifest()
        device = task.runtime_manifest.get("device") or {}
        version_manifest.update({
            "task_id": task.task_id,
            "memory_binding_ref": task.memory_binding_ref,
            "memory_identity": dict(task.memory_identity),
            "device_id": device.get("device_id"),
            "installation_id": device.get("installation_id"),
            "app_version": device.get("app_version"),
        })
        # GAP ⑤c：workflow_ref 契约形态 = {id, version} 对象或 null
        # （任务级 task.workflow_ref 保持字符串 id，不动其既有比较逻辑）
        if task.workflow_ref:
            wf = self.wf_lookup(task.workflow_ref)
            version_manifest["workflow_ref"] = {
                "id": task.workflow_ref,
                "version": getattr(wf, "version", None) if wf else None}
        # GAP ⑤c：任务级运行时清单快照并入 run manifest（source/
        # model_override/device/manifest_extras 等执行前已知事实）；
        # 已算出的契约实值不覆盖，为 null 的契约字段（如
        # evaluation_suite_ref）允许被运行前已知事实补齐
        for k, v in (task.runtime_manifest or {}).items():
            if k not in version_manifest or version_manifest[k] is None:
                version_manifest[k] = v
        try:
            run_id, started = self.runs.begin(task, version_manifest)
        except BaseException:
            self._unregister_active(task.task_id)
            raise
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
        except LeaseLost:
            task = self.db.get_task(task.task_id) or task
        except AssetError as e:
            task.task_status = TaskStatus.FAILED
            task.failure_reason = f"资产消费校验失败: {e}"
            self.db.save_task(task)
            self._record_diagnostic(task, Verdict(passed=False), [],
                                    "asset_binding_rejected")
            self._emit(task, "task_asset_binding_rejected",
                       {"reason": str(e)}, severity="warn")
        except BudgetExceeded as e:
            task.task_status = TaskStatus.FAILED
            task.failure_reason = f"预算耗尽[{e.dimension}]: {e}"
            self.db.save_task(task)
            self._emit(task, "task_budget_exhausted",
                       {"dimension": e.dimension}, severity="warn")
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
            try:
                self.runs.finish(task, run_id, started)
            finally:
                self._unregister_active(task.task_id)
                self._slots.release()
        return task

    # ---- Phase 1: 分类 ----
    def _classify(self, task: Task, intent_hint: Optional[str],
                  domain_hint: Optional[str],
                  budget: BudgetController) -> Task:
        if task.task_status != TaskStatus.CLASSIFYING:
            task = self._move(task, TaskStatus.CLASSIFYING)
        if self.classifier is not None:
            # 显式 hint 短路不调用模型；无 hint 时先做预算闸门。
            # ALN-23-A：general 提示不能无条件屏蔽真实分类——视为未指定，
            # 交给分类器确认或覆盖（分类器回落仍是 general，语义不变）。
            hint_for_classify = (None if intent_hint == "general"
                                 else intent_hint)
            adapter = getattr(self.classifier, "model", None)
            model_id = None
            if not hint_for_classify:
                try:
                    budget.check_model_call()
                except BudgetExceeded as e:
                    task.failure_reason = f"预算耗尽[{e.dimension}]"
                    self._emit(task, "task_budget_exhausted",
                               {"dimension": e.dimension}, severity="warn")
                    return self._move(task, TaskStatus.FAILED)
                if adapter is not None:
                    model_id = adapter_model_id(adapter)
            goal = task.user_goal
            if task.user_answers:
                goal += "\n用户补充：\n" + "\n".join(a["answer"] for a in task.user_answers)
            from .runtime_budget import CLASSIFICATION_BUDGET
            token = CLASSIFICATION_BUDGET.set(budget)
            try:
                intent, domain, conf, method = self.classifier.classify(
                    goal, hint=hint_for_classify, domain_hint=domain_hint)
            finally:
                CLASSIFICATION_BUDGET.reset(token)
            task.confidence = conf
            task.runtime_manifest["intent_classification"] = {
                "method": method, "confidence": conf}
        else:
            intent, domain, conf, method = intent_hint or "", domain_hint or "", \
                task.confidence, "route"
        expert: Expert = self.experts.route(intent or "general", domain or "")
        task.intent = intent or expert.expert_id
        # 模型给出的 domain 可能不是已注册专家；归一化以保持预算池/记忆作用域一致
        task.domain = (domain if domain and self.experts.get(domain)
                       else expert.expert_id)
        # 领域专家预算池（PRD 55）：任务按领域累计，超限由账本拒绝
        budget.bind_pool(
            f"expert:{task.domain}",
            max_model_calls=self.cfg.budget.expert_daily_model_calls,
            max_cost=self.cfg.budget.expert_daily_cost)
        # PRD 12.3: 意图不明确时按风险采取不同策略
        risk = task.risk_level.value
        conf = task.confidence
        if conf < self.cfg.clarify_threshold and risk == "critical":
            return self._wait_for_user(task, budget,
                "关键歧义未消除，请补充目标、操作对象和期望结果", "classification")
        return task

    # ---- Phase 2: 规划 ----
    def _plan(self, task: Task, budget: BudgetController) -> Task:
        # 已注册工作流直接就绪（PRD 11.3：固定流程不必重新发明计划）
        expert = self.experts.get(task.domain) or self.experts.route(task.intent)
        if task.workflow_ref is None and expert and expert.workflow_ids \
                and task.intent in expert.applicable_intents:
            task.workflow_ref = expert.workflow_ids[0]
        if task.workflow_ref:
            wf = self.wf_lookup(task.workflow_ref)
            hints = [n.tool for n in wf.nodes] if wf is not None else []
            self._enrich_task_model(task, hints, len(hints),
                                    has_workflow=True)
            task = self._move(task, TaskStatus.READY)
            return task
        task = self._move(task, TaskStatus.PLANNING)
        model = self._route_model(task)
        self._bind_model_pool(budget, model)
        try:
            plan = self.planner.make_plan(
                task.user_goal + ("\n用户补充：\n" + "\n".join(
                    a["answer"] for a in task.user_answers) if task.user_answers else ""),
                intent=task.intent,
                constraints=task.constraints,
                acceptance=task.acceptance_criteria,
                model=model, budget=budget)
            plan.original_goal = task.user_goal
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
        self._enrich_task_model(task, [s.tool_hint for s in plan.steps],
                                len(plan.steps))
        task = self._move(task, TaskStatus.READY)
        return task

    def _enrich_task_model(self, task: Task, tool_hints: List[str],
                           step_count: int, *, has_workflow: bool = False) -> None:
        """补齐 PRD 7.3 解析结果中可从计划/工具契约确定的字段。

        只填空值：显式输入（API/CLI）优先，不被推断覆盖。
        """
        hints = [h for h in tool_hints if h]
        if not task.required_tools:
            task.required_tools = sorted(set(hints))
        perms: List[str] = []
        network = False
        for tid in (task.required_tools or hints):
            contract = self.tools.contract(tid)
            if contract is None:
                continue
            required = list(contract.permission_requirements or [])
            perms.extend(required)
            if tid in _NETWORK_TOOLS or "network" in required:
                network = True
        if not task.required_permissions:
            task.required_permissions = sorted(set(perms))
        if task.requires_network is None:
            task.requires_network = network
        if not task.complexity:
            task.complexity = ("simple" if step_count <= 2 else
                               "moderate" if step_count <= 5 else "complex")
        if task.workflow_suitable is None:
            expert = (self.experts.get(task.domain)
                      or self.experts.route(task.intent))
            task.workflow_suitable = bool(
                has_workflow or (expert and expert.workflow_ids))

    # ---- Phase 3: 执行 ----
    def _run(self, task: Task, budget: BudgetController,
             *, start: bool = True) -> Task:
        if self.tools.authorizations is not None:
            grant = self.tools.authorizations.current(task.task_id)
            if grant is None:
                if task.authorization_ref:
                    raise PermissionError("任务引用的授权已丢失，禁止重新发放")
                expert = self.experts.get(task.domain) or self.experts.route(task.intent)
                contracts = self.tools.available_tools(expert.tool_whitelist if expert else None)
                tool_ids = [c.tool_id for c in contracts]
                workflow = self.wf_lookup(task.workflow_ref) if task.workflow_ref else None
                if workflow and any(n.fn and n.requires_confirmation for n in workflow.nodes) and (
                        expert is None or expert.tool_whitelist is None or "workflow_node_confirm" in expert.tool_whitelist):
                    tool_ids.append("workflow_node_confirm")
                task.authorization_ref = self.tools.authorizations.issue(task.task_id,
                    {"tools": tool_ids,
                     "permissions": sorted({p for c in contracts for p in c.permission_requirements})},
                    actor="system:expert-scope",
                    principal=(task.runtime_manifest or {}).get("principal_id"))
                self.db.save_task(task)
            else:
                task.authorization_ref = grant["authorization_ref"]
            task.runtime_manifest["authorization_ref"] = task.authorization_ref
        if start:
            task = self._move(task, TaskStatus.RUNNING)
        if task.workflow_ref:
            return self._run_workflow(task, budget)
        return self._run_react(task, budget)

    def _run_react(self, task: Task, budget: BudgetController) -> Task:
        # A present but corrupt plan must not silently become unplanned execution.
        plan = self._plan_from_task(task)
        expert = self.experts.get(task.domain) or self.experts.route(task.intent)
        # E-07 能力缺口信号源：意图与领域均无专属能力，解析落到
        # general 且该意图未在 general 的适用声明中——能力目录缺口，
        # 发 expert_fallback 事件（dedup/阈值在 SignalCollector）
        if (expert is not None and expert.expert_id == "general"
                and task.intent
                not in (getattr(expert, "applicable_intents", None) or [])):
            self._emit(task, "expert_fallback", {
                "intent": task.intent,
                "expert_id": getattr(expert, "expert_id", "") or "",
            })
        model = self._route_model(task)
        self._freeze_execution_manifest(task, expert=expert, model=model)
        self._bind_model_pool(budget, model)
        whitelist = expert.tool_whitelist if expert else None
        hive_key = (expert.memory_scope_key if expert and expert.memory_scope_key else task.domain)
        identity, grant = self.memory_bindings.resolve(task, hive_key)
        ctx = self.assembler.assemble(
            task.user_goal, intent=task.intent, domain=task.domain,
            constraints=task.constraints, acceptance=task.acceptance_criteria,
            status=task.task_status.value,
            tool_docs=self._tool_docs(whitelist),
            # GAP 死字段修复③：读侧用专家 memory_scope_key（与 _remember
            # 写侧同键），专家域记忆真正隔离在专属蜂巢内
            identity=identity, grant=grant,
            include_memory=task.runtime_manifest.get("source") != "evaluation",
            max_chars=self._context_limit_chars(model))
        task.runtime_manifest["context"] = ctx.stats()
        self.db.save_context_manifest(task.task_id, task.current_run_id, ctx.manifest)
        task.runtime_manifest["context_manifest_ref"] = ctx.manifest.manifest_id
        self._emit(task, "context_assembled", {
            "manifest_id":ctx.manifest.manifest_id, "digest":ctx.manifest.digest,
            "memory_binding_ref":task.memory_binding_ref,
            "memory_candidates":ctx.memory_candidates, "memory_used":ctx.memory_used})
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
        cp = self.cp_mgr.latest(task.task_id)

        def save_progress(state):
            if plan is not None:
                task.runtime_manifest["execution_plan"] = plan.to_dict()
                task.runtime_manifest["plan_progress"] = _plan_progress(plan)
            self.checkpoint(task, "react_boundary", extras={
                "budget": budget.snapshot(), "react_state": state,
                "pending_side_effects": self.side_fx.pending(task.task_id)})
            self.db.save_task(task)

        outcome = self.react.run(
            messages, budget,
            allowed_tools=expert.tool_whitelist if expert else None,
            run_id=task.current_run_id, task_id=task.task_id,
            cancel_flag=cancel_flag,
            plan=plan, model=model,
            restored_state=cp.react_state if cp else None,
            user_replies=task.user_answers, on_checkpoint=save_progress,
            principal=(task.runtime_manifest or {}).get("principal_id"))
        if plan is not None:
            task.runtime_manifest["execution_plan"] = plan.to_dict()
            task.runtime_manifest["plan_progress"] = _plan_progress(plan)
        self._emit(task, "react_finished", {
            "stop_reason": outcome.stop_reason,
            "steps": len(outcome.steps),
            "repeated_failures": outcome.repeated_failures,
        }, severity="warn" if outcome.stop_reason in (
            "repeated_failure", "max_iter", "no_progress", "plan_blocked") else "info")
        if outcome.stop_reason == "cancel":
            if task.task_status != TaskStatus.CANCELLED:
                task = self._move(task, TaskStatus.CANCELLED)
            return task
        if outcome.stop_reason == "needs_user":
            return self._wait_for_user(task, budget, outcome.answer,
                                       "react", outcome.continuation)
        if outcome.stop_reason in ("paused", "side_effect_unknown", "approval_required", "plan_blocked"):
            # F-13 检查点有序化（GAP §1.3）：必须先落 paused 检查点、最后才
            # 把 SUSPENDED 状态写库。否则观察者一看到 SUSPENDED 状态就读
            # latest()，拿到的仍是旧 running 检查点（丢失预算/计划），
            # test_pause_then_resume_completes 的 50% flaky 即此窗口。
            task.waiting_approvals = list(outcome.approval_ids)
            task = self._move(task, TaskStatus.SUSPENDED, save=False)
            self.checkpoint(task, "waiting_approval" if outcome.approval_ids else "paused", extras={
                "budget": budget.snapshot(),
                "react_state": outcome.continuation,
                "pending_side_effects": self.side_fx.pending(task.task_id),
                "plan": plan.to_dict() if plan else None})
            self.db.save_task(task)
            self._audit("task_paused", target=task.task_id,
                        detail_stage="react_decision_boundary")
            self._emit(task, "task_paused", {
                "reason": outcome.stop_reason,
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
        wf = copy.deepcopy(self.wf_lookup(task.workflow_ref or ""))
        if wf is None:
            task = self._move(task, TaskStatus.VERIFYING)
            verdict = Verdict(passed=False, checks=[{"type": "workflow",
                            "passed": False,
                            "error": f"工作流不存在: {task.workflow_ref}"}])
            return self._finalize(task, verdict, None, budget=budget)
        # Registered workflow definitions are shared assets, not mutable run
        # state. Restore only this task's completed outputs below.
        for node in wf.nodes:
            node.state = WorkflowNodeState.PENDING
            node.attempt, node.output, node.error = 0, None, None
        self._freeze_execution_manifest(
            task, expert=self.experts.get(task.domain), workflow=wf)
        # created_from_task 属运行态字段（不进资产内容摘要），须在
        # freeze 绑定之后设置——否则绑定漂移检查必然失败。
        wf.created_from_task = task.task_id

        # 断点续跑：同一任务存在本版本工作流检查点时复用已完成节点输出
        completed = None
        cp = self.cp_mgr.latest(task.task_id)
        if (cp is not None and cp.workflow_version == wf.version
                and cp.task_state.get("workflow_id", wf.workflow_id) == wf.workflow_id
                and cp.task_state.get("workflow_outputs")):
            completed = cp.task_state["workflow_outputs"]

        cfg = self.cfg.budget
        wf_budget = budget.for_scope(
            f"workflow:{wf.workflow_id}",
            BudgetLimits(
                max_model_calls=cfg.workflow_max_model_calls,
                max_tool_calls=cfg.workflow_max_tool_calls,
                max_cost=cfg.workflow_max_cost,
                max_seconds=cfg.workflow_max_seconds,
                max_cost_per_model_call=cfg.task_max_cost_per_model_call,
                max_tool_calls_per_tool=cfg.task_max_tool_calls_per_tool),
            initial_usage=BudgetUsage.from_dict(cp.budget_state["workflow_scope"]["usage"])
            if cp and cp.budget_state.get("workflow_scope", {}).get("usage") else None)
        result = self.workflows.run(
            wf, {"goal": task.user_goal}, budget=wf_budget,
            cancel_flag=self._active.get(task.task_id),
            completed=completed,
            completed_tool_results=cp.task_state.get("workflow_tool_results", {}) if cp else None,
            run_id=task.current_run_id,
            allowed_tools=(self.experts.get(task.domain).tool_whitelist
                           if self.experts.get(task.domain) else None),
            principal=(task.runtime_manifest or {}).get("principal_id"))
        task.runtime_manifest["workflow_budget"] = wf_budget.snapshot()
        # GAP ⑤d：node_run_id（契约统一标识）入任务清单——DAG 节点执行实体
        task.runtime_manifest["node_runs"] = [
            {"node": s.get("node"), "node_run_id": s.get("node_run_id"),
             "state": s.get("state")} for s in result.node_stats
            if s.get("node_run_id")]
        if result.status == WorkflowNodeState.CANCELLED:
            return self._move(task, TaskStatus.CANCELLED)
        if result.status in (WorkflowNodeState.PAUSED,
                             WorkflowNodeState.WAITING_AUTH,
                             WorkflowNodeState.WAITING_EXTERNAL):
            task.waiting_approvals = list(result.approval_ids)
            task = self._move(task, TaskStatus.SUSPENDED, save=False)
            # The engine snapshot contains outputs/budgets. Publish task wait
            # state only after its matching approval ids are checkpointed.
            self.checkpoint(task, "waiting_approval" if result.approval_ids else "paused")
            self.db.save_task(task)
            self._audit("task_paused", target=task.task_id,
                        workflow=task.workflow_ref,
                        reason=str(result.error or ""))
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
        # 预算在最后一次调用后才越界时，不能把任务记为成功（PRD 56）
        try:
            budget.check_alive()
        except BudgetExceeded as e:
            if verdict.passed:
                verdict = Verdict(passed=False, checks=[
                    {"type": "budget", "passed": False, "error": str(e)}])
            failure_override = failure_override or f"预算耗尽: {e}"
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
        # S-05：失败建案审计（含案件引用）
        self._audit("diagnostic_case_recorded", target=task.task_id,
                    outcome="warn", case_id=case.case_id,
                    stop_reason=stop_reason)

    # ================= 检查点 / 恢复 / 取消 =================
    def decide_approval(self, approval_id, approved, *, actor="admin:local", wait=True):
        if not isinstance(approved, bool):
            raise TypeError("approved 必须是布尔值")
        row = self.permissions.approvals.get(approval_id)
        if row is None:
            raise KeyError("审批不存在")
        self._wait_for_previous_run(row["task_id"])
        task, accepted, ready = self.db.decide_task_approval(approval_id, approved, actor)
        if accepted:
            self._audit("approval_decided", target=approval_id,
                        task_id=task.task_id, approved=approved, actor=actor)
            self._emit(task, "approval_decided", {"approval_id": approval_id,
                                                "approved": approved})
        if ready:
            try:
                task = self.resume_task(task.task_id, wait=wait)
            except PermissionError:
                # Approval does not establish the outcome of an external action.
                # Keep its decision but require reconciliation before resuming.
                task = self.db.get_task(task.task_id)
        return task, accepted

    def _wait_for_user(self, task, budget, question, phase, react_state=None):
        task.waiting_request = {"request_id": "question_" + uuid.uuid4().hex,
                                "question": question or "请补充所需信息",
                                "phase": phase, "created_at": time.time(),
                                "answered": False}
        task.result = task.waiting_request["question"]
        task = self._move(task, TaskStatus.WAITING_USER, save=False)
        self.checkpoint(task, "waiting_user", extras={
            "budget": budget.snapshot(), "react_state": react_state or {},
            "pending_side_effects": self.side_fx.pending(task.task_id)})
        self.db.save_task(task)
        self._emit(task, "user_input_requested", task.waiting_request)
        return task

    def answer_task(self, task_id, request_id, answer, *, wait=True):
        if not isinstance(request_id, str) or not request_id:
            raise TypeError("request_id 必须是非空字符串")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 16000:
            raise TypeError("answer 必须是 1..16000 字符的非空文本")
        self._wait_for_previous_run(task_id)
        task, accepted = self.db.record_user_answer(task_id, request_id, answer)
        if accepted:
            self._emit(task, "user_input_received", {"request_id": request_id})
            task = self.resume_task(task_id, wait=wait)
        return task, accepted

    def checkpoint(self, task: Task, stage: str,
                   extras: Optional[Dict[str, Any]] = None) -> str:
        cp = Checkpoint(task_id=task.task_id, current_stage=stage,
                        task_state=task.to_dict())
        previous = self.cp_mgr.latest(task.task_id)
        if previous:
            cp.task_state = {**previous.task_state, **cp.task_state}
            cp.completed_nodes = list(previous.completed_nodes)
            cp.pending_nodes = list(previous.pending_nodes)
            cp.workflow_version = previous.workflow_version
            cp.budget_state = copy.deepcopy(previous.budget_state)
            cp.react_state = copy.deepcopy(previous.react_state)
            cp.pending_side_effects = list(previous.pending_side_effects)
        if extras:
            cp.pending_side_effects = extras.get("pending_side_effects", cp.pending_side_effects)
            cp.budget_state = extras.get("budget", cp.budget_state) or {}
            cp.react_state = extras.get("react_state", cp.react_state) or {}
        # 已完成节点与下一步候选（PRD 34）
        cp.next_candidate_actions = [{"action": "resume"}]
        return self.cp_mgr.save(cp)

    def pause_task(self, task_id: str) -> Dict[str, Any]:
        """请求暂停运行中的 ReAct 任务；在下一个决策边界转入 SUSPENDED（PRD 36）。"""
        task = self.db.get_task(task_id)
        if task is None:
            return {"ok": False, "error": "任务不存在"}
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
        self._wait_for_previous_run(task_id)
        task = self.db.get_task(task_id)
        if task is None:
            return None
        self.memory_bindings.resolve(task)
        if task.runtime_manifest.get("recovery_blocked"):
            raise ValueError(task.runtime_manifest["recovery_blocked"])
        if task.task_status == TaskStatus.CREATED and task.goal_id:
            from ..goals import GoalStore
            GoalStore(self.db).check_task(task.task_id)
            if wait:
                return self.execute(task)
            threading.Thread(target=self.execute, args=(task,), daemon=True).start()
            return task
        if task.task_status == TaskStatus.SUSPENDED:
            # F-03：未决副作用以账本投影为准（GAP §7：不再是检查点自持）；
            # 无账本记录的旧任务回落读检查点（向后兼容）。
            cp = self.cp_mgr.latest(task_id)
            # Once the ledger has facts, it is authoritative. A checkpoint
            # is a historical projection and must not resurrect reconciled
            # effects. Only pre-ledger tasks need the legacy fallback.
            pending = (self.side_fx.pending(task_id)
                       if self.side_fx.history(task_id)
                       else (cp.pending_side_effects if cp else []))
            if pending and not force:
                self._emit(task, "side_effect_state_unknown",
                           {"side_effects": pending}, severity="warn")
                raise PermissionError(
                    "存在结果未知的副作用；需人工核验后以 force=true 恢复")
            if pending and force:
                # F-04：明示授权落审批记录（动作摘要绑 resume+任务、
                # 目标对象=task_id、一次性语义=自动消费）
                if self.permissions.approvals is not None:
                    self.permissions.approvals.note_decision(
                        "resume",
                        {"target": task_id,
                         "pending_side_effects": len(pending)},
                        task_id=task_id, granted_by="api:force",
                        approved=True)
            usage = None
            state = (cp.budget_state if cp else {}) or {}
            if state.get("usage"):
                usage = BudgetUsage.from_dict(state["usage"])
            budget = self._make_budget(task, initial_usage=usage)
            self._apply_expert_budget(task, budget)
            if wait:
                return self._resume_suspended(task, budget, force)
            try:
                self._register_active(task)
            except ValueError:
                return self.db.get_task(task_id)
            threading.Thread(target=self._resume_suspended,
                             args=(task, budget, force), kwargs={"registered": True},
                             daemon=True).start()
            return task
        if task.task_status == TaskStatus.FAILED:
            if self.side_fx.pending(task_id):
                raise PermissionError("失败任务仍有未决副作用，必须先对账再重跑")
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

    def recover_task(self, task_id, *, wait=True):
        """Explicit expired-owner recovery. It never treats expiry as a negative receipt."""
        task = self.db.get_task(task_id)
        if task is None:
            raise KeyError("任务不存在")
        if task.task_status == TaskStatus.WAITING_USER or task.is_terminal():
            return self.runs.leases.interrupt(task_id, "等待用户或终态收尾")
        try:
            self.runs.leases.inspect_recovery(task)
        except RecoveryBlocked as exc:
            task = self.runs.leases.interrupt(task_id, str(exc))
            self._emit(task, "run_interrupted", {"reason": str(exc)}, severity="warn")
            return task
        if task.task_status not in (TaskStatus.RUNNING, TaskStatus.SUSPENDED):
            raise ValueError("此阶段尚不支持自动接管")
        cp = self.cp_mgr.latest(task_id)
        if cp.react_state.get("model_call_inflight"):
            task.runtime_manifest["cost_reconciliation_required"] = True
        budget = self._make_budget(task, initial_usage=BudgetUsage.from_dict(cp.budget_state["usage"]))
        self._apply_expert_budget(task, budget)
        if wait:
            return self._resume_suspended(task, budget, False, recovery=True)
        self._register_active(task)
        threading.Thread(target=self._resume_suspended, args=(task, budget, False),
                         kwargs={"registered": True, "recovery": True}, daemon=True).start()
        return task

    def _resume_suspended(self, task: Task, budget: BudgetController,
                          force: bool, *, registered=False, recovery=False) -> Task:
        if not registered:
            self._register_active(task)
        if recovery:
            # ALN-07-C：接管恢复先对账后决定——副作用不明时禁止自动续跑
            gated = self._reconciliation_gate(task)
            if gated is not None:
                self._unregister_active(task.task_id)
                return gated
        task.runtime_manifest["resume_versions"] = copy.deepcopy(task.version_manifest)
        # Verification failures also release the active-task registration.
        try:
            if self.asset_binder is not None and task.runtime_manifest.get("asset_bindings"):
                self.asset_binder.verify_pinned(task.runtime_manifest["asset_bindings"])
            run_id, started = self.runs.begin(task, task.version_manifest, recovery=recovery)
        except BaseException:
            self._unregister_active(task.task_id)
            raise
        self._slots.acquire()
        try:
            request = task.waiting_request or {}
            phase = request.get("phase") if request.get("answered") else None
            task.waiting_request = None
            task.waiting_approvals = []
            task.result = None
            target = TaskStatus.CLASSIFYING if phase == "classification" else TaskStatus.RUNNING
            if task.task_status != target:
                self._move(task, target)
            self._emit(task, "task_resumed", {"forced": bool(force), "recovered": recovery})
            if phase == "classification":
                task = self._classify(task, None, None, budget)
                if task.task_status != TaskStatus.WAITING_USER and not task.is_terminal():
                    self._apply_expert_budget(task, budget)
                    task = self._plan(task, budget)
                    if not task.is_terminal():
                        task = self._run(task, budget)
            else:
                task = self._run(task, budget, start=False)
            self.db.save_task(task)
        except LeaseLost:
            task = self.db.get_task(task.task_id) or task
        except Exception as e:  # noqa: BLE001
            task.task_status = TaskStatus.FAILED
            task.failure_reason = f"恢复异常: {type(e).__name__}: {e}"
            task.waiting_approvals = []
            self.db.save_task(task)
            self.db.revoke_task_approvals(task.task_id)
            self._record_diagnostic(task, Verdict(passed=False), [],
                                    "resume_error")
            self._emit(task, "task_failed",
                       {"reason": task.failure_reason}, severity="error")
        finally:
            try:
                self.runs.finish(task, run_id, started)
            finally:
                self._unregister_active(task.task_id)
                self._slots.release()
        return task

    def _reconciliation_gate(self, task: Task) -> Optional[Task]:
        """接管恢复的对账门禁（07-C 验收：真实恢复先对账后决定）。

        步骤：补绑崩溃遗留的未决意图 → 计划声明 × 账本 × 绑定三方对账 →
        干净（divergence_free）才放行续跑；存在未声明/未执行/未决/歧义
        效果时保守转 WAITING_USER，禁止自动重放（R-12）。无持久计划的
        任务无从对账，放行并如实标注（legacy 路径，不虚构门禁结论）。
        """
        bindings = getattr(self.react, "bindings", None)
        try:
            plan = self._plan_from_task(task)
        except PlanRevisionError as exc:
            task = self._move(task, TaskStatus.FAILED)
            task.failure_reason = f"恢复对账失败：计划不可读: {exc}"
            self.db.save_task(task)
            self._emit(task, "reconciliation_gate", {"decision": "failed",
                       "reason": task.failure_reason}, severity="warn")
            return task
        if plan is None or bindings is None:
            task.runtime_manifest["reconciliation_skipped"] = (
                "no_plan" if plan is None else "no_bindings")
            return None
        rows = self.side_fx.history(task.task_id)
        try:
            healed = bindings.heal_pending(task.task_id, rows)
            from ..planning.reconcile import reconcile_plan
            report = reconcile_plan(
                plan, rows, step_bindings=bindings.for_task(task.task_id),
                task_id=task.task_id)
        except Exception as exc:  # noqa: BLE001
            task = self._move(task, TaskStatus.FAILED)
            task.failure_reason = f"恢复对账异常: {type(exc).__name__}: {exc}"
            self.db.save_task(task)
            self._emit(task, "reconciliation_gate", {"decision": "error",
                       "reason": task.failure_reason}, severity="warn")
            return task
        detail = {"decision": "resume" if report.ok else "await_user",
                  "healed": healed["healed"], "stale_intents": healed["stale"],
                  "ambiguous_intents": healed["ambiguous"],
                  "undeclared": [u.record.effect_id for u in report.undeclared],
                  "uncertain": [r.effect_id for r in report.uncertain],
                  "ambiguous_effects": [r.effect_id for r in report.ambiguous],
                  "unexecuted": [d.step_id for d in report.unexecuted]}
        if report.ok:
            self._emit(task, "reconciliation_gate", detail)
            return None
        # 副作用不明：不自动重放、不盲目续跑——转等待用户裁决（R-12）
        task.failure_reason = None
        task = self._move(task, TaskStatus.WAITING_USER)
        task.waiting_request = {
            "request_id": f"recon_{task.task_id}",
            "phase": "reconciliation",
            "question": ("接管恢复对账发现副作用分歧（未声明/未决/歧义），"
                         "禁止自动重放；请确认账本后以 resume 继续或取消。"),
            "answered": False,
            "summary": detail,
        }
        self.db.save_task(task)
        self._emit(task, "reconciliation_gate", detail, severity="warn")
        return task

    def _build_retry(self, task: Task) -> Task:
        """失败任务重跑创建新任务（终态不可改写，PRD 36 / Arch 3.3）。"""
        retry = Task(user_goal=task.user_goal,
                     goal_id=task.goal_id, origin=task.origin,
                     constraints=list(task.constraints),
                     acceptance_criteria=list(task.acceptance_criteria),
                     risk_level=task.risk_level,
                     user_answers=copy.deepcopy(task.user_answers),
                     budget_policy=dict(task.budget_policy),
                     intent=task.intent, domain=task.domain)
        retry.parent_task_id = task.task_id
        retry.parent_run_id = task.current_run_id
        self.memory_bindings.inherit(task, retry)
        retry.runtime_manifest["resumed_from"] = task.task_id
        if task.goal_id:
            from ..goals import GoalStore
            GoalStore(self.db).bind_retry(task.task_id, retry)
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
        task.waiting_approvals = []
        self.db.save_task(task)
        self.db.revoke_task_approvals(task_id)
        self._emit(task, "task_cancelled", {})
        return {"ok": True, "task_id": task_id, "status": task.task_status.value}

    # ================= 内部工具 =================
    def _make_budget(self, task: Task,
                     initial_usage: Optional[BudgetUsage] = None
                     ) -> BudgetController:
        cfg = self.cfg.budget
        durable = self.db.get_execution_budget(task.task_id)
        if durable:
            initial_usage = BudgetUsage.from_dict(durable["usage"])
        limits = BudgetLimits(
            max_model_calls=cfg.task_max_model_calls,
            max_tool_calls=cfg.task_max_tool_calls,
            max_cost=cfg.task_max_cost,
            max_seconds=cfg.task_max_seconds,
            max_concurrent=cfg.max_concurrent_tasks,
            max_cost_per_model_call=cfg.task_max_cost_per_model_call,
            max_tool_calls_per_tool=cfg.task_max_tool_calls_per_tool)
        budget = BudgetController(limits, dict(task.budget_policy or {}),
                                  initial_usage=initial_usage,
                                  ledger=self.ledger, ledger_scope="runtime")
        budget._restored_scopes = (durable or {}).get("scopes", {})
        budget.on_change = lambda state: self.db.save_execution_budget(
            task.task_id, task.current_run_id, state)
        if task.goal_id or task.origin != GoalOrigin.USER.value:
            from ..goals import GoalStore
            def check_goal_authority():
                limits = GoalStore(self.db).check_task(task.task_id)
                for key, value in (limits or {}).items():
                    setattr(budget.limits, key, min(getattr(budget.limits, key), value))
            budget.authority_check = check_goal_authority
        goal_check = budget.authority_check
        def check_identity_authority():
            self.memory_bindings.resolve(task)
            if goal_check:
                goal_check()
        budget.authority_check = check_identity_authority
        if self.runtime_budgets is not None:
            budget.runtime_binding = self.runtime_budgets.bind(task, budget)
            task.runtime_manifest["aggregate_budget"] = dict(budget.runtime_binding.data)
        if task.current_run_id:
            budget.persist_usage()
        return budget

    def _apply_expert_budget(self, task: Task,
                             budget: BudgetController) -> None:
        scope = self.experts.get(task.domain) or self.experts.route(task.intent)
        if scope and scope.budget_class == "generous":
            budget.limits.max_model_calls *= 2
            budget.limits.max_cost *= 2

    def _route_model(self, task: Task) -> Any:
        """按意图/领域选择模型并记录路由结果（PRD 19）。

        显式 model_override 优先（迁移回归/受控验证）；解析失败时明确告警并
        回退常规路由，绝不静默把未知模型当成默认模型执行。
        """
        override = (task.runtime_manifest or {}).get("model_override")
        if override:
            resolved = self.router.resolve(override)
            if resolved is not None:
                self._record_route(task, resolved, source="override")
                return resolved
            self._emit(task, "model_override_unavailable",
                       {"model_id": override}, severity="warn")
        try:
            model = self.router.route(task.intent or "general",
                                      task.domain or "general")
        except Exception as exc:  # noqa: BLE001 —— 记录后交由任务失败流程收尾
            # Only the router may authorize a fallback. In particular its
            # rejection must not execute react.model behind the policy's back.
            task.runtime_manifest["model_route"] = {
                "model_id": None, "provider": None,
                "intent": task.intent, "domain": task.domain,
                "source": "router_rejected" if isinstance(exc, ValueError) else "router_error",
                "error_class": type(exc).__name__}
            self._emit(task, "model_route_failed",
                       dict(task.runtime_manifest["model_route"]), severity="error")
            raise
        self._record_route(task, model, source="router")
        return model

    @staticmethod
    def _record_route(task: Task, model: Any, *, source: str) -> None:
        try:
            profile = model.profile()
        except Exception:  # noqa: BLE001
            profile = None
        task.runtime_manifest["model_route"] = {
            "model_id": getattr(profile, "model_id", None),
            "provider": getattr(profile, "provider", None),
            "intent": task.intent, "domain": task.domain,
            "source": source}

    def _bind_model_pool(self, budget: BudgetController, model: Any) -> None:
        """模型提供方预算池（PRD 55）：同一提供方跨任务累计。"""
        try:
            provider = model.profile().provider or "unknown"
        except Exception:  # noqa: BLE001
            provider = "unknown"
        budget.bind_pool(
            f"provider:{provider}",
            max_model_calls=self.cfg.budget.provider_daily_model_calls,
            max_cost=self.cfg.budget.provider_daily_cost)

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
        if "execution_plan" not in task.runtime_manifest:
            return None  # Legacy tasks truly lacking a plan remain supported.
        raw = task.runtime_manifest.get("execution_plan")
        if (not isinstance(raw, dict) or not isinstance(raw.get("steps"), list)
                or any(not isinstance(step, dict) for step in raw["steps"])):
            raise PlanRevisionError("持久执行计划格式损坏")
        try:
            plan = ExecutionPlan.from_dict(raw)
        except Exception as exc:
            raise PlanRevisionError("持久执行计划无法读取") from exc
        plan.validate_execution_state()
        if task.execution_plan_ref and plan.plan_id != task.execution_plan_ref:
            raise PlanRevisionError("持久执行计划引用不匹配")
        return plan

    def _freeze_execution_manifest(self, task: Task, *, expert=None,
                                    model=None, workflow=None) -> None:
        manifest = task.version_manifest
        facts = {}
        if expert is not None:
            facts["expert_ref"] = f"{expert.expert_id}/{expert.version}"
        if model is not None:
            facts["model_profile_ref"] = (
                f"{model.profile().model_id}/{manifest.get('model_adapter_version', 'unknown')}")
        if workflow is not None:
            facts["workflow_ref"] = {"id": workflow.workflow_id,
                                      "version": workflow.version}
        previous = task.runtime_manifest.get("resume_versions") or {}
        for key, value in facts.items():
            if previous.get(key) is not None and previous[key] != value:
                raise ValueError(f"恢复所需组件已改变: {key}")
        manifest.update(facts)
        manifest["tool_versions"] = self.tools.version_inventory()
        if "expert_ref" in facts:
            task.runtime_manifest["expert_ref"] = facts["expert_ref"]
        self._bind_task_assets(task, expert=expert, workflow=workflow)
        self.runs.freeze(task)
        self.db.save_task(task)

    def _bind_task_assets(self, task: Task, *, expert=None,
                          workflow=None) -> None:
        """ALN-06-R：执行前把专家/工作流绑到不可变资产的精确版本。

        已有绑定（恢复/续跑）只验证存在与摘要一致，不重新解析——
        资产事后退役不得阻断旧运行；新绑定拒绝 retired/draft/candidate。
        工作流库/专家注册表内容与绑定资产摘要不一致视为库漂移，拒绝执行。
        """
        if self.asset_binder is None:
            return
        existing = task.runtime_manifest.get("asset_bindings")
        if existing:
            self.asset_binder.verify_pinned(existing)
            # 执行内容仍须与绑定一致：库漂移在续跑路径同样拒绝
            from ..assets.mapping import expert_to_asset, workflow_to_asset
            from ..assets.content import content_digest
            from ..assets.consumption import ConsumptionError
            if workflow is not None:
                pinned = existing.get(f"workflow:{workflow.workflow_id}"
                                      f"@{workflow.version}")
                if pinned is None:
                    raise ConsumptionError("当前执行实体未包含在冻结资产绑定中")
                if content_digest(
                        workflow_to_asset(workflow).content) != pinned["content_digest"]:
                    raise ConsumptionError(
                        f"工作流库内容与绑定资产摘要不一致（续跑）: "
                        f"{workflow.workflow_id}@{workflow.version}")
            if expert is not None:
                pinned = existing.get(f"expert:{expert.expert_id}"
                                      f"@{expert.version}")
                if pinned is None:
                    raise ConsumptionError("当前执行实体未包含在冻结资产绑定中")
                if content_digest(
                        expert_to_asset(expert).content) != pinned["content_digest"]:
                    raise ConsumptionError(
                        f"专家注册表内容与绑定资产摘要不一致（续跑）: "
                        f"{expert.expert_id}@{expert.version}")
            return
        bindings: Dict[str, Any] = {}
        # 内容快照摘要：库内容漂移时与绑定资产不一致，执行前拒绝
        from ..assets.mapping import expert_to_asset, workflow_to_asset
        from ..assets.content import content_digest
        from ..assets.consumption import ConsumptionError
        if workflow is not None:
            bindings.update(self.asset_binder.bind(
                "workflow", workflow.workflow_id,
                prefer_ref=f"workflow:{workflow.workflow_id}@{workflow.version}"))
            bound = bindings.get(f"workflow:{workflow.workflow_id}"
                                 f"@{workflow.version}")
            if bound is None:
                raise ConsumptionError(
                    f"工作流 {workflow.workflow_id} 绑定版本与执行版本不一致")
            drift = content_digest(workflow_to_asset(workflow).content)
            if drift != bound["content_digest"]:
                raise ConsumptionError(
                    f"工作流库内容与绑定资产摘要不一致: "
                    f"{workflow.workflow_id}@{workflow.version}")
        if expert is not None:
            bindings.update(self.asset_binder.bind(
                "expert", expert.expert_id,
                prefer_ref=f"expert:{expert.expert_id}@{expert.version}"))
            bound = bindings.get(f"expert:{expert.expert_id}"
                                 f"@{expert.version}")
            if bound is None:
                raise ConsumptionError("专家绑定版本与执行版本不一致")
            if bound is not None:
                drift = content_digest(expert_to_asset(expert).content)
                if drift != bound["content_digest"]:
                    raise ConsumptionError(
                        f"专家注册表内容与绑定资产摘要不一致: "
                        f"{expert.expert_id}@{expert.version}")
        if bindings:
            task.runtime_manifest["asset_bindings"] = bindings
            task.version_manifest["asset_bindings"] = dict(bindings)
            self._emit(task, "asset_bindings_frozen",
                       {"refs": sorted(bindings)})

    def _version_manifest(self) -> Dict[str, Any]:
        if self.releases and self.releases.current_release_id:
            m = self.releases.manifests.get(self.releases.current_release_id)
        else:
            m = None
        if m is not None:
            base = {
                "release_id": m.release_id, "version": m.version,
                "artifact_digest": m.artifact_digest,
                "code_commit": m.code_commit,
                "model_adapter_version": m.model_adapter_version,
                "prompt_version": m.prompt_version,
                "tools_version": m.tools_version,
                "workflow_versions": list(m.workflow_versions),
                "env_version": m.env_version,
            }
        else:
            base = {"release_id": None, "version": "unreleased"}
        return self._manifest_contract_fields(base)

    def _manifest_contract_fields(self, base: Dict[str, Any]) -> Dict[str, Any]:
        """契约 §5 Runtime Manifest 补充字段（GAP 第一批第5项·子步⑤c）。

        契约字段映射到既有版本锚点（release manifest），不发明新版本源：
        - runtime_adapter{name,version}：适配器形态取当前 provider，
          版本锚 = model_adapter_version
        - model_profile_ref：{model_id}/{model_adapter_version}
        - router_policy_ref：adapter/{model_adapter_version}（路由策略
          归属适配器层版本锚）
        - context_policy_ref：env/{env_version}（上下文策略归属环境版本锚）
        - environment_digest：稳定事实集合的 sha256（不可变指纹）
        - evaluation_suite_ref：运行前由调用方经 manifest_extras 预置
          （evolution runner 传数据集 id/version），未知为 null
        - workflow_ref：契约形态为 {id, version} 对象或 null，execute
          落值（wf_lookup 核对版本）
        - expert_ref：_run_react 专家解析后落值
        - tool_versions[]：注册表逐工具 {tool_id, version} 对象数组
        """
        adapter_version = str(base.get("model_adapter_version") or "0.0.0")
        adapter_name = ("openai_compat"
                        if self.cfg.provider.api_key else "mock")
        facts = {
            "version": base.get("version"),
            "code_commit": base.get("code_commit"),
            "model_adapter_version": base.get("model_adapter_version"),
            "prompt_version": base.get("prompt_version"),
            "tools_version": base.get("tools_version"),
            "env_version": base.get("env_version"),
            "core_version": CORE_VERSION,
        }
        base.update({
            "created_at": datetime.datetime.now(
                datetime.timezone.utc).isoformat(timespec="seconds"),
            "core_version": CORE_VERSION,
            "runtime_adapter": {"name": adapter_name,
                                "version": adapter_version},
            "model_profile_ref": None,
            "router_policy_ref": f"adapter/{adapter_version}",
            "context_policy_ref": f"env/{base.get('env_version') or '0.0.0'}",
            "evaluation_suite_ref": None,
            "expert_ref": None,
            "workflow_ref": None,
            "tool_versions": self.tools.version_inventory()
            if self.tools is not None else [],
            "environment_digest": hashlib.sha256(
                json.dumps(facts, sort_keys=True, ensure_ascii=False,
                           default=str).encode("utf-8")).hexdigest(),
        })
        return base

    def _move(self, task: Task, target: TaskStatus, *,
              save: bool = True) -> Task:
        task.task_status = transition(task.task_status, target)
        task.updated_at = time.time()
        if target in (TaskStatus.RUNNING, TaskStatus.VERIFYING):
            self.checkpoint(task, target.value)
        if save:
            self.db.save_task(task)
        # S-05：状态流转全审计（每转必留痕）
        self._audit("task_state_transition", target=task.task_id,
                    to=target.value)
        self._emit(task, "state_transition", {"to": target.value})
        return task

    def _remember(self, task: Task, verified: bool) -> None:
        """执行结果沉淀为记忆（凭证据决定可信状态；原始日志不自动成为经验）。"""
        if task.runtime_manifest.get("source") == "evaluation":
            return  # Holdout answers must never become production/tuning memory.
        # GAP 死字段修复③：写侧 key 与专家 memory_scope_key 对齐
        # （读侧 _run_react 同键检索；mobile_operation 专家键=mobile）
        expert = self.experts.get(task.domain)
        hive_key = (expert.memory_scope_key if expert and
                    expert.memory_scope_key else "") or task.domain
        identity, _ = self.memory_bindings.resolve(task, hive_key)
        content = f"任务<{task.intent}>: {task.user_goal} -> " + \
                  (task.result or task.failure_reason or "")
        item = MemoryItem(
            content=content[:300],
            kind=MemoryKind.RUNTIME_EXPERIENCE,
            scope=Scope.EXPERT,
            scope_key=hive_key,
            status=Credibility.VERIFIED if verified else Credibility.CANDIDATE,
            source="runtime",
            applicable_domains=[task.domain],
            evidence={"task_id": task.task_id,
                      "verdict": verdict_of(task)},
            valid_until=(time.time() + 7 * 86400) if not verified else None,
        )
        item = self.memory.write(item, identity=identity)
        # S-05：记忆沉淀审计（记录可信状态与证据引用）
        self._audit("memory_remembered", target=task.task_id,
                    verified=verified, status=item.status.value,
                    memory_id=item.memory_id, memory_binding_ref=task.memory_binding_ref)

    def _audit(self, action: str, *, target: str = "", outcome: str = "ok",
               **detail: Any) -> None:
        """S-05 执行环节审计：追加记录、不参与业务判断（Arch 18.2）。

        审计自身故障不阻断执行；detail 展开为结构的负载。
        """
        if self.audit is None:
            return
        try:
            self.audit.record("controller", action, target=target,
                              outcome=outcome,
                              detail=detail if detail else None)
        except Exception:  # noqa: BLE001 —— 审计故障不得影响业务
            pass

    def _emit(self, task: Task, event_type: str,
              payload: Dict[str, Any], severity: str = "info") -> RunEvent:
        return self.bus.emit(event_type=event_type, task_id=task.task_id,
                             run_id=task.current_run_id,
                             runtime_manifest_id=(task.version_manifest or {})
                             .get("runtime_manifest_id"),
                             component_id="runtime_controller",
                             severity=severity, payload=payload)

    def register_active(self, task_id: str) -> None:
        self._register_active(Task(task_id=task_id))

    def _register_active(self, task: Task) -> None:
        class _Flag:
            cancelled = False
            paused = False
        with self._lock:
            if task.task_id in self._active:
                raise ValueError("任务正在运行")
            flag = _Flag()
            flag.done = threading.Event()
            self._active[task.task_id] = flag

    def _wait_for_previous_run(self, task_id):
        with self._lock:
            flag = self._active.get(task_id)
        task = self.db.get_task(task_id)
        if flag and task and task.task_status in (TaskStatus.SUSPENDED, TaskStatus.WAITING_USER):
            if not flag.done.wait(5):
                raise ValueError("上一次运行正在收尾，请稍后重试")

    def _unregister_active(self, task_id: str) -> None:
        with self._lock:
            flag = self._active.pop(task_id, None)
            if flag:
                flag.done.set()


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
