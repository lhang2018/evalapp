"""执行模式主链路由（ALN-08-W：工作流与执行模式的主链路由）。

把 ALN-07-W 的 ``WorkflowMatcher.select`` 接入运行时主链（此前只有评测
``evolution/routing_eval.py`` 在调它，主运行时仍按「专家登记的第一个工作流」
静态拍板，理由是写死的、拒绝原因看不见）。本模块负责三件事：

1. **入口与评测一致**：候选构建、五项检查与选择都走同一个
   ``WorkflowMatcher.select``；评测与本模块对同一上下文必须得到同一结果。
2. **拒绝/降级证据**：每个候选的五项检查结果与缺失项都保留
   （``selection.verdicts``），选择理由写进 ``rationale``；
   「不可用」不得被当成隐式默认——要么明确选 ``dynamic_planning``，
   要么明确选中一个五项全过的候选。
3. **决策与真实执行归因一致**：执行结束后由控制器记录
   ``mode_attribution``（决策模式 vs 实际执行模式），不一致必须显式标出。

本模块是纯读的：不授权、不扣预算、不改持久状态。
"""

import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..planning.contracts import MatchContext, ModeSelection, WorkflowCandidate
from ..planning.matching import WorkflowMatcher

STABLE = "stable_workflow"
HYBRID = "hybrid_agent"
DYNAMIC = "dynamic_planning"
MODES = (STABLE, HYBRID, DYNAMIC)


def candidate_from_workflow(workflow: Any) -> WorkflowCandidate:
    """把已注册的 ``WorkflowDef`` 映射成匹配候选。

    只映射可机器判定的字段：
    - ``required_inputs`` = ``input_schema.required``（用户必须提供的输入名）；
    - ``required_permissions`` = 节点工具需求（``tool:<id>``）；
    - ``cost_estimate`` = 节点数（与预算同为「次」的量级近似）；
    - 未声明模型版本约束（空集合 = 不限）。
    """
    schema = getattr(workflow, "input_schema", None) or {}
    required = schema.get("required") or ()
    tools = sorted({node.tool for node in getattr(workflow, "nodes", [])
                    if getattr(node, "tool", None)})
    return WorkflowCandidate(
        workflow_id=workflow.workflow_id,
        version=getattr(workflow, "version", "") or "1",
        required_inputs=tuple(str(item) for item in required),
        required_permissions=tuple(f"tool:{tool_id}" for tool_id in tools),
        cost_estimate=len(getattr(workflow, "nodes", []) or []),
        description=getattr(workflow, "name", "") or "")


class ModeRouter:
    """主链执行模式路由；与评测共用 ``WorkflowMatcher`` 入口。"""

    def __init__(self, workflow_lookup: Any):
        self.workflow_lookup = workflow_lookup

    # ---- 候选与上下文 ----
    def candidates(self, workflow_ids: Iterable[str]) -> List[WorkflowCandidate]:
        found: List[WorkflowCandidate] = []
        for workflow_id in workflow_ids:
            workflow = self.workflow_lookup(workflow_id) if self.workflow_lookup else None
            if workflow is None:
                continue  # 定义不存在：不参与候选，也不静默当成可用
            found.append(candidate_from_workflow(workflow))
        return found

    @staticmethod
    def _budget_state(budget: Any) -> Tuple[bool, int]:
        """返回 (预算是否可知, 剩余可用额度)。

        预算对象缺失/快照异常 → 不可知（匹配阶段不得假设充足）；
        未声明任何上限 → 可知且视为不受限（返回 0 但 known=True）。
        """
        if budget is None:
            return False, 0
        try:
            snapshot = budget.snapshot()
            exceeded = budget.exceeded()
        except Exception:  # noqa: BLE001 —— 无法核验即视为不可知
            return False, 0
        if exceeded:
            return True, 0
        limits = (snapshot or {}).get("limits") or {}
        usage = (snapshot or {}).get("usage") or {}
        remaining: List[float] = []
        for key in ("max_model_calls", "max_tool_calls"):
            limit = limits.get(key)
            if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0:
                used = usage.get(key) or 0
                remaining.append(float(limit) - float(used))
        if not remaining:
            return True, 0  # 无上限声明：已知且不受限
        return True, int(max(0, min(remaining)))

    def context(self, *, goal: str, intent: str,
                candidates: Sequence[WorkflowCandidate],
                provided_inputs: Iterable[str] = (),
                tool_whitelist: Optional[Iterable[str]] = None,
                model_version: str = "", budget: Any = None) -> MatchContext:
        known, remaining = self._budget_state(budget)
        if tool_whitelist is None:
            granted = {permission for candidate in candidates
                       for permission in candidate.required_permissions}
        else:
            granted = {f"tool:{tool_id}" for tool_id in tool_whitelist}
        return MatchContext.build(
            goal=goal, intent=intent,
            inputs=provided_inputs,
            permissions=granted,
            dependencies=(),
            model_version=model_version or "",
            remaining_budget=remaining,
            budget_known=known)

    # ---- 决策 ----
    def decide(self, *, goal: str, intent: str, workflow_ids: Iterable[str],
               provided_inputs: Iterable[str] = (),
               tool_whitelist: Optional[Iterable[str]] = None,
               model_version: str = "",
               budget: Any = None) -> ModeSelection:
        candidates = self.candidates(workflow_ids)
        context = self.context(
            goal=goal, intent=intent, candidates=candidates,
            provided_inputs=provided_inputs, tool_whitelist=tool_whitelist,
            model_version=model_version, budget=budget)
        return WorkflowMatcher(candidates).select(context)

    # ---- 证据 ----
    @staticmethod
    def rejections(selection: ModeSelection) -> List[Dict[str, Any]]:
        """被拒绝候选项的机器可读清单（含缺失项，供运行清单/审计）。"""
        out = []
        for verdict in selection.verdicts:
            if verdict.applicable:
                continue
            out.append({"workflow_id": verdict.workflow_id,
                        "version": verdict.version,
                        "missing": list(verdict.missing),
                        "dynamic_gap": verdict.dynamic_gap})
        return out

    @staticmethod
    def selection_from_manifest(raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict) or not raw.get("mode"):
            return None
        return raw

    @staticmethod
    def attribution(decision: Any, actual_mode: str,
                    workflow_id: str = "") -> Dict[str, Any]:
        """核对「决策模式 vs 实际执行」；不一致必须显式标出。"""
        decided = decision if isinstance(decision, dict) else decision.to_dict()
        decided_mode = str(decided.get("mode") or "")
        decided_workflow = str(decided.get("workflow_id") or "")
        consistent = (decided_mode == actual_mode
                      and (not decided_workflow or decided_workflow == workflow_id))
        return {"decision": decided_mode,
                "decision_workflow_id": decided_workflow,
                "actual": actual_mode,
                "actual_workflow_id": workflow_id,
                "consistent": bool(consistent),
                "at": time.time()}
