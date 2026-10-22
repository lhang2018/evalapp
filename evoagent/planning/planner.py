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

from ..core.budget import BudgetController, BudgetExceeded
from ..models.base import ChatMessage
from ..models.metering import adapter_model_id, settle_adapter_cost


INTENT_TOOL_HINTS: Dict[str, str] = {
    "time_query": "current_time",
    "computation": "calculate",
    "weather": "http_get",
    "note_taking": "file_write",
}


@dataclass
class PlanStep:
    step_id: str
    description: str
    deps: List[str] = field(default_factory=list)
    tool_hint: Optional[str] = None
    acceptance: Optional[List[Dict[str, Any]]] = None
    state: str = "pending"             # pending | running | done | failed | skipped

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "description": self.description,
                "deps": list(self.deps), "tool_hint": self.tool_hint,
                "acceptance": self.acceptance, "state": self.state}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PlanStep":
        return cls(step_id=str(d.get("step_id") or ""),
                   description=str(d.get("description") or ""),
                   deps=list(d.get("deps") or []),
                   tool_hint=d.get("tool_hint"),
                   acceptance=d.get("acceptance"),
                   state=str(d.get("state") or "pending"))


class PlanRevisionError(ValueError):
    """修订违反不变量时抛出；计划保持修订前状态（ALN-07-P）。

    不变量违约不是可恢复的执行失败，而是规划层的契约违反：
    放行它意味着续跑可能重放成功副作用，因此一律拒绝并留痕。
    """


# 修订分级（任务卡 07-计划修订与证据保持的第一份交付）：
#   append      —— 只允许追加（如连续失败后的恢复步骤）
#   replace     —— 可替换**未开始**步骤，已完成证据必须原样保留
#   replan      —— 重规划未开始子图，同样不得动已完成子图
#   reauthorize —— 目标/约束变更，必须走人工授权，adjust 不静默接受
REVISION_LEVELS = ("append", "replace", "replan", "reauthorize")


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
    revisions: List[Dict[str, Any]] = field(default_factory=list)

    def adjust(self, new_steps: List[PlanStep], reason: str,
               *, level: str = "replace") -> List[str]:
        """动态调整（PRD 11.2 / ALN-07-P）：分级修订，不变量违约即拒绝。

        不变量（违反即抛 PlanRevisionError，计划保持修订前状态）：
        1. 本方法不改写 original_goal / immutable_constraints / acceptance_criteria；
        2. completed_work 单调不减——已完成步骤不得被丢弃或降级
           （否则续跑会重放成功副作用）；
        3. 修订后的步骤集合必须结构合法（无重复 id、deps 存在、无环、非空）；
        4. append 级别只允许追加，不得删除既有步骤；
        5. 每次修订留痕（级别/原因/增删/时间），供崩溃恢复后读取。

        引擎在步骤成功时把 step_id 追加进 completed_work；此处**合并**而非
        重算，就是为了不让修订抹掉那份证据。
        """
        if level not in REVISION_LEVELS:
            raise PlanRevisionError(f"未知修订级别: {level}")
        if level == "reauthorize":
            raise PlanRevisionError(
                "目标/约束变更必须走人工授权接口，adjust 不静默接受")
        new_steps = list(new_steps or [])
        if not new_steps:
            raise PlanRevisionError("修订后计划不能为空")

        # —— 先校验，全部通过才提交变更 ——
        problems = self._structural_problems(new_steps)
        if problems:
            raise PlanRevisionError(
                "修订后计划结构不合法: " + "; ".join(problems))

        new_by_id: Dict[str, PlanStep] = {}
        for s in new_steps:
            if s.step_id in new_by_id:
                raise PlanRevisionError(f"修订后 step_id 重复: {s.step_id}")
            new_by_id[s.step_id] = s

        for sid in self.completed_work:
            nxt = new_by_id.get(sid)
            if nxt is None:
                raise PlanRevisionError(
                    f"修订丢弃已完成步骤 {sid}——已完成证据只增不减")
            if nxt.state != "done":
                raise PlanRevisionError(
                    f"修订把已完成步骤 {sid} 降级为 {nxt.state}"
                    "——不允许重跑成功副作用")

        if level == "append":
            for s in self.steps:
                if s.step_id not in new_by_id:
                    raise PlanRevisionError(
                        f"append 修订删除既有步骤 {s.step_id}——只允许追加")

        old_ids = [s.step_id for s in self.steps]
        added = [s.step_id for s in new_steps if s.step_id not in
                 set(old_ids)]
        removed = [sid for sid in old_ids if sid not in new_by_id]

        # —— 提交 ——
        self.steps = new_steps
        merged = list(self.completed_work)
        for s in new_steps:
            if s.state == "done" and s.step_id not in merged:
                merged.append(s.step_id)
        self.completed_work = merged
        self.change_reasons.append(reason)
        self.revisions.append({
            "level": level,
            "reason": reason,
            "added": added,
            "removed": removed,
            "completed_work": list(self.completed_work),
            "at": time.time(),
        })
        return self.change_reasons

    @staticmethod
    def _structural_problems(steps: List[PlanStep]) -> List[str]:
        """结构校验：非空、id 唯一、deps 存在、无环。"""
        problems: List[str] = []
        if not steps:
            return ["计划为空"]
        ids = {s.step_id for s in steps}
        if len(ids) != len(steps):
            problems.append("step_id 重复")
        for s in steps:
            for d in s.deps:
                if d not in ids:
                    problems.append(f"步骤 {s.step_id} 依赖不存在的 {d}")
        visiting: set = set()
        visited: set = set()

        def dfs(sid: str) -> None:
            if sid in visiting:
                problems.append(f"存在依赖环: {sid}")
                return
            if sid in visited:
                return
            visiting.add(sid)
            step = next((x for x in steps if x.step_id == sid), None)
            if step:
                for d in step.deps:
                    dfs(d)
            visiting.remove(sid)
            visited.add(sid)

        for s in steps:
            dfs(s.step_id)
        return problems

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id, "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "original_goal": self.original_goal,
            "immutable_constraints": list(self.immutable_constraints),
            "completed_work": list(self.completed_work),
            "change_reasons": list(self.change_reasons),
            "reserved_budget_hint": dict(self.reserved_budget_hint),
            "risks": list(self.risks),
            "acceptance_criteria": list(self.acceptance_criteria),
            "created_at": self.created_at,
            "revisions": [dict(r) for r in self.revisions],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExecutionPlan":
        return cls(
            plan_id=str(d.get("plan_id") or "plan_main"),
            goal=str(d.get("goal") or ""),
            steps=[PlanStep.from_dict(s) for s in (d.get("steps") or [])
                   if isinstance(s, dict)],
            original_goal=str(d.get("original_goal") or d.get("goal") or ""),
            immutable_constraints=list(d.get("immutable_constraints") or []),
            completed_work=list(d.get("completed_work") or []),
            change_reasons=list(d.get("change_reasons") or []),
            reserved_budget_hint=dict(d.get("reserved_budget_hint") or {}),
            risks=list(d.get("risks") or []),
            acceptance_criteria=list(d.get("acceptance_criteria") or []),
            created_at=float(d.get("created_at") or time.time()),
            revisions=[dict(r) for r in (d.get("revisions") or [])
                       if isinstance(r, dict)],
        )

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
                  plan_id: str = "plan_main",
                  model=None,
                  budget: Optional[BudgetController] = None) -> ExecutionPlan:
        steps: Optional[List[PlanStep]] = None
        change_reasons: List[str] = []
        adapter = model or self.model
        if adapter is not None:
            try:
                steps = self._model_plan(goal, intent, constraints or [],
                                         acceptance or [], model=adapter,
                                         budget=budget)
            except BudgetExceeded:
                raise
            except Exception as e:  # noqa: BLE001
                change_reasons.append(f"model_plan_failed:{type(e).__name__}")
        if not steps:
            steps = self._default_steps(goal, intent)
            if adapter is not None and not change_reasons:
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
                    acceptance: List[Dict[str, Any]],
                    *, model=None,
                    budget: Optional[BudgetController] = None) -> List[PlanStep]:
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
        adapter = model or self.model
        model_id = adapter_model_id(adapter)
        if budget is not None:
            budget.check_model_call()
            # 先预登记次数，再按计量结果结算金额；预估只作旧适配器兜底，
            # 有提供方实测时不会顶替实测成本。
            budget.record_model_call(0.0, model_id)
        try:
            raw = adapter.generate([system, user])
        except Exception:
            if budget is not None:
                settle_adapter_cost(budget, adapter, model_id)
            raise
        if budget is not None:
            settle_adapter_cost(
                budget, adapter, model_id,
                fallback_cost=_estimate_cost(
                    adapter, len(system.content) + len(user.content),
                    len(raw)))
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
        # 已知意图给核心步骤一个工具提示，让回退计划也能声明所需工具（PRD 7.3）
        hint = INTENT_TOOL_HINTS.get(intent)
        base = [
            PlanStep(step_id="s1", description=f"解析目标并收集上下文: {goal}"),
            PlanStep(step_id="s2", description="执行核心动作", deps=["s1"],
                     tool_hint=hint),
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


def _estimate_cost(model, input_chars: int, output_chars: int) -> float:
    fn = getattr(model, "estimate_cost", None)
    if not callable(fn):
        return 0.0
    try:
        return float(fn(input_chars, output_chars))
    except Exception:  # noqa: BLE001 —— 计量失败不阻断规划
        return 0.0


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
