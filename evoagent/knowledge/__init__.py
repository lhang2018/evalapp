"""故障经验知识库与跨专家迁移验证（ALN-27-A）。

导出：
- ``KnowledgeEntry`` / ``KnowledgeStore``：带来源/可信度/适用域/版本与撤销的知识条目。
- ``plan_injection``：注入前适用性门禁（不适用条目不注入）。
- ``evaluate_transfer``：跨专家前后对照的迁移验证（任务质量，非命中率）。
"""
from .store import KnowledgeEntry, KnowledgeError, KnowledgeStore
from .applicability import (
    InjectionPlan,
    SkippedEntry,
    inapplicability_reason,
    plan_injection,
)
from .transfer import ExpertDelta, TaskOutcome, TransferReport, evaluate_transfer

__all__ = [
    "KnowledgeEntry",
    "KnowledgeError",
    "KnowledgeStore",
    "InjectionPlan",
    "SkippedEntry",
    "inapplicability_reason",
    "plan_injection",
    "ExpertDelta",
    "TaskOutcome",
    "TransferReport",
    "evaluate_transfer",
]
