"""评测任务执行器：把稳定轨运行时接入评测层（Architecture 11.2）。

候选评测与最终验收使用同一执行入口，但评测数据与验收数据隔离（PRD 52）。
"""

import time
from typing import Dict, Tuple

from ..core.task import TaskStatus
from .evaluator import EvalCase, TaskRunner


class ControllerTaskRunner(TaskRunner):
    def __init__(self, controller, ledger=None, scope: str = "evolution"):
        self.controller = controller
        self.ledger = ledger
        self.scope = scope

    def run_case(self, case: EvalCase) -> Tuple[bool, float, Dict]:
        acceptance = case.expected or [{"type": "result_nonempty"}]
        started = time.time()
        usage_before = self._usage()
        try:
            task = self.controller.submit_task(
                case.input_goal, intent=case.intent, domain=case.domain,
                acceptance=acceptance, wait=True)
        except Exception as e:  # noqa: BLE001 —— 评测失败不能中断整批
            return False, time.time() - started, {
                "error": f"{type(e).__name__}: {e}", "cost": 0.0}
        latency = time.time() - started
        usage_after = self._usage()
        delta = {k: usage_after.get(k, 0) - usage_before.get(k, 0)
                 for k in ("model_calls", "tool_calls", "cost", "seconds")}
        if self.ledger is not None:
            self.ledger.record(self.scope, **{k: delta[k] for k in
                                              ("model_calls", "tool_calls",
                                               "cost", "seconds")})
        ok = task.task_status == TaskStatus.SUCCEEDED
        progress = (task.runtime_manifest or {}).get("plan_progress") or {}
        recovery_attempted = bool(progress.get("change_reasons")
                                  or progress.get("failed"))
        tool_ok = not any(
            ev.get("type", "").startswith("tool") and not ev.get("passed")
            for ev in (task.result_evidence or []))
        evidence = {"task_id": task.task_id,
                    "status": task.task_status.value,
                    "cost": delta.get("cost", 0.0),
                    "model_calls": delta.get("model_calls", 0),
                    "tool_calls": delta.get("tool_calls", 0),
                    "tool_ok": tool_ok,
                    "recovery_attempted": recovery_attempted,
                    "recovered": ok if recovery_attempted else False,
                    "constraint_violations": 0}
        if not ok:
            evidence["failure"] = task.failure_reason
        return ok, latency, evidence

    def _usage(self) -> Dict:
        if self.ledger is None:
            return {}
        return self.ledger.usage(self.scope)
