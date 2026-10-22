"""领域专家（PRD 9 / Architecture 4.3-4.4）。

专家 = 独立能力配置 + 记忆命名空间 + 工具白名单 + 工作流 + 模型适配 + 评测标准。
领域隔离：一个专家的改动默认只影响该专家；不得绕过共享内核。
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


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

    def capabilities(self) -> List[CapabilityEntry]:
        return list(self._capabilities.values())

    def register_capability(self, cap: CapabilityEntry, depends_on: List[str] | None = None) -> None:
        self._capabilities[cap.capability_id] = cap
        if depends_on:
            self._dep_edges[cap.capability_id] = list(depends_on)

    # ---- 路由 ----
    def route(self, intent: str, domain: str = "") -> Expert:
        """多层路由第二层：领域能力路由（Architecture 4.2）。"""
        expert, _ = self.route_with_match(intent, domain)
        return expert

    def route_with_match(self, intent: str, domain: str = ""
                         ) -> Tuple[Expert, bool]:
        """路由并返回是否命中专属能力（E-07 能力缺口信号源：兜底=False）。

        命中 = 意图显式命中某专家的 applicable_intents，或按领域取到；
        兜底 = 落到 general/任一专家但意图未显式声明——能力目录缺口。
        """
        intent_match = [e for e in self._experts.values()
                        if intent in e.applicable_intents]
        if intent_match:
            return intent_match[0], True
        by_name = self._experts.get(domain)
        if by_name is not None:
            return by_name, True
        general = [e for e in self._experts.values() if e.expert_id == "general"]
        return (general[0] if general else
                (list(self._experts.values())[0] if self._experts else None),
                False)

    # ---- 影响分析（PRD 10.3，GAP C-09：传递闭包 + 分层回归范围）----
    def _depends_on_closure(self, cap_id: str) -> set:
        """上游依赖传递闭包（A→B→C 时 A 的上游 = B、C）。"""
        seen: set = set()
        stack = [x for x in self._dep_edges.get(cap_id, []) if x != cap_id]
        while stack:
            cur = stack.pop()
            if cur in seen or cur == cap_id:
                continue
            seen.add(cur)
            stack.extend(x for x in self._dep_edges.get(cur, [])
                         if x not in seen)
        return seen

    def _affected_closure(self, cap_id: str) -> set:
        """下游受影响传递闭包（谁最终依赖 cap_id）。环安全（visited）。"""
        rev: Dict[str, List[str]] = {}
        for cid, deps in self._dep_edges.items():
            for d in deps:
                rev.setdefault(d, []).append(cid)
        seen: set = set()
        stack = [x for x in rev.get(cap_id, []) if x != cap_id]
        while stack:
            cur = stack.pop()
            if cur in seen or cur == cap_id:
                continue
            seen.add(cur)
            stack.extend(x for x in rev.get(cur, [])
                         if x not in seen)
        return seen

    def impact_analysis(self, capability_id: str) -> Dict[str, Any]:
        """能力变更的影响分析（传递闭包，C-09 修复）。

        must_rerun_tests 不再硬编码：按受影响闭包的能力分层定回归范围——
        - 触及 shared 共享内核（如 file_toolset）：全量回归
          （builtin_smoke + builtin_holdout + 安全套件 builtin_safety）
        - 仅专家领域层受影响：领域回归（builtin_holdout）
        - 无受影响能力：无需重跑（证据可复用）
        名单对应真实评测资产（evolution/datasets.py 与 SafetySuite suite_id），
        不发明不存在的测试名。
        """
        direct = list(self._dep_edges.get(capability_id, []))
        reverse = [cid for cid, deps in self._dep_edges.items()
                   if capability_id in deps]
        up = self._depends_on_closure(capability_id)
        down = self._affected_closure(capability_id)
        affected = sorted(down)
        source = self._capabilities.get(capability_id)
        touches_shared = any(
            getattr(self._capabilities.get(cid), "domain", "") == "shared"
            for cid in affected)
        # 变更源本身是 shared 内核 → 同为结构性变更，全量回归
        if source is not None and getattr(source, "domain", "") == "shared":
            touches_shared = True
        if touches_shared:
            rerun = ["builtin_smoke", "builtin_holdout", "builtin_safety"]
        elif affected:
            rerun = ["builtin_holdout"]
        else:
            rerun = []
        return {
            "directly_affected": reverse,          # 下游一跳（兼容保留）
            "depends_on": direct,                  # 上游一跳（兼容保留）
            "affected_closure": affected,          # 下游传递闭包
            "upstream_closure": sorted(up),        # 上游传递闭包
            "must_rerun_tests": rerun,
            "can_reuse_evidence": not affected,
        }
