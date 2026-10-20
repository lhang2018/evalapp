"""系统装配：把所有子系统按 Architecture 组装成一个可运行实例。"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Config
from .core.budget import BudgetLimits
from .core.checkpoint import CheckpointManager
from .core.controller import RuntimeController
from .core.events import EventBus
from .core.persistence import Database
from .core.permissions import (PermissionController, PermissionDecision,
                                ToolPolicy)
from .evolution.controller import EvolutionController
from .evolution.diagnostics import DiagnosisEngine
from .evolution.probes import PROBES, ProbeRunner
from .experts.builtin import builtin_experts
from .memory.context import ContextAssembler
from .memory.store import MemoryStore
from .models.base import ModelRegistry
from .models.mock import MockModel
from .models.openai_compat import OpenAICompatAdapter
from .models.router import ModelRouter
from .planning.intent import IntentClassifier
from .planning.planner import PlanningEngine
from .react.engine import ReactEngine
from .release.manager import ReleaseManager
from .tools.builtin import BUILTIN_FACTORIES
from .tools.registry import ToolRegistry
from .workflows.builtin import comic_drama_workflow, register_comic_drama_tools
from .workflows.engine import WorkflowEngine


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

        # 基础设施
        self.db = Database(str(d / "evoagent.db"))
        self.bus = EventBus(str(d / "events.db"))
        self.memory = MemoryStore(str(d / "memory.db"))
        self.releases = ReleaseManager()
        self.release_manifest = self.releases.build_manifest(
            version="v0.1.0", model_adapter_version=self.config.provider.name)
        self.releases.current_release_id = self.release_manifest.release_id

        # 权限（高风险默认确认制；confirm_callback 由宿主在获得用户授权后注入）
        self.permissions = PermissionController(high_risk_mode=self.config.high_risk_mode,
                                                confirm_callback=confirm_callback)

        # 工具
        self.tools = ToolRegistry(self.permissions)
        self._register_tools()

        # 记忆与上下文
        self.assembler = ContextAssembler(self.memory)

        # 模型
        self.model_registry = ModelRegistry()
        self.mock_model = MockModel()
        self.model_registry.register(self.mock_model.profile())
        self._real_adapter: Optional[OpenAICompatAdapter] = None
        if self.config.provider.api_key:
            self._real_adapter = OpenAICompatAdapter(
                model_id=self.config.provider.model_id,
                api_key=self.config.provider.api_key,
                base_url=self.config.provider.base_url,
                timeout_seconds=self.config.provider.timeout_seconds,
            )
            self.model_registry.register(self._real_adapter.profile())
        self.router = ModelRouter(self.model_registry, self.mock_model)
        # 路由按 model_id 解析到真实适配器；未命中时回退 Mock
        self.router.setup(self._adapter_for_model_id)

        # 专家
        self.experts = builtin_experts()

        # 执行组件：真实模型可用时，规划/分类/ReAct 共用（Mock 时自然回退）
        planning_model = self._model() or self.mock_model
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
        self.evolution = EvolutionController(event_bus=self.bus)
        self.probes = ProbeRunner(self.tools, self.memory,
                                  self.config.diagnostics)
        self.diagnostics = DiagnosisEngine(
            self.db, self.bus, self.probes, self.config.diagnostics,
            self.evolution)

        # 控制器
        self.controller = RuntimeController(
            self.config, self.db, self.bus, self.tools, self.experts,
            self.planner, self.react, self.workflow_engine, self.router,
            self.memory, self.assembler,
            CheckpointManager(self.db), self.permissions, self.releases,
            wf_lookup=self._wf_lookup, diagnostics=self.diagnostics,
            classifier=self.classifier)

    # ---- 装配细节 ----
    def _model(self):
        """默认使用真实模型（若配置），否则 Mock；可按意图再路由。"""
        return self._real_adapter

    def _adapter_for_model_id(self, model_id: str):
        if self._real_adapter is not None:
            try:
                if self._real_adapter.profile().model_id == model_id:
                    return self._real_adapter
            except Exception:  # noqa: BLE001
                pass
        return self.mock_model

    def _known_intents(self) -> list:
        intents = {"general"}
        for e in self.experts.all():
            intents.add(e.expert_id)
            intents.update(e.applicable_intents)
        return sorted(intents)

    def _register_tools(self) -> None:
        from .tools.builtin import file_write_tool
        for factory in BUILTIN_FACTORIES.values():
            if factory is file_write_tool:
                contract, runner = factory(workdir=str(self.config.data_dir / "workspace"))
            else:
                contract, runner = factory()
            self.tools.register(contract, runner)
        register_comic_drama_tools(self.tools)
        # 高风险工具显式策略：需要用户确认
        self.permissions.register_tool_policy(ToolPolicy(
            tool_id="file_write", decision=PermissionDecision.CONFIRM))

    def _register_workflows(self) -> None:
        self._workflows["comic_drama_demo"] = comic_drama_workflow(theme="三只小猫的一天")

    def _wf_lookup(self, wid: str):
        return self._workflows.get(wid)

    # ---- 便捷操作 ----
    def submit(self, goal: str, **kwargs) -> Any:
        return self.controller.submit_task(goal, **kwargs)

    def get_task(self, task_id: str):
        return self.db.get_task(task_id)

    def health(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "version": "0.1.0",
            "data_dir": str(self.config.data_dir),
            "provider": self.config.provider.name,
            "tools_health": self.tools.health(),
            "experts": [e.expert_id for e in self.experts.all()],
            "workflows": list(self._workflows.keys()),
            "diagnostics": {
                "cases": len(self.db.list_diagnostic_cases(limit=1000)),
                "probes": sorted(PROBES),
            },
        }
