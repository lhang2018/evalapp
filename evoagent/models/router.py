"""模型路由（Architecture 7 / PRD 19-20）。

按任务类别/难度/成本/时延/可用性选择模型；允许不同领域用不同模型，
不允许因某模型整体便宜而把所有任务无条件切换过去。
"""

from typing import Any, Dict, List, Optional, Tuple

from .base import ModelAdapter, ModelProfile, ModelRegistry
from .metering import PRICING_UNKNOWN, pricing_status

# 质量优先：成本/时延只在质量可接受时作为次要因素（Arch 7 / PRD 19）
DEFAULT_WEIGHTS = {"quality": 0.70, "cost": 0.15, "latency": 0.15}


class ModelRouter:
    def __init__(self, registry: ModelRegistry,
                 default_adapter: ModelAdapter,
                 routes: Optional[Dict[str, str]] = None,
                 weights: Optional[Dict[str, float]] = None):
        """
        routes: intent/domain -> model_id 映射（经注册表解析到 adapter 工厂）。
        adapter_factory: model_id -> ModelAdapter，由宿主注入。
        """
        self.registry = registry
        self.default_adapter = default_adapter
        self.adapter_factory = None  # 由 setup() 注入
        self._routes: Dict[str, str] = routes or {}
        # 迁移启用后的默认模型（受控启用，优先于评分但仍让显式路由优先）
        self.default_model_id: Optional[str] = None
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            self.weights.update(weights)

    def setup(self, adapter_factory) -> None:
        self.adapter_factory = adapter_factory

    def bind_route(self, key: str, model_id: str) -> None:
        self._routes[key] = model_id

    def routes(self) -> Dict[str, str]:
        return dict(self._routes)

    def resolve(self, model_id: Optional[str]) -> Optional[ModelAdapter]:
        """按 model_id 解析适配器；未知/不可用模型返回 None（不静默回退）。

        迁移执行链依赖该语义区分「模型真实可用」与「回退到默认模型」。
        """
        if not model_id or self.adapter_factory is None:
            return None
        profile = self.registry.get(model_id)
        if profile is None or not profile.available:
            return None
        try:
            adapter = self.adapter_factory(model_id)
            if adapter is None:
                return None
            if adapter.profile().model_id != model_id or not adapter.profile().available:
                return None
            return adapter
        except Exception:  # noqa: BLE001 —— 解析失败视为不可用
            return None

    def set_default_model(self, model_id: Optional[str]) -> None:
        self.default_model_id = model_id

    def route(self, intent: str, domain: str,
              require_vision: bool = False,
              prefer_cost: bool = False) -> ModelAdapter:
        return self.decide(intent, domain, require_vision=require_vision,
                           prefer_cost=prefer_cost)[0]

    def decide(self, intent: str, domain: str,
               require_vision: bool = False,
               prefer_cost: bool = False) -> Tuple[ModelAdapter, str]:
        """返回 (适配器, 决策来源)。

        来源：explicit（显式路由）/ default（迁移默认模型）/ cheap（成本优先）
        / score（评分）/ fallback（无可用候选，回退默认适配器）。
        评测用来源判断“回退率”，不靠适配器对象身份猜。
        """
        profiles = self.registry.all()
        # 显式路由优先
        model_id = self._routes.get(intent) or self._routes.get(domain)
        if model_id:
            prof = self.registry.get(model_id)
            if prof and prof.available and (not require_vision or prof.supports_vision):
                adapter = self.resolve(model_id)
                if adapter is not None and (not require_vision or adapter.profile().supports_vision):
                    return adapter, "explicit"
        # 迁移受控启用：默认模型优先于评分；解析失败则继续评分
        if self.default_model_id:
            adapter = self.resolve(self.default_model_id)
            if adapter is not None and (
                    not require_vision
                    or adapter.profile().supports_vision):
                return adapter, "default"
        # 否则按能力过滤 + 质量/成本/时延评分（Arch 7 / PRD 19）
        candidates = [p for p in profiles if p.available
                      and (not require_vision or p.supports_vision)]
        if candidates and prefer_cost:
            priced = [p for p in candidates if pricing_status(p) != PRICING_UNKNOWN]
            for profile in sorted(priced, key=lambda p: float(p.price_per_1k_input) + float(p.price_per_1k_output)):
                adapter = self.resolve(profile.model_id)
                if adapter is not None and (not require_vision or adapter.profile().supports_vision):
                    return adapter, "cheap"
        if candidates:
            for profile in sorted(candidates, key=lambda p: self.score(p, candidates), reverse=True):
                adapter = self.resolve(profile.model_id)
                if adapter is not None and (not require_vision or adapter.profile().supports_vision):
                    return adapter, "score"
        fallback = self.default_adapter.profile()
        registered = self.registry.get(fallback.model_id)
        if (not fallback.available or (registered is not None and not registered.available) or
                (require_vision and not fallback.supports_vision)):
            raise ValueError("没有满足能力与可用性要求的模型，拒绝不兼容回退")
        return self.default_adapter, "fallback"

    def score(self, profile: ModelProfile,
              pool: Optional[List[ModelProfile]] = None) -> float:
        """归一化质量/成本/时延加权分；可用性在候选过滤阶段处理。"""
        pool = pool or self.registry.all()
        prices = [float(p.price_per_1k_input) + float(p.price_per_1k_output) for p in pool
                  if pricing_status(p) != PRICING_UNKNOWN]
        max_price = max(prices) if prices else 0.0
        known = pricing_status(profile) != PRICING_UNKNOWN
        price = float(profile.price_per_1k_input) + float(profile.price_per_1k_output) if known else None
        cost_score = (1.0 - (price / max_price) if max_price > 0 else 1.0) if known else 0.0
        latency_score = 1.0 / (1.0 + max(0.0, profile.avg_latency_s))
        return round(
            self.weights["quality"] * profile.quality_score
            + self.weights["cost"] * cost_score
            + self.weights["latency"] * latency_score, 6)

    def rank(self, *, require_vision: bool = False,
             prefer_cost: bool = False) -> List[Dict[str, Any]]:
        """路由决策解释：候选模型及评分。"""
        cands = [p for p in self.registry.all() if p.available
                 and (not require_vision or p.supports_vision)]
        rows = [{"model_id": p.model_id, "score": self.score(p, cands),
                 "price": (float(p.price_per_1k_input) + float(p.price_per_1k_output))
                          if pricing_status(p) != PRICING_UNKNOWN else None,
                 "pricing_status": pricing_status(p),
                 "quality": p.quality_score, "latency_s": p.avg_latency_s}
                for p in cands]
        if prefer_cost:
            rows.sort(key=lambda r: (r["price"] is None, r["price"] or 0.0, -r["score"]))
        else:
            rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

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
