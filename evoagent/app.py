"""系统装配：把所有子系统按 Architecture 组装成一个可运行实例。"""

import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Config
from .core.audit import AuditLog
from .core.backup import BackupManager
from .core.budget import BudgetLedger, BudgetLimits
from .core.checkpoint import CheckpointManager
from .core.approvals import ApprovalStore  # F-04 审批记录
from .core.controller import RuntimeController
from .core.events import EventBus
from .core.log_layers import (EvidenceStore, ExecutionTracer, LayerManager)
from .core.metrics import MetricsAggregator
from .core.persistence import Database
from .core.permissions import (PermissionController, PermissionDecision,
                                ToolPolicy)
from .core.replay import ReplayService
from .evolution.controller import EvolutionController
from .evolution.datasets import builtin_datasets
from .evolution.diagnostics import DiagnosisEngine
from .evolution.sandbox import SandboxRunner
from .evolution.drift import DriftDetector
from .evolution.evaluator import Evaluator, ReleaseGate
from .evolution.probes import PROBES, ProbeRunner
from .evolution.runner import ControllerTaskRunner
from .experts.builtin import builtin_experts
from .memory.context import ContextAssembler
from .memory.embeddings import build_embedder
from .memory.evaluation import MemoryEvaluator
from .memory.store import MemoryStore
from .models.base import ModelRegistry
from .models.metering import MeteredModel
from .models.migration import ModelMigrationManager
from .models.mock import MockModel
from .models.openai_compat import OpenAICompatAdapter
from .models.router import ModelRouter
from .planning.intent import IntentClassifier
from .planning.planner import PlanningEngine
from .react.engine import ReactEngine
from .release.delivery import ReleaseDelivery
from .release.manager import ReleaseManager
from .tools.builtin import BUILTIN_FACTORIES
from .tools.registry import ToolRegistry
from .workflows.builtin import comic_drama_workflow, register_comic_drama_tools
from .workflows.engine import WorkflowEngine
from .workflows.extraction import WorkflowExtractor


class App:
    """依赖装配根。dir_name 支持多实例隔离（测试/生产）。"""

    def __init__(self, config_path: Optional[str] = None,
                 data_dir: Optional[str] = None,
                 confirm_callback=None):
        self.config = Config.load(config_path)
        if data_dir:
            self.config.data_dir = Path(data_dir)
            self.config.data_dir.mkdir(parents=True, exist_ok=True)
        d = self.config.data_dir
        self.started_at = time.time()

        # 基础设施
        self.db = Database(str(d / "evoagent.db"))
        self.bus = EventBus(str(d / "events.db"))
        self.memory = MemoryStore(str(d / "memory.db"),
                                  embedder=build_embedder(self.config))
        self.ledger = BudgetLedger(self.db)
        self.audit = AuditLog(self.db, self.bus)
        # 日志分层：执行轨迹 + 诊断证据 + 保留策略（PRD 38-39 / Arch 9.2）
        self.evidence = EvidenceStore(self.db)
        self.logs = LayerManager(self.db, self.bus, self.evidence)
        self.tracer = ExecutionTracer(self.bus, self.db)
        secret = self._release_secret(d)
        self.releases = ReleaseManager(signing_secret=secret, store=self.db,
                                       artifact_dir=str(d / "artifacts"))
        if not self.releases.manifests:
            self.release_manifest = self.releases.build_manifest(
                version="v0.1.0",
                model_adapter_version=self.config.provider.name)
            self.releases.current_release_id = self.release_manifest.release_id
        else:
            self.release_manifest = next(
                iter(self.releases.manifests.values()))

        # 权限（高风险默认确认制；confirm_callback 由宿主在获得用户授权后注入）
        # F-04：审批记录存取——高风险决策与一次性语义在决策点落账
        self.approvals = ApprovalStore(self.db)
        self.permissions = PermissionController(high_risk_mode=self.config.high_risk_mode,
                                                confirm_callback=confirm_callback,
                                                approvals=self.approvals)

        # 工具
        self.tools = ToolRegistry(self.permissions, store=self.db)
        self._register_tools()

        # 版本对账与客户端应用回执（双端协同 §5）：只读发布状态，不改变发布权
        self.delivery = ReleaseDelivery(
            self.releases, self.db, tool_ids=set(self.tools.health()))

        # 记忆与上下文
        self.assembler = ContextAssembler(self.memory)
        self.memory_evaluator = MemoryEvaluator()

        # 模型（包装后记录调用成功/失败与时延，PRD 40）
        self.model_registry = ModelRegistry()
        self.mock_model = MockModel()
        self._mock_metered = MeteredModel(self.mock_model, self.bus)
        self._real_adapter: Optional[OpenAICompatAdapter] = None
        self._real_metered: Optional[MeteredModel] = None
        if self.config.provider.api_key:
            self._real_adapter = OpenAICompatAdapter(
                model_id=self.config.provider.model_id,
                api_key=self.config.provider.api_key,
                base_url=self.config.provider.base_url,
                timeout_seconds=self.config.provider.timeout_seconds,
            )
            self.model_registry.register(self._real_adapter.profile())
            self._real_metered = MeteredModel(self._real_adapter, self.bus)
        else:
            # 配了真实模型时 Mock 只作回退，不参与路由候选
            self.model_registry.register(self.mock_model.profile())
        self.router = ModelRouter(self.model_registry, self._mock_metered)
        # 路由按 model_id 解析到真实适配器；未命中时回退 Mock
        self.router.setup(self._adapter_for_model_id)

        # 专家
        self.experts = builtin_experts()

        # 执行组件：真实模型可用时，规划/分类/ReAct 共用（Mock 时自然回退）
        planning_model = self._model() or self._mock_metered
        self.planner = PlanningEngine(planning_model)
        self.classifier = IntentClassifier(
            planning_model,
            known_intents=self._known_intents(),
            threshold=self.config.clarify_threshold)
        self.react = ReactEngine(planning_model,
                                 self.tools, self.bus,
                                 max_iterations=self.config.react_max_iterations,
                                 max_repeat_failures=self.config.react_max_repeat_failures)
        self.workflow_engine = WorkflowEngine(self.tools,

                                             CheckpointManager(self.db), self.bus)
        self._workflows: Dict[str, Any] = {}
        self._register_workflows()

        # 进化与诊断：诊断只生成案例/假设，候选发布走进化门禁
        self.datasets = builtin_datasets()
        self.evolution = EvolutionController(
            event_bus=self.bus, store=self.db, releases=self.releases,
            datasets=self.datasets, gate=ReleaseGate(),
            budget=self.ledger, budget_config=self.config.budget)
        self.probes = ProbeRunner(self.tools, self.memory,
                                  self.config.diagnostics)
        self.diagnostics = DiagnosisEngine(
            self.db, self.bus, self.probes, self.config.diagnostics,
            self.evolution, model=self._model())
        # 候选代码隔离执行（Arch 18.3）：一次性目录、无网络/子进程、凭据不继承
        self.sandbox = SandboxRunner()
        # 环境漂移检测（PRD 44）：只读窗口分析，默认不制造事件
        self.drift = DriftDetector(self.bus, self.db)
        # 运行回放（Arch 20.3）：只读重建，不执行真实副作用
        self.replay = ReplayService(self.db, self.bus, self.releases)
        # 备份与恢复（Arch 20.4）：SQLite 在线备份 + 校验 + 恢复
        self.backups = BackupManager(
            str(d), db=self.db, bus=self.bus, memory=self.memory,
            releases=self.releases)

        # 成功任务 → 工作流候选（Arch 16.2）：需重复支持 + 人工批准 + 参数绑定
        self.workflow_extractor = WorkflowExtractor(
            self.db, self.bus, self.tools, audit=self.audit,
            on_publish=lambda wf: self._workflows.__setitem__(
                wf.workflow_id, wf))

        # 控制器
        self.controller = RuntimeController(
            self.config, self.db, self.bus, self.tools, self.experts,
            self.planner, self.react, self.workflow_engine, self.router,
            self.memory, self.assembler,
            CheckpointManager(self.db), self.permissions, self.releases,
            wf_lookup=self._wf_lookup, diagnostics=self.diagnostics,
            classifier=self.classifier, ledger=self.ledger)

        # 评测器依赖运行时执行入口；在控制器之后注入以打破循环依赖
        self.evaluator = Evaluator(
            ControllerTaskRunner(self.controller, ledger=self.ledger),
            self.bus)
        self.evolution.attach_evaluator(self.evaluator, self.datasets)
        # 模型迁移执行链（PRD 20）：回归固定目标模型，不影响常规路由
        self.migrations = ModelMigrationManager(
            self.model_registry, self.router,
            runner_factory=lambda mid: ControllerTaskRunner(
                self.controller, ledger=self.ledger, model_id=mid),
            datasets=self.datasets, releases=self.releases,
            db=self.db, bus=self.bus)
        self.metrics = MetricsAggregator(
            self.db, self.bus, self.ledger, self.releases,
            self.evolution, self.tools, started_at=self.started_at,
            logs=self.logs, drift=self.drift,
            daily_cost_limit=self.config.budget.runtime_daily_cost)

    # ---- 装配细节 ----
    def _release_secret(self, data_dir: Path) -> str:
        if self.config.release_signing_secret:
            return self.config.release_signing_secret
        path = data_dir / "release_secret"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        secret = secrets.token_hex(32)
        path.write_text(secret, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return secret

    def _model(self):
        """默认使用真实模型（若配置），否则 Mock；可按意图再路由。"""
        return self._real_metered

    def _adapter_for_model_id(self, model_id: str):
        """按 model_id 解析适配器；未知模型返回 None，由路由回退默认模型。

        迁移执行链要求「新模型真实可解析」，静默返回 Mock 会让兼容性检查失真。
        """
        try:
            if self._real_adapter is not None \
                    and self._real_adapter.profile().model_id == model_id:
                return self._real_metered
            if self.mock_model.profile().model_id == model_id:
                return self._mock_metered
        except Exception:  # noqa: BLE001
            pass
        return None

    def _known_intents(self) -> list:
        intents = {"general"}
        for e in self.experts.all():
            intents.add(e.expert_id)
            intents.update(e.applicable_intents)
        return sorted(intents)

    def _register_tools(self) -> None:
        from .tools.builtin import file_write_tool, git_read_tool
        workdir = str(self.config.data_dir / "workspace")
        for factory in BUILTIN_FACTORIES.values():
            if factory is file_write_tool:
                contract, runner = factory(workdir=workdir)
            elif factory is git_read_tool:
                contract, runner = factory(workdir=str(Path.cwd()))
            else:
                contract, runner = factory()
            self.tools.register(contract, runner)
        register_comic_drama_tools(self.tools)
        # 高风险工具显式策略：需要用户确认
        self.permissions.register_tool_policy(ToolPolicy(
            tool_id="file_write", decision=PermissionDecision.CONFIRM))

    def _register_workflows(self) -> None:
        from .workflows.library import load_workflow, save_workflow
        comic = comic_drama_workflow(theme="三只小猫的一天")
        self._workflows[comic.workflow_id] = comic
        if self.db.get_workflow_def(comic.workflow_id) is None:
            save_workflow(self.db, comic)
        # 从资产库载入其他已发布工作流（仅工具节点可序列化）
        for row in self.db.list_workflow_defs():
            wid = row["workflow_id"]
            if wid in self._workflows:
                continue
            try:
                wf = load_workflow(self.db, wid, row["version"])
            except Exception:  # noqa: BLE001
                continue
            if wf is not None:
                self._workflows[wid] = wf

    def _wf_lookup(self, wid: str):
        return self._workflows.get(wid)

    # ---- 便捷操作 ----
    def submit(self, goal: str, **kwargs) -> Any:
        return self.controller.submit_task(goal, **kwargs)

    def get_task(self, task_id: str):
        return self.db.get_task(task_id)

    def run_memory_eval(self, *, persist: bool = True) -> Dict[str, Any]:
        """离线记忆质量评测 + 真实任务反馈；报告持久化供趋势查看。"""
        report = self.memory_evaluator.evaluate()
        report["runtime_feedback"] = self.memory_evaluator.runtime_feedback(
            self.db.list_tasks(limit=500))
        report["report_id"] = uuid.uuid4().hex[:12]
        if persist:
            self.db.save_memory_eval_report(
                report["report_id"], report["dataset_id"],
                report["version"], report)
            self.bus.emit(event_type="memory_eval_completed",
                          component_id="memory_evaluator",
                          payload={
                              "report_id": report["report_id"],
                              "recall": report.get("recall"),
                              "precision": report.get("precision"),
                              "expired_misuse_rate":
                                  report.get("expired_misuse_rate"),
                          })
        return report

    def health(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "version": "0.1.0",
            "data_dir": str(self.config.data_dir),
            "provider": self.config.provider.name,
            "tools_health": self.tools.health(),
            "experts": [e.expert_id for e in self.experts.all()],
            "workflows": list(self._workflows.keys()),
            "datasets": sorted(self.datasets),
            "candidates": len(self.evolution.all_candidates()),
            "budget": {
                "runtime_day": self.ledger.usage("runtime", "day"),
                "evolution_day": self.ledger.usage("evolution", "day"),
            },
            "auth": "enabled" if (self.config.api_token
                                  or self.config.api_admin_token) else "open",
            "diagnostics": {
                "cases": len(self.db.list_diagnostic_cases(limit=1000)),
                "probes": sorted(PROBES),
            },
        }
