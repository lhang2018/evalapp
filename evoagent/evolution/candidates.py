"""进化候选与生命周期状态机（PRD 48-50 / Architecture 10.4-10.6）。"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from ..models import ModelRegistry  # noqa: F401 仅为包结构说明保留
from ..react.engine import ReactOutcome  # noqa: F401


class CandidateStatus(str, Enum):
    DETECTED = "DETECTED"
    TRIAGED = "TRIAGED"
    HYPOTHESIZED = "HYPOTHESIZED"
    CANDIDATE_CREATED = "CANDIDATE_CREATED"
    TESTING = "TESTING"
    EVALUATING = "EVALUATING"
    REJECTED = "REJECTED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    RELEASE_READY = "RELEASE_READY"
    DEPLOYING = "DEPLOYING"
    MONITORING = "MONITORING"
    PROMOTED = "PROMOTED"
    ROLLED_BACK = "ROLLED_BACK"


TERMINAL = {CandidateStatus.REJECTED, CandidateStatus.PROMOTED,
            CandidateStatus.ROLLED_BACK}

# 与 Architecture 10.4 状态图一致的合法转换
ALLOWED: Dict[CandidateStatus, List[CandidateStatus]] = {
    CandidateStatus.DETECTED: [CandidateStatus.TRIAGED],
    CandidateStatus.TRIAGED: [CandidateStatus.HYPOTHESIZED, CandidateStatus.REJECTED],
    CandidateStatus.HYPOTHESIZED: [CandidateStatus.CANDIDATE_CREATED, CandidateStatus.REJECTED],
    CandidateStatus.CANDIDATE_CREATED: [CandidateStatus.TESTING],
    CandidateStatus.TESTING: [CandidateStatus.EVALUATING],
    CandidateStatus.EVALUATING: [CandidateStatus.REJECTED, CandidateStatus.APPROVAL_PENDING],
    CandidateStatus.APPROVAL_PENDING: [CandidateStatus.RELEASE_READY, CandidateStatus.REJECTED],
    CandidateStatus.RELEASE_READY: [CandidateStatus.DEPLOYING],
    CandidateStatus.DEPLOYING: [CandidateStatus.MONITORING],
    CandidateStatus.MONITORING: [CandidateStatus.PROMOTED, CandidateStatus.ROLLED_BACK],
    CandidateStatus.REJECTED: [],
    CandidateStatus.PROMOTED: [],
    CandidateStatus.ROLLED_BACK: [],
}


class IllegalCandidateTransition(Exception):
    pass


@dataclass
class EvolutionCandidate:
    candidate_id: str
    baseline_version: str
    target_component: str            # 受影响组件（PRD 49 目标组件）
    target_problem: str
    root_cause_evidence: List[str] = field(default_factory=list)
    hypothesis: str = ""
    change_scope: str = "local"      # local | expert | shared | critical
    eval_dataset_id: Optional[str] = None
    quality_result: Optional[Dict[str, Any]] = None
    holdout_result: Optional[Dict[str, Any]] = None
    safety_result: Optional[Dict[str, Any]] = None
    # PRD 50：十维收益评价（evaluate_benefit 产出）
    benefit_result: Optional[Dict[str, Any]] = None
    # PRD 82：发布后监测结论
    monitor_result: Optional[Dict[str, Any]] = None
    deployed_at: Optional[float] = None
    deployed_release_id: str = ""
    target_domain: str = ""
    cost_delta: Optional[float] = None
    latency_delta: Optional[float] = None
    risk_level: str = "L2"           # PRD 80 风险分级
    compatible_requirements: List[str] = field(default_factory=list)
    rollback_version: str = ""
    publish_suggestion: str = ""
    final_result: str = ""
    status: CandidateStatus = CandidateStatus.DETECTED
    # 状态变更依据（Arch 10.4：每次转换记录依据）
    transition_log: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # PRD 10.3 影响分析（GAP 子步④：propose_candidate 落传递闭包，
    # 供回归范围选择消费；None=无可用依赖图）
    impact_result: Optional[Dict[str, Any]] = None

    def transition(self, target: CandidateStatus, reason: str,
                   evidence: Optional[Dict[str, Any]] = None) -> CandidateStatus:
        if target not in ALLOWED.get(self.status, []):
            raise IllegalCandidateTransition(
                f"非法候选转换: {self.status.value} -> {target.value}")
        self.transition_log.append({
            "from": self.status.value, "to": target.value,
            "reason": reason, "evidence": evidence or {},
            "at": time.time()})
        self.status = target
        return self.status

    def to_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EvolutionCandidate":
        d = dict(d)
        d["status"] = CandidateStatus(d.get("status", "DETECTED"))
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})

    def benefit_report(self) -> Dict[str, Any]:
        """收益评价（PRD 50）：十维向量由 evaluate_benefit 产出。"""
        return self.benefit_result or {"status": "not_evaluated"}
