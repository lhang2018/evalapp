"""领域专家（PRD 9 / Architecture 4.3-4.4）。

专家 = 独立能力配置 + 记忆命名空间 + 工具白名单 + 工作流 + 模型适配 + 评测标准。
领域隔离：一个专家的改动默认只影响该专家；不得绕过共享内核。
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Expert:
    expert_id: str
    version: str = "1.0.0"
    domain_description: str = ""
    capabilities: List[str] = field(default_factory=list)
    applicable_intents: List[str] = field(default_factory=list)
    not_applicable: List[str] = field(default_factory=list)
    system_prompt: str = ""
    tool_whitelist: Optional[List[str]] = None          # None=全部可用
    memory_scope_key: str = ""
    workflow_ids: List[str] = field(default_factory=list)
    model_preference: Optional[str] = None              # 建议模型（路由可覆盖）
    budget_class: str = "standard"                       # standard | tight | generous
    quality_metrics: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    release_record: Dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class CapabilityEntry:
    """统一能力注册表条目（Architecture 4.1）。"""
    capability_id: str
    version: str
    domain: str
    description: str
    input_contract: str = ""
    output_contract: str = ""
    applicable: List[str] = field(default_factory=list)
    not_applicable: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    compatible_models: List[str] = field(default_factory=list)
    tool_ids: List[str] = field(default_factory=list)
    eval_suite: str = ""
    permission_requirements: List[str] = field(default_factory=list)
    budget_policy: str = "task_default"
    status: str = "active"                      # active | experimental | disabled


class ExpertRegistry:
    """专家注册表 + 能力依赖图（Architecture 4.1 / PRD 10）。"""

    def __init__(self) -> None:
        self._experts: Dict[str, Expert] = {}
        self._capabilities: Dict[str, CapabilityEntry] = {}
        # 依赖边: capability_id -> [依赖的 capability_id 列表]（PRD 10.2）
        self._dep_edges: Dict[str, List[str]] = {}

    def register_expert(self, expert: Expert) -> None:
        self._experts[expert.expert_id] = expert

    def get(self, expert_id: str) -> Optional[Expert]:
        return self._experts.get(expert_id)

    def all(self) -> List[Expert]:
        return list(self._experts.values())

    def register_capability(self, cap: CapabilityEntry, depends_on: List[str] | None = None) -> None:
        self._capabilities[cap.capability_id] = cap
        if depends_on:
            self._dep_edges[cap.capability_id] = list(depends_on)

    # ---- 路由 ----
    def route(self, intent: str, domain: str = "") -> Expert:
        """多层路由第二层：领域能力路由（Architecture 4.2）。"""
        intent_match = [e for e in self._experts.values()
                        if intent in e.applicable_intents]
        if intent_match:
            return intent_match[0]
        by_name = self._experts.get(domain)
        if by_name:
            return by_name
        general = [e for e in self._experts.values() if e.expert_id == "general"]
        return general[0] if general else list(self._experts.values())[0]

    # ---- 影响分析（PRD 10.3）----
    def impact_analysis(self, capability_id: str) -> Dict[str, List[str]]:
        direct = list(self._dep_edges.get(capability_id, []))
        reverse = [cid for cid, deps in self._dep_edges.items()
                   if capability_id in deps]
        return {
            "directly_affected": reverse,          # 下游直接受影响
            "depends_on": direct,                  # 上游依赖
            "must_rerun_tests": ["capability_eval"] if reverse else [],
            "can_reuse_evidence": True if not reverse else False,
        }
