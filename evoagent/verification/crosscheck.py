"""交叉核验：把结论与来源集合映射为五态判定（ALN-28-A）。

判定顺序（fail-closed）：
1. 结论适用时间已过 → `stale`（即使有来源支持）；
2. 存在反对来源 → `contradicted`（支持与反对都逐条可见）；
3. 有可访问来源直接支持 → `supported`（多发布者时置信度更高）；
4. 来源只提及、未直接支持 → `unknown`（不升级为可信）；
5. 有内部已核验事实推导 → `inferred`（非外部直接支持）；
6. 其余 → `unknown`；模型自述**永远**不能单独判为可信。

不可访问的来源单独记录，不计入支持/反对。
"""

import time
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .claims import Claim, ClaimState
from .sources import SourceRecord


@dataclass(frozen=True)
class Verdict:
    claim_id: str
    statement: str
    state: str
    confidence: float
    supporting: List[Dict[str, Any]] = field(default_factory=list)
    conflicting: List[Dict[str, Any]] = field(default_factory=list)
    mentions: List[Dict[str, Any]] = field(default_factory=list)
    inaccessible: List[Dict[str, Any]] = field(default_factory=list)
    applicable_window: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    model_only: bool = False
    policy: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"claim_id": self.claim_id, "statement": self.statement,
                "state": self.state, "confidence": self.confidence,
                "supporting": self.supporting,
                "conflicting": self.conflicting,
                "mentions": self.mentions,
                "inaccessible": self.inaccessible,
                "applicable_window": self.applicable_window,
                "reasons": self.reasons, "model_only": self.model_only,
                "policy": self.policy}


def _publishers(citations: List[Dict[str, Any]]) -> set:
    return {c.get("publisher") for c in citations if c.get("publisher")}


class FactCrossChecker:
    """外部事实交叉核验器（无状态、可注入时钟）。"""

    POLICY = "无外部证据不得仅凭模型自述判可信"

    def __init__(self, *, clock=None):
        self.clock = clock or time.time

    def verify(self, claim: Claim, sources: List[SourceRecord], *,
               now: Optional[float] = None) -> Verdict:
        claim.__post_init__()
        now = self.clock() if now is None else now
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError("核验时钟必须为有限时间戳")
        matched = [s for s in sources if s.claim_key == claim.claim_key]
        accessible = [s for s in matched if not s.evidence_problem()]
        supports = [s for s in accessible if s.stance == "supports"]
        contradicts = [s for s in accessible if s.stance == "contradicts"]
        mentions = [s for s in accessible if s.stance == "mentions"]
        inaccessible = [{**s.citation(), "reason": s.evidence_problem()}
                        for s in matched if s.evidence_problem()]
        supporting = [s.citation() for s in supports]
        conflicting = [s.citation() for s in contradicts]
        mention_refs = [s.citation() for s in mentions]

        reasons: List[str] = []
        if claim.is_expired(now):
            state = ClaimState.STALE.value
            confidence = 0.3
            reasons.append(
                f"适用时间已过：valid_until={claim.valid_until} 早于当前 {now}")
        elif claim.valid_from is not None and now < claim.valid_from:
            state = ClaimState.UNKNOWN.value
            confidence = 0.0
            reasons.append("结论适用时间尚未开始")
        elif contradicts:
            state = ClaimState.CONTRADICTED.value
            confidence = 0.4
            reasons.append(
                f"来源冲突：{len(supports)} 条支持、{len(contradicts)} 条反对")
            if not supports:
                reasons.append("仅存在反对来源，无来源支持该结论")
        elif supports:
            state = ClaimState.SUPPORTED.value
            publishers = _publishers(supporting)
            confidence = 0.9 if len(publishers) >= 2 else 0.7
            reasons.append(
                f"可访问来源直接支持（{len(supports)} 条，"
                f"{len(publishers)} 个发布者）")
        elif mentions:
            state = ClaimState.UNKNOWN.value
            confidence = 0.0
            reasons.append("来源仅提及该主题，未直接支持结论，不判为可信")
        elif claim.derived_from_internal:
            state = ClaimState.INFERRED.value
            confidence = 0.5
            reasons.append("由内部已核验事实推导，非外部直接支持")
        else:
            state = ClaimState.UNKNOWN.value
            confidence = 0.0
            reasons.append("未提供外部来源：模型自述不得判为可信")

        if inaccessible:
            reasons.append(f"{len(inaccessible)} 个来源不可访问或记录无效，未计入证据")
        model_only = bool(claim.model_generated and not supports)

        return Verdict(
            claim_id=claim.claim_id, statement=claim.statement, state=state,
            confidence=confidence, supporting=supporting,
            conflicting=conflicting, mentions=mention_refs,
            inaccessible=inaccessible,
            applicable_window=claim.applicable_window(), reasons=reasons,
            model_only=model_only, policy=self.POLICY)
