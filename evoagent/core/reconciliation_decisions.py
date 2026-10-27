"""WAITING_USER(reconciliation) 人工裁决消费（ALN-07-C 缺口补齐）。

原则（任务卡 R-12 与 07-C 验收）：

- 只对处于 ``WAITING_USER`` 且 ``waiting_request.phase == "reconciliation"`` 的任务生效；
- 只允许对门禁报告的**未决目标**裁决：未声明/未决/歧义**效果**（effect_id）与
  未执行**步骤**（step_id）；不在清单内一律拒绝；
- 裁决**只记录证据**：不改变任务状态、不触发重放；恢复仍走既有 resume 路径，
  对未知副作用绝不盲目重放；
- 同一目标改判需要 ``override=True``（冲突默认拒绝，避免静默改写历史）；
  相同裁决幂等。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .task import Task, TaskStatus

VALID_DECISIONS = ("executed", "not_executed", "retry")
EFFECT_BUCKETS = ("undeclared", "uncertain", "ambiguous_effects")
STEP_BUCKETS = ("unexecuted",)


class ReconciliationDecisionError(ValueError):
    """裁决请求非法（任务状态/目标/取值/记录人）。"""


class ReconciliationDecisionConflict(ReconciliationDecisionError):
    """同一目标已有不同裁决且未显式覆盖。"""


def _targets(summary: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    targets: Dict[str, Dict[str, str]] = {}
    for bucket in EFFECT_BUCKETS:
        for effect_id in (summary.get(bucket) or []):
            key = str(effect_id)
            if key:
                targets.setdefault(key, {"target_id": key, "target_kind": "effect",
                                         "bucket": bucket})
    for bucket in STEP_BUCKETS:
        for step_id in (summary.get(bucket) or []):
            key = str(step_id)
            if key:
                targets.setdefault(key, {"target_id": key, "target_kind": "step",
                                         "bucket": bucket})
    return targets


def pending(task: Task) -> Dict[str, Any]:
    """返回该任务当前可裁决的目标（非 reconciliation 等待时 reconcilable=False）。"""
    request = getattr(task, "waiting_request", None) or {}
    phase = request.get("phase")
    if phase != "reconciliation":
        return {"phase": phase, "reconcilable": False, "targets": []}
    summary = request.get("summary") or {}
    targets = _targets(summary if isinstance(summary, dict) else {})
    return {
        "phase": phase,
        "reconcilable": True,
        "request_id": request.get("request_id"),
        "summary": summary,
        "targets": sorted(targets.values(),
                          key=lambda t: (t["target_kind"], t["target_id"])),
    }


class ReconciliationDecisions:
    """人工裁决的持久化（幂等域表 ``reconciliation_decisions``）。"""

    def __init__(self, db, now=None):
        self.db = db
        self._now = now or time.time

    def for_task(self, task_id: str) -> List[Dict[str, Any]]:
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT task_id, target_id, target_kind, decision, actor,"
                " reason, created_at FROM reconciliation_decisions"
                " WHERE task_id=? ORDER BY created_at, target_id",
                (task_id,)).fetchall()
        return [{"task_id": row["task_id"], "target_id": row["target_id"],
                 "target_kind": row["target_kind"], "decision": row["decision"],
                 "actor": row["actor"], "reason": row["reason"],
                 "created_at": row["created_at"]} for row in rows]

    def record(self, task: Task, decisions: Any, *, actor: str, reason: str,
               override: bool = False) -> Dict[str, Any]:
        if not isinstance(task, Task):
            raise ReconciliationDecisionError("缺少任务对象")
        if task.task_status != TaskStatus.WAITING_USER:
            raise ReconciliationDecisionError(
                "任务不在 WAITING_USER，无法记录副作用裁决")
        info = pending(task)
        if not info["reconcilable"]:
            raise ReconciliationDecisionError("当前等待不是副作用对账裁决")
        if not isinstance(actor, str) or not actor.strip():
            raise ReconciliationDecisionError("裁决必须记录 actor")
        if not isinstance(reason, str) or not reason.strip():
            raise ReconciliationDecisionError("裁决必须记录 reason")
        if not isinstance(decisions, list) or not decisions:
            raise ReconciliationDecisionError("decisions 必须是非空数组")

        known = {t["target_id"]: t for t in info["targets"]}
        seen: set = set()
        normalized: List[Tuple[str, str, str]] = []
        for item in decisions:
            if not isinstance(item, dict):
                raise ReconciliationDecisionError("每条裁决必须是对象")
            target_id = str(item.get("target_id") or "").strip()
            decision = str(item.get("decision") or "").strip()
            if decision not in VALID_DECISIONS:
                raise ReconciliationDecisionError(
                    "decision 必须是 " + "/".join(VALID_DECISIONS))
            if target_id not in known:
                raise ReconciliationDecisionError(
                    f"目标不在未决清单: {target_id}")
            if target_id in seen:
                raise ReconciliationDecisionError(f"同一目标重复裁决: {target_id}")
            seen.add(target_id)
            normalized.append((target_id, known[target_id]["target_kind"],
                               decision))

        recorded: List[str] = []
        already: List[str] = []
        overridden: List[str] = []
        now = float(self._now())
        with self.db._lock:
            self.db._conn.execute("BEGIN IMMEDIATE")
            try:
                for target_id, target_kind, decision in normalized:
                    row = self.db._conn.execute(
                        "SELECT decision FROM reconciliation_decisions"
                        " WHERE task_id=? AND target_id=?",
                        (task.task_id, target_id)).fetchone()
                    if row is not None:
                        if row["decision"] == decision:
                            already.append(target_id)
                            continue
                        if not override:
                            raise ReconciliationDecisionConflict(
                                f"目标已有不同裁决，需显式 override: {target_id}")
                        overridden.append(target_id)
                    data = {"task_id": task.task_id, "target_id": target_id,
                            "target_kind": target_kind, "decision": decision,
                            "actor": actor, "reason": reason, "at": now}
                    self.db._conn.execute(
                        "INSERT INTO reconciliation_decisions"
                        " (task_id, target_id, target_kind, decision, actor,"
                        " reason, created_at, data)"
                        " VALUES (?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(task_id, target_id) DO UPDATE SET"
                        " decision=excluded.decision, actor=excluded.actor,"
                        " reason=excluded.reason, created_at=excluded.created_at,"
                        " data=excluded.data",
                        (task.task_id, target_id, target_kind, decision, actor,
                         reason, now, json.dumps(data, ensure_ascii=False)))
                    recorded.append(target_id)
                self.db._conn.commit()
            except BaseException:
                self.db._conn.rollback()
                raise

        stored = self.for_task(task.task_id)
        decided = {row["target_id"] for row in stored}
        remaining = [t["target_id"] for t in info["targets"]
                     if t["target_id"] not in decided]
        return {
            "recorded": recorded,
            "already": already,
            "overridden": overridden,
            "decisions": stored,
            "unresolved_remaining": remaining,
            "resume_ready": not remaining,
            "auto_resumed": False,
            "note": "仅记录裁决证据；恢复仍由既有 resume 路径执行，不自动重放",
        }
