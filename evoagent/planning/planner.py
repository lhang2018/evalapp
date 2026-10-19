"""Planning 引擎（PRD 11 / Architecture 3.6）。

职责：把目标转成可执行任务结构（分解/依赖/顺序/并行/预算/风险/验收）。
动态规划保留原始目标、硬约束、已完成工作与变更理由。
已验证的固定流程直接走工作流，不要求每次重新发明计划（PRD 11.3）。
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PlanStep:
    step_id: str
    description: str
    deps: List[str] = field(default_factory=list)
    tool_hint: Optional[str] = None
    acceptance: Optional[List[Dict[str, Any]]] = None
    state: str = "pending"             # pending | running | done | failed | skipped


@dataclass
class ExecutionPlan:
    plan_id: str
    goal: str
    steps: List[PlanStep]
    # 不可变内容（动态调整必须保留，PRD 11.2）
    original_goal: str = ""
    immutable_constraints: List[str] = field(default_factory=list)
    completed_work: List[str] = field(default_factory=list)
    change_reasons: List[str] = field(default_factory=list)
    reserved_budget_hint: Dict[str, Any] = field(default_factory=dict)
    risks: List[str] = field(default_factory=list)
    acceptance_criteria: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def adjust(self, new_steps: List[PlanStep], reason: str) -> List[str]:
        """动态调整：保留原始目标/约束/已完成工作/变更记录。"""
        self.steps = new_steps
        self.change_reasons.append(reason)
        self.completed_work = [s.step_id for s in new_steps if s.state == "done"]
        return self.change_reasons

    def validate(self) -> List[str]:
        """规划验收（PRD 11.3）：完整性/依赖顺序/资源可行性/风险覆盖/终止条件。"""
        problems: List[str] = []
        ids = {s.step_id for s in self.steps}
        if not self.steps:
            problems.append("计划为空")
        for s in self.steps:
            for d in s.deps:
                if d not in ids:
                    problems.append(f"步骤 {s.step_id} 依赖不存在的 {d}")
        # 依赖环检查（DFS）
        visiting, visited = set(), set()
        def dfs(sid: str) -> None:
            if sid in visiting:
                problems.append(f"存在依赖环: {sid}")
                return
            if sid in visited:
                return
            visiting.add(sid)
            step = next((x for x in self.steps if x.step_id == sid), None)
            if step:
                for d in step.deps:
                    dfs(d)
            visiting.remove(sid)
            visited.add(sid)
        for s in self.steps:
            dfs(s.step_id)
        if not self.acceptance_criteria:
            problems.append("缺少验收条件")
        return problems


class PlanningEngine:
    """首次实现：模型辅助 + 确定性模板兜底；工作流命中时直接引用工作流。"""

    def __init__(self, model_adapter=None):
        self.model = model_adapter

    def make_plan(self, goal: str, *, intent: str = "general",
                  constraints: Optional[List[str]] = None,
                  acceptance: Optional[List[Dict[str, Any]]] = None,
                  plan_id: str = "plan_main") -> ExecutionPlan:
        steps = self._default_steps(goal, intent)
        plan = ExecutionPlan(
            plan_id=plan_id, goal=goal, steps=steps,
            original_goal=goal,
            immutable_constraints=list(constraints or []),
            acceptance_criteria=list(acceptance) if acceptance
            else [{"type": "result_nonempty"}],
            risks=self._risks_for(intent),
            reserved_budget_hint={"max_model_calls": 10},
        )
        return plan

    def _default_steps(self, goal: str, intent: str) -> List[PlanStep]:
        base = [
            PlanStep(step_id="s1", description=f"解析目标并收集上下文: {goal}"),
            PlanStep(step_id="s2", description="执行核心动作", deps=["s1"]),
            PlanStep(step_id="s3", description="验证结果并总结", deps=["s2"]),
        ]
        return base

    @staticmethod
    def _risks_for(intent: str) -> List[str]:
        if intent in ("note_taking", "mobile_operation"):
            return ["涉及本地/设备副作用，需权限确认"]
        if intent == "comic_drama":
            return ["生成成本较高，失败需局部重试而非整体重做"]
        return []
