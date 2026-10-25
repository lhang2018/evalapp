"""可信交付报告（ALN-28-A）。

把核验结果组织成用户可核验的交付物：
- `delivered`：只有 `supported`（有可访问引用）的结论；
- `conflicts`：来源冲突逐条可见（含双方引用与原因）；
- `unverified`：未知 / 推断 / 过期结论逐条给出原因；
- `citations`：汇总所有引用（URL、发布者、获取时间、正文摘要、片段），
  夹具引用带 `fixture=True` 明确标注；
- 规则项写死在报告里，便于审阅：引用必须可访问、模型自述不得单独可信、
  冲突必须可见、结论必须带适用时间窗。
"""

import time
from typing import Any, Dict, List, Optional

from .claims import Claim
from .crosscheck import FactCrossChecker, Verdict
from .sources import SourceRecord


class DeliveryError(ValueError):
    """交付物违反可信规则（例如 supported 结论没有可访问引用）。"""


class TrustedDelivery:
    def __init__(self, *, checker: Optional[FactCrossChecker] = None,
                 clock=None):
        self.checker = checker or FactCrossChecker(clock=clock)
        self.clock = clock or time.time

    def build(self, *, topic: str, claims: List[Claim],
              sources: List[SourceRecord],
              now: Optional[float] = None) -> Dict[str, Any]:
        now = self.clock() if now is None else now
        verdicts = [self.checker.verify(claim, sources, now=now)
                    for claim in claims]
        delivered: List[Dict[str, Any]] = []
        conflicts: List[Dict[str, Any]] = []
        unverified: List[Dict[str, Any]] = []
        for verdict in verdicts:
            if verdict.state == "supported":
                if not verdict.supporting:
                    raise DeliveryError(
                        "supported 结论缺少可访问引用: " + verdict.claim_id)
                delivered.append(verdict.to_dict())
            elif verdict.state == "contradicted":
                conflicts.append(verdict.to_dict())
            else:
                unverified.append(verdict.to_dict())
        citations = self._citations(delivered + conflicts + unverified)
        fixture_refs = [c for c in citations if c.get("fixture")]
        return {
            "topic": topic,
            "generated_at": now,
            "delivered": delivered,
            "conflicts": conflicts,
            "unverified": unverified,
            "citations": citations,
            "counts": {"claims": len(claims), "delivered": len(delivered),
                       "conflicts": len(conflicts),
                       "unverified": len(unverified),
                       "fixture_citations": len(fixture_refs)},
            "policy": FactCrossChecker.POLICY,
            "rules": {
                "citation_accessible_required": True,
                "model_only_trust": False,
                "conflicts_visible": True,
                "applicable_window_required": True,
                "fixture_citations_labelled": True,
            },
        }

    @staticmethod
    def _citations(verdicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        rows: List[Dict[str, Any]] = []
        for verdict in verdicts:
            for group in ("supporting", "conflicting", "mentions"):
                for citation in verdict.get(group) or []:
                    key = (citation.get("url"), citation.get("content_digest"))
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(citation)
        return rows

    @staticmethod
    def assert_deliverable(verdict: Verdict) -> None:
        """审阅防线：不支持「无引用却记 supported」的结论。"""
        if verdict.state == "supported" and not verdict.supporting:
            raise DeliveryError(
                "拒绝把无引用结论列为 supported: " + verdict.claim_id)
        if verdict.state == "supported" and verdict.model_only:
            raise DeliveryError(
                "模型自述不得单独判为可信: " + verdict.claim_id)
