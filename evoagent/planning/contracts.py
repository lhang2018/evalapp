"""工作流匹配与执行模式契约（ALN-07-W）。

官方设计《总体架构》§6 定义三种任务执行模式：

- ``stable_workflow`` 稳定工作流：适合重复且可验收的任务，
  **先检查输入、权限、依赖、版本和预算**；
- ``hybrid_agent`` 混合 Agent 工作流：主流程确定，局部节点需要动态判断；
- ``dynamic_planning`` 动态规划：没有适用工作流时，在工具白名单、最大步数、
  时限、预算和停止条件内探索。

本模块只定义数据契约。候选集合由调用方按意图/领域预筛（Router 决定候选路径），
匹配服务只做五项检查与模式选择，**不引入第二套授权或预算权威、不扣账**。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

#: 三种执行模式（与设计 §6 一一对应）
EXECUTION_MODES = ("stable_workflow", "hybrid_agent", "dynamic_planning")

#: 稳定工作流必须通过的检查（顺序固定，便于逐项留痕与比较）
CHECK_NAMES = ("inputs", "permissions", "dependencies", "versions", "budget")


@dataclass(frozen=True)
class WorkflowCandidate:
    """一条候选工作流（可由能力资产/工作流注册表提供）。"""

    workflow_id: str
    version: str = "1"
    #: 需要的输入名
    required_inputs: Tuple[str, ...] = ()
    #: 需要的权限
    required_permissions: Tuple[str, ...] = ()
    #: 依赖（已就绪的依赖标识，如 ``skill:pdf@1``）
    dependencies: Tuple[str, ...] = ()
    #: 支持的模型版本；空=不限
    supported_model_versions: Tuple[str, ...] = ()
    #: 预估成本（与预算同单位；0 表示不声明成本）
    cost_estimate: int = 0
    #: 绑定的预算引用（只作记录，匹配阶段不扣账）
    budget_ref: str = ""
    #: 需要动态判断的节点（混合模式使用）
    dynamic_nodes: Tuple[str, ...] = ()
    #: 允许在运行时动态补齐的检查项（混合模式的适用条件）
    dynamic_tolerates: Tuple[str, ...] = ()
    description: str = ""

    def key(self) -> Tuple[str, str]:
        return (self.workflow_id, self.version)


@dataclass(frozen=True)
class MatchContext:
    """匹配上下文（只读快照）。"""

    goal: str = ""
    intent: str = ""
    available_inputs: FrozenSet[str] = frozenset()
    granted_permissions: FrozenSet[str] = frozenset()
    available_dependencies: FrozenSet[str] = frozenset()
    model_version: str = ""
    #: 剩余可用额度；与预算同单位
    remaining_budget: int = 0
    #: 预算是否可知；不可知时不得假设充足
    budget_known: bool = True

    @staticmethod
    def build(goal: str = "", intent: str = "", *,
              inputs: Iterable[str] = (),
              permissions: Iterable[str] = (),
              dependencies: Iterable[str] = (),
              model_version: str = "",
              remaining_budget: int = 0,
              budget_known: bool = True) -> "MatchContext":
        return MatchContext(
            goal=goal, intent=intent,
            available_inputs=frozenset(inputs),
            granted_permissions=frozenset(permissions),
            available_dependencies=frozenset(dependencies),
            model_version=model_version,
            remaining_budget=int(remaining_budget),
            budget_known=budget_known)


@dataclass(frozen=True)
class CheckResult:
    """单项检查结果（拒绝原因必须可查）。"""

    name: str
    ok: bool
    missing: Tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok,
                "missing": list(self.missing), "detail": self.detail}


@dataclass(frozen=True)
class CandidateVerdict:
    """一个候选的判定。"""

    workflow_id: str
    version: str
    applicable: bool
    checks: Tuple[CheckResult, ...] = ()
    #: 汇总缺失项（人类可读），空表示全部满足
    missing: Tuple[str, ...] = ()
    cost_estimate: int = 0
    dynamic_nodes: Tuple[str, ...] = ()
    #: 仅缺一项且该项允许动态补齐
    dynamic_gap: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"workflow_id": self.workflow_id, "version": self.version,
                "applicable": self.applicable,
                "checks": [c.to_dict() for c in self.checks],
                "missing": list(self.missing), "cost_estimate": self.cost_estimate,
                "dynamic_nodes": list(self.dynamic_nodes),
                "dynamic_gap": self.dynamic_gap}


@dataclass(frozen=True)
class ModeSelection:
    """显式的执行模式选择结果（含理由）。"""

    mode: str
    workflow_id: str = ""
    version: str = ""
    rationale: str = ""
    checks: Tuple[CheckResult, ...] = ()
    verdicts: Tuple[CandidateVerdict, ...] = ()
    dynamic_nodes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "workflow_id": self.workflow_id,
                "version": self.version, "rationale": self.rationale,
                "checks": [c.to_dict() for c in self.checks],
                "verdicts": [v.to_dict() for v in self.verdicts],
                "dynamic_nodes": list(self.dynamic_nodes)}

    @property
    def stable(self) -> bool:
        return self.mode == "stable_workflow"
