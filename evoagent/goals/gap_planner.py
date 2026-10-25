"""能力缺口证据与主动规划候选（ALN-26-A）。

从**真实**信号识别能力缺口，再生成有范围/收益/成本的候选计划：

1. 证据来源（可追溯）：
   - 失败/取消任务（`status in FAILED/CANCELLED`，要求有 failure_reason）；
   - 能力清单兜底路由（`ExpertRegistry.route_with_match` 返回 fallback=True，
     即意图没有显式能力声明——E-07 缺口信号）；
   - 未满足的用户目标（目标非终态且其下存在失败任务）。
2. 缺口聚合与去重：按「能力/领域签名」聚合，证据按 `ref` 去重；每个缺口
   至少要达到 `min_evidence` 条独立证据，且**至少一条失败任务证据带失败
   原因**（或两条以上兜底路由证据），否则归入 `insufficient` 并写清原因。
3. 主动规划候选：对可晋升缺口生成带 `benefit`（受影响任务数/频率/严重度）
   与 `cost`（按证据数估算的单位预算，非真实账单）的候选；候选**只能**通过
   既有 `EvolutionController.propose_candidate` 登记，**不绕过评测与发布**，
   也不在这里部署。
4. 可追溯校验：计划前逐条核对 `task:*` / `goal:*` 引用仍存在；失配即拒绝
   规划（缺口必须是证据可回查的，不是泛泛建议）。
"""

import hashlib
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from ..core.task import TaskStatus

FAILED_STATUSES = {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
GOAL_TERMINAL = {"completed", "cancelled", "expired"}
POLICY = (
    "缺口必须有可追溯证据；证据不足不生成候选；候选只经既有评测与"
    "发布流程，规划器不做部署"
)


@dataclass(frozen=True)
class GapEvidence:
    kind: str  # failed_task / fallback_route / unmet_goal
    ref: str  # task:<id> / goal:<id>
    capability: str  # 能力/缺口签名
    domain: str = ""
    intent: str = ""
    task_id: str = ""
    goal_id: str = ""
    failure_reason: str = ""
    observed_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class GapPlannerError(ValueError):
    """缺口证据/规划请求非法。"""


def _positive_int(value, name):
    if type(value) is not int or value < 1:
        raise GapPlannerError(name + "必须是正整数")
    return value


def _snapshot(record):
    data = dict(record)
    data["evidence"] = [
        {k: v for k, v in row.items() if k != "observed_at"}
        for row in record.get("evidence", [])
    ]
    return data


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CapabilityGapPlanner:
    def __init__(
        self,
        db,
        *,
        experts=None,
        evolution=None,
        goals=None,
        clock=None,
        min_evidence: int = 2,
    ):
        self.db = db
        self.experts = experts
        self.evolution = evolution
        self.goals = goals
        self.clock = clock or time.time
        self.min_evidence = _positive_int(min_evidence, "min_evidence")

    # ---- 证据收集 ----
    def _capability_key(self, task: Any, matched: bool) -> str:
        intent = getattr(task, "intent", "") or "general"
        domain = getattr(task, "domain", "") or "general"
        if matched:
            return f"capability:{domain}:{intent}"
        return f"missing_capability:{domain}:{intent}"

    def collect_evidence(
        self, *, task_limit: int = 200, goal_limit: int = 100
    ) -> List[GapEvidence]:
        now = self.clock()
        evidence: List[GapEvidence] = []
        tasks = self.db.list_tasks(
            limit=min(_positive_int(task_limit, "task_limit"), 1000)
        )
        for task in tasks:
            raw_status = getattr(task, "task_status", getattr(task, "status", ""))
            status = getattr(raw_status, "value", raw_status)
            matched = True
            if self.experts is not None:
                _expert, matched = self.experts.route_with_match(
                    getattr(task, "intent", "general"),
                    getattr(task, "domain", "general"),
                )
            capability = self._capability_key(task, matched)
            if status in FAILED_STATUSES:
                evidence.append(
                    GapEvidence(
                        kind="failed_task",
                        ref=f"task:{task.task_id}",
                        capability=capability,
                        domain=getattr(task, "domain", ""),
                        intent=getattr(task, "intent", ""),
                        task_id=task.task_id,
                        goal_id=getattr(task, "goal_id", None) or "",
                        failure_reason=(getattr(task, "failure_reason", "") or ""),
                        observed_at=now,
                    )
                )
            elif not matched:
                evidence.append(
                    GapEvidence(
                        kind="fallback_route",
                        ref=f"task:{task.task_id}",
                        capability=capability,
                        domain=getattr(task, "domain", ""),
                        intent=getattr(task, "intent", ""),
                        task_id=task.task_id,
                        goal_id=getattr(task, "goal_id", None) or "",
                        observed_at=now,
                    )
                )
        # 未满足目标：非终态目标 + 其下失败任务（证据已收集，补 goal 引用）
        if self.goals is not None:
            try:
                goal_rows = {
                    row["goal_id"]: row
                    for row in self.goals.list(limit=max(1, min(int(goal_limit), 1000)))
                }
            except (ValueError, TypeError):
                goal_rows = {}
            goal_context = {
                (item.goal_id, item.capability): item
                for item in evidence
                if item.kind == "failed_task" and item.goal_id
            }
            for (goal_id, capability), source in sorted(goal_context.items()):
                row = goal_rows.get(goal_id)
                if row is None or row.get("status") in GOAL_TERMINAL:
                    continue
                evidence.append(
                    GapEvidence(
                        kind="unmet_goal",
                        ref=f"goal:{goal_id}",
                        capability=capability,
                        domain=source.domain,
                        intent=source.intent,
                        goal_id=goal_id,
                        failure_reason=str(row.get("title") or ""),
                        observed_at=now,
                    )
                )
        return evidence

    # ---- 聚合分析 ----
    def analyze(
        self, *, task_limit: int = 200, min_evidence: Optional[int] = None
    ) -> Dict[str, Any]:
        threshold = (
            self.min_evidence
            if min_evidence is None
            else _positive_int(min_evidence, "min_evidence")
        )
        if threshold < 1:
            raise GapPlannerError("min_evidence 必须 >= 1")
        evidence = self.collect_evidence(task_limit=task_limit)
        grouped: Dict[str, Dict[str, GapEvidence]] = {}
        for item in evidence:
            grouped.setdefault(item.capability, {})[item.ref] = item
        gaps: List[Dict[str, Any]] = []
        insufficient: List[Dict[str, Any]] = []
        for capability, by_ref in sorted(grouped.items()):
            rows = sorted(by_ref.values(), key=lambda e: (e.kind, e.ref))
            kinds = sorted({row.kind for row in rows})
            failure_reasons = sorted(
                {
                    row.failure_reason
                    for row in rows
                    if row.kind == "failed_task" and row.failure_reason
                }
            )
            has_failed_with_reason = bool(failure_reasons)
            fallback_only = kinds == ["fallback_route"]
            independent_count = len(
                {
                    row.task_id
                    for row in rows
                    if row.task_id and row.kind in ("failed_task", "fallback_route")
                }
            )
            required_count = max(2, threshold) if fallback_only else threshold
            promotable = independent_count >= required_count and (
                has_failed_with_reason or fallback_only
            )
            record = {
                "gap_id": "gap_" + _sha(capability)[:12],
                "capability": capability,
                "domain": rows[0].domain,
                "intent": rows[0].intent,
                "kinds": kinds,
                "evidence": [row.to_dict() for row in rows],
                "evidence_refs": [row.ref for row in rows],
                "evidence_count": len(rows),
                "independent_evidence_count": independent_count,
                "distinct_tasks": len({row.task_id for row in rows if row.task_id}),
                "distinct_goals": len({row.goal_id for row in rows if row.goal_id}),
                "failure_reasons": failure_reasons,
                "severity": (
                    "high"
                    if len(rows) >= 3
                    or len({row.task_id for row in rows if row.task_id}) >= 3
                    else "medium"
                ),
                "promotable": bool(promotable),
            }
            if promotable:
                gaps.append(record)
            else:
                reasons = []
                if independent_count < required_count:
                    reasons.append(
                        f"独立证据不足：{independent_count} 条 < 阈值 {required_count}"
                    )
                if not has_failed_with_reason and not fallback_only:
                    reasons.append("缺少带失败原因的任务证据或足够的兜底路由证据")
                insufficient.append(
                    {**record, "insufficient_reason": "；".join(reasons)}
                )
        return {
            "gaps": gaps,
            "insufficient": insufficient,
            "counts": {
                "evidence": len(evidence),
                "gaps": len(gaps),
                "insufficient": len(insufficient),
            },
            "policy": POLICY,
            "analyzed_at": self.clock(),
            "task_limit": min(_positive_int(task_limit, "task_limit"), 1000),
        }

    # ---- 可追溯校验 ----
    def traceability(self, gap: Dict[str, Any]) -> Dict[str, Any]:
        refs = gap.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            return {"ok": False, "missing": ["<empty_or_invalid_evidence_refs>"]}
        missing: List[str] = []
        for ref in refs:
            if not isinstance(ref, str) or not ref.strip():
                missing.append(repr(ref))
                continue
            kind, _, ident = ref.partition(":")
            if kind == "task":
                if self.db.get_task(ident) is None:
                    missing.append(ref)
            elif kind == "goal":
                if self.goals is None:
                    missing.append(ref)
                    continue
                try:
                    if self.goals.get(ident) is None:
                        missing.append(ref)
                except (ValueError, TypeError, KeyError):
                    missing.append(ref)
            else:
                missing.append(ref)
        return {"ok": not missing, "missing": missing}

    # ---- 主动规划候选 ----
    def plan_candidates(
        self,
        *,
        baseline_version: str = "v0",
        actor: str = "gap-planner",
        min_evidence: Optional[int] = None,
        analysis: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if analysis is not None and (
            not isinstance(analysis, dict) or not isinstance(analysis.get("gaps"), list)
        ):
            raise GapPlannerError("analysis必须是完整分析快照")
        fresh = self.analyze(
            task_limit=analysis.get("task_limit", 200) if analysis is not None else 200,
            min_evidence=min_evidence,
        )
        current_gaps = {row["gap_id"]: row for row in fresh["gaps"]}
        analysis = fresh if analysis is None else analysis
        planned: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        for gap in analysis["gaps"]:
            trace = self.traceability(gap)
            if not trace["ok"]:
                rejected.append(
                    {
                        "gap_id": gap["gap_id"],
                        "capability": gap["capability"],
                        "reason": "证据引用不可追溯: " + ", ".join(trace["missing"]),
                    }
                )
                continue
            current = current_gaps.get(gap["gap_id"])
            if current is None or _snapshot(current) != _snapshot(gap):
                rejected.append(
                    {
                        "gap_id": gap["gap_id"],
                        "capability": gap["capability"],
                        "reason": "证据已变化或分析快照与当前权威记录不一致",
                    }
                )
                continue
            gap = current
            if self.evolution is None:
                rejected.append(
                    {
                        "gap_id": gap["gap_id"],
                        "capability": gap["capability"],
                        "reason": "未配置进化候选登记服务，未创建候选",
                    }
                )
                continue
            benefit = {
                "affected_tasks": gap["distinct_tasks"],
                "evidence_count": gap["evidence_count"],
                "severity": gap["severity"],
            }
            cost = {
                "budget_units": round(1.0 + 0.5 * gap["evidence_count"], 2),
                "basis": "按证据条数估算的单位预算（非真实账单）",
            }
            candidate_id = None
            candidate_status = None
            if self.evolution is not None:
                problem = (
                    gap["failure_reasons"][0]
                    if gap["failure_reasons"]
                    else "能力清单兜底路由命中缺口: " + gap["capability"]
                )
                hypothesis = (
                    "补齐/修复该能力后，同类任务应不再失败或落到兜底："
                    + gap["capability"]
                )
                lock = self.evolution.__dict__.setdefault(
                    "_gap_planning_lock", threading.RLock()
                )
                with lock:
                    candidate = next(
                        (
                            c
                            for c in self.evolution.all_candidates()
                            if c.baseline_version == baseline_version
                            and c.target_component == gap["capability"]
                            and c.target_domain == gap["domain"]
                            and c.target_problem == problem
                            and set(c.root_cause_evidence) == set(gap["evidence_refs"])
                        ),
                        None,
                    )
                    reused = candidate is not None
                    if candidate is None:
                        candidate = self.evolution.propose_candidate(
                            baseline_version,
                            gap["capability"],
                            problem,
                            hypothesis,
                            change_scope="local",
                            evidence=gap["evidence_refs"],
                            target_domain=gap["domain"],
                        )
                candidate_id = candidate.candidate_id
                candidate_status = getattr(
                    getattr(candidate, "status", None),
                    "value",
                    getattr(candidate, "status", None),
                )
            planned.append(
                {
                    "gap_id": gap["gap_id"],
                    "capability": gap["capability"],
                    "domain": gap["domain"],
                    "benefit": benefit,
                    "cost": cost,
                    "evidence_refs": gap["evidence_refs"],
                    "candidate_id": candidate_id,
                    "candidate_status": candidate_status,
                    "status": "existing" if reused else "proposed",
                    "requires_evaluation": True,
                    "policy": "候选仅登记，不绕过评测/门禁/发布流程",
                    "actor": actor,
                }
            )
        return {
            "planned": planned,
            "rejected": rejected,
            "insufficient": fresh["insufficient"],
            "counts": {
                "planned": len(planned),
                "rejected": len(rejected),
                "insufficient": len(fresh["insufficient"]),
            },
            "policy": POLICY,
        }
