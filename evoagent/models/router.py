"""模型路由（Architecture 7 / PRD 19-20）。

按任务类别/难度/成本/时延/可用性选择模型；允许不同领域用不同模型，
不允许因某模型整体便宜而把所有任务无条件切换过去。
"""

from typing import Dict, List, Optional

from .base import ModelAdapter, ModelRegistry


class ModelRouter:
    def __init__(self, registry: ModelRegistry,
                 default_adapter: ModelAdapter,
                 routes: Optional[Dict[str, str]] = None):
        """
        routes: intent/domain -> model_id 映射（经注册表解析到 adapter 工厂）。
        adapter_factory: model_id -> ModelAdapter，由宿主注入。
        """
        self.registry = registry
        self.default_adapter = default_adapter
        self.adapter_factory = None  # 由 setup() 注入
        self._routes: Dict[str, str] = routes or {}

    def setup(self, adapter_factory) -> None:
        self.adapter_factory = adapter_factory

    def bind_route(self, key: str, model_id: str) -> None:
        self._routes[key] = model_id

    def route(self, intent: str, domain: str,
              require_vision: bool = False,
              prefer_cost: bool = False) -> ModelAdapter:
        profiles = self.registry.all()
        # 显式路由优先
        model_id = self._routes.get(intent) or self._routes.get(domain)
        if model_id:
            prof = self.registry.get(model_id)
            if prof and (not require_vision or prof.supports_vision):
                if self.adapter_factory:
                    return self.adapter_factory(model_id)
        # 否则按能力过滤 + 成本/质量启发式
        candidates = [p for p in profiles
                      if not require_vision or p.supports_vision]
        if candidates and prefer_cost:
            best = min(candidates, key=lambda p: p.price_per_1k_input)
            if self.adapter_factory:
                return self.adapter_factory(best.model_id)
        return self.default_adapter

    # ---- 迁移（PRD 20）----
    def plan_migration(self, old_model_id: str, new_model_id: str) -> List[str]:
        steps = [
            "创建新模型档案",
            "识别可继承的能力资产",
            "识别受影响组件",
            "运行 API 与工具兼容性测试",
            "运行必要的领域回归",
            "比较质量、成本、时延",
            "生成新发布组合",
            "受控验证后启用",
            "保留旧模型稳定版本与回滚能力",
        ]
        return steps
