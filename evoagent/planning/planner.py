"""Planning 引擎（PRD 11 / Architecture 3.6）。

职责：把目标转成可执行任务结构（分解/依赖/顺序/并行/预算/风险/验收）。
动态规划保留原始目标、硬约束、已完成工作与变更理由。
已验证的固定流程直接走工作流，不要求每次重新发明计划（PRD 11.3）。
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..models.base import ChatMessage


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
        steps: Optional[List[PlanStep]] = None
        change_reasons: List[str] = []
        if self.model is not None:
            try:
                steps = self._model_plan(goal, intent, constraints or [],
                                         acceptance or [])
            except Exception as e:  # noqa: BLE001
                change_reasons.append(f"model_plan_failed:{type(e).__name__}")
        if not steps:
            steps = self._default_steps(goal, intent)
            if self.model is not None and not change_reasons:
                change_reasons.append("model_plan_failed:empty")
        plan = ExecutionPlan(
            plan_id=plan_id, goal=goal, steps=steps,
            original_goal=goal,
            immutable_constraints=list(constraints or []),
            acceptance_criteria=list(acceptance) if acceptance
            else [{"type": "result_nonempty"}],
            change_reasons=change_reasons,
            risks=self._risks_for(intent),
            reserved_budget_hint={"max_model_calls": 10},
        )
        return plan

    # ---- 模型辅助规划（PRD 11.2；失败必须可追溯地回退）----
    def _model_plan(self, goal: str, intent: str,
                    constraints: List[str],
                    acceptance: List[Dict[str, Any]]) -> List[PlanStep]:
        system = ChatMessage(role="system", content=(
            "你是任务规划器。只输出 JSON，不要解释或 Markdown 围栏："
            '{"steps":[{"step_id":"s1","description":"...",'
            '"deps":[],"tool_hint":null}]}。'
            "约束：1-12 步；step_id 唯一；deps 只能引用已出现的步骤；"
            "依赖必须无环；每步 description 可执行且可验证。"))
        user = ChatMessage(role="user", content=(
            f"goal:{goal}\nintent:{intent}\n"
            f"constraints:{json.dumps(constraints, ensure_ascii=False)}\n"
            f"acceptance:{json.dumps(acceptance, ensure_ascii=False)}"))
        raw = self.model.generate([system, user])
        data = _parse_json(raw)
        steps_raw = data.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ValueError("计划缺少 steps")
        if len(steps_raw) > 12:
            raise ValueError("步骤超过 12 个")
        steps: List[PlanStep] = []
        seen = set()
        for item in steps_raw:
            if not isinstance(item, dict):
                raise ValueError("步骤格式错误")
            sid = str(item.get("step_id") or "").strip()
            desc = str(item.get("description") or "").strip()
            if not sid or not desc:
                raise ValueError("步骤缺少 step_id 或 description")
            if sid in seen:
                raise ValueError(f"step_id 重复: {sid}")
            seen.add(sid)
            deps = item.get("deps") or []
            if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
                raise ValueError("deps 必须是字符串列表")
            steps.append(PlanStep(step_id=sid, description=desc,
                                  deps=list(deps),
                                  tool_hint=item.get("tool_hint") or None))
        _check_acyclic(steps)
        return steps

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


def _parse_json(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise ValueError("模型未返回内容")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("计划必须是 JSON 对象")
    return data


def _check_acyclic(steps: List[PlanStep]) -> None:
    ids = {s.step_id for s in steps}
    graph = {s.step_id: s.deps for s in steps}
    for s in steps:
        for d in s.deps:
            if d not in ids:
                raise ValueError(f"依赖不存在: {s.step_id} -> {d}")
    state: Dict[str, int] = {}   # 0=visiting, 1=done

    def visit(sid: str) -> None:
        mark = state.get(sid)
        if mark == 0:
            raise ValueError(f"存在依赖环: {sid}")
        if mark == 1:
            return
        state[sid] = 0
        for d in graph.get(sid, []):
            visit(d)
        state[sid] = 1

    for sid in graph:
        visit(sid)
