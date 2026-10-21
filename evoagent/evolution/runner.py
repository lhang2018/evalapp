"""评测任务执行器：把稳定轨运行时接入评测层（Architecture 11.2）。

候选评测与最终验收使用同一执行入口，但评测数据与验收数据隔离（PRD 52）。
评测执行器支持注入单候选实验预算（PRD 55-56），预算耗尽后剩余用例
直接记为失败，而不是让异常中断整批评测。
"""

import time
from typing import Dict, Optional, Tuple

from ..core.budget import BudgetController
from ..core.task import TaskStatus
from .evaluator import EvalCase, TaskRunner


class ControllerTaskRunner(TaskRunner):
    def __init__(self, controller, ledger=None, scope: str = "evolution",
                 model_id: Optional[str] = None):
        self.controller = controller
        self.ledger = ledger
        self.scope = scope
        self.budget: Optional[BudgetController] = None
        # 迁移回归：固定目标模型执行，与常规路由隔离（PRD 20）
        self.model_id = model_id
        # 契约 §5 evaluation_suite_ref（GAP ⑤c）：评测期间由进化控制器
        # 设置为 "{dataset_id}/{version}"，经 manifest_extras 落入任务
        # 与 run manifest
        self.suite_ref: str = ""

    def set_budget(self, budget: Optional[BudgetController]) -> None:
        """注入/清除单候选实验预算（由进化控制器在评测期间设置）。"""
        self.budget = budget

    def set_model(self, model_id: Optional[str]) -> None:
        self.model_id = model_id

    def run_case(self, case: EvalCase) -> Tuple[bool, float, Dict]:
        acceptance = case.expected or [{"type": "result_nonempty"}]
        started = time.time()
        if self.budget is not None:
            problem = self.budget.exceeded()
            if problem:
                return False, 0.0, {
                    "error": f"候选实验预算耗尽: {problem}",
                    "budget_exhausted": problem, "cost": 0.0}
        try:
            # GAP ⑤c：评测套件引用随任务运行时清单落档
            extras = ({"evaluation_suite_ref": self.suite_ref}
                      if self.suite_ref else None)
            task = self.controller.submit_task(
                case.input_goal, intent=case.intent, domain=case.domain,
                acceptance=acceptance, wait=True, source="evaluation",
                model_override=self.model_id, manifest_extras=extras)
        except Exception as e:  # noqa: BLE001 —— 评测失败不能中断整批
            return False, time.time() - started, {
                "error": f"{type(e).__name__}: {e}", "cost": 0.0}
        latency = time.time() - started
        usage = _task_usage(task)
        if self.budget is not None:
            self.budget.record_usage(
                model_calls=int(usage.get("model_calls", 0) or 0),
                tool_calls=int(usage.get("tool_calls", 0) or 0),
                cost=float(usage.get("cost", 0.0) or 0.0))
        elif self.ledger is not None:
            self.ledger.record(
                self.scope,
                model_calls=int(usage.get("model_calls", 0) or 0),
                tool_calls=int(usage.get("tool_calls", 0) or 0),
                cost=float(usage.get("cost", 0.0) or 0.0),
                seconds=float(usage.get("seconds", 0.0) or 0.0))
        ok = task.task_status == TaskStatus.SUCCEEDED
        progress = (task.runtime_manifest or {}).get("plan_progress") or {}
        recovery_attempted = bool(progress.get("change_reasons")
                                  or progress.get("failed"))
        tool_ok = not any(
            ev.get("type", "").startswith("tool") and not ev.get("passed")
            for ev in (task.result_evidence or []))
        evidence = {"task_id": task.task_id,
                    "status": task.task_status.value,
                    "cost": float(usage.get("cost", 0.0) or 0.0),
                    "model_calls": int(usage.get("model_calls", 0) or 0),
                    "tool_calls": int(usage.get("tool_calls", 0) or 0),
                    "tool_ok": tool_ok,
                    "recovery_attempted": recovery_attempted,
                    "recovered": ok if recovery_attempted else False,
                    "constraint_violations": 0}
        if not ok:
            evidence["failure"] = task.failure_reason
        return ok, latency, evidence


def _task_usage(task) -> Dict[str, float]:
    """读取任务自身的预算用量，避免用账本差值误计并发任务。"""
    for ev in reversed(task.result_evidence or []):
        if not isinstance(ev, dict):
            continue
        usage = (ev.get("budget") or {}).get("usage")
        if isinstance(usage, dict):
            return usage
    return {}
