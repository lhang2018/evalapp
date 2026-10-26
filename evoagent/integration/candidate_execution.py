"""受控候选评测入口与问题队列管理面（ALN-09-I）。

把已接受的 `IsolatedCandidateHost`（09-H）与 `HostBenchmark`（10-E）接到
**受控候选评测入口**，并补齐 `ProblemQueue`（09-H，已被进化控制器消费）的
查询/管理面；不重建队列、不绕过候选评测与发布门禁：

- 只消费通过**内容摘要与版本**校验的真实候选制品（host.resolve 双重校验，
  篡改/未注册/非代码资产即拒绝，不做假成功）；
- 结果绑定 baseline/candidate 身份（asset/version/content_digest/entry）、
  数据集引用与实际宿主参数（benchmark 版本/超时/重复/来源）；
- 评测成功**不自动发布**：返回 release_before/release_after 相同，发布仍由
  既有 release 门禁决定；
- 队列管理仅做统计/待办列表/过期租约恢复（复用 ProblemQueue.recover_stale，
  不覆盖未过期租约、不重复提案）。
"""

import time
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Optional

from ..evaluation.host_bench import BenchCase, BenchError, HostBenchmark
from ..evolution.host import CandidateAssetError, IsolatedCandidateHost


class CandidateExecutionError(ValueError):
    """候选评测请求非法或不可执行。"""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateExecutionError(label + " 不得为空")
    return value.strip()


def _cases(value: Any) -> List[BenchCase]:
    if not isinstance(value, list) or not value:
        raise CandidateExecutionError("cases 至少需要一个用例")
    out: List[BenchCase] = []
    for index, item in enumerate(value):
        if isinstance(item, BenchCase):
            out.append(item)
            continue
        if not isinstance(item, dict):
            raise CandidateExecutionError(f"cases[{index}] 必须是对象")
        case_id = _text(item.get("case_id"), f"cases[{index}].case_id")
        payload = item.get("input") if item.get("input") is not None else {}
        if not isinstance(payload, dict):
            raise CandidateExecutionError(f"cases[{index}].input 必须是对象")
        out.append(BenchCase(case_id=case_id, input=payload,
                             domain=str(item.get("domain") or "general"),
                             required=bool(item.get("required", True))))
    return out


class CandidateEvaluationService:
    def __init__(self, repository: Any, *, queue: Any = None,
                 releases: Any = None, timeout: float = 5.0,
                 repeat: int = 2, source: str = "candidate-host/09-I",
                 clock=None):
        self.repository = repository
        self.queue = queue
        self.releases = releases
        self.host = IsolatedCandidateHost(repository)
        self.benchmark = HostBenchmark(self.host, timeout=timeout,
                                       repeat=repeat, source=source)
        self.clock = clock or time.time

    def evaluate(self, *, candidate_id: str, baseline_ref: Any,
                 candidate_ref: Any, cases: Any, dataset_ref: str = "",
                 actor: str, reason: str) -> Dict[str, Any]:
        candidate_id = _text(candidate_id, "candidate_id")
        actor = _text(actor, "actor")
        reason = _text(reason, "reason")
        bench_cases = _cases(cases)
        # 双重校验：摘要/版本/可执行代码形态（篡改或未注册即拒绝）；
        # 资产/基准错误原样上抛，由 API 映射为 409（请求合法但候选不可执行）。
        baseline = self.host.resolve(baseline_ref)
        candidate = self.host.resolve(candidate_ref)
        release_before = getattr(self.releases, "current_release_id", None) \
            if self.releases is not None else None
        report = self.benchmark.compare(baseline_ref, candidate_ref,
                                        bench_cases)
        release_after = getattr(self.releases, "current_release_id", None) \
            if self.releases is not None else None
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "baseline": baseline.identity(),
            "candidate": candidate.identity(),
            "dataset_ref": dataset_ref,
            "host": {"benchmark_version": report["benchmark_version"],
                     "timeout_s": report["timeout_s"],
                     "repeat": report["repeat"],
                     "source": report["source"]},
            "verdict": report["verdict"],
            "evidence_digest": report["evidence_digest"],
            "release_decision": report["release_decision"],
            "report": report,
            "auto_published": False,
            "release_before": release_before,
            "release_after": release_after,
            "actor": actor,
            "reason": reason,
            "evaluated_at": self.clock(),
            "note": ("评测成功不等于发布：结果只作为既有 release 门禁输入；"
                     "不自动部署、不改当前发布版本"),
        }

    # ---- 队列管理（复用 09-H ProblemQueue，不重建）----
    def queue_overview(self, *, limit: int = 50) -> Dict[str, Any]:
        if self.queue is None:
            return {"available": False, "stats": {}, "pending": []}
        stats = self.queue.stats()
        rows = []
        for row in self.queue.pending(limit=max(1, min(int(limit), 200))):
            ticket = row.get("ticket")
            if is_dataclass(ticket):
                ticket = asdict(ticket)
            rows.append({"ticket_id": row.get("ticket_id"),
                         "status": row.get("status"),
                         "detected_at": row.get("detected_at"),
                         "attempts": row.get("attempts"),
                         "lease_owner": row.get("lease_owner"),
                         "lease_expires_at": row.get("lease_expires_at"),
                         "ticket": ticket})
        return {"available": True, "stats": stats, "pending": rows,
                "note": "只读视图；领取/完成仍由进化控制器消费"}

    def queue_recover(self, *, actor: str, reason: str) -> Dict[str, Any]:
        if self.queue is None:
            return {"available": False, "recovered": 0}
        _text(actor, "actor")
        _text(reason, "reason")
        recovered = self.queue.recover_stale()
        return {"available": True, "recovered": int(recovered),
                "stats": self.queue.stats(), "actor": actor,
                "reason": reason,
                "note": "只恢复过期租约；未过期租约与已消费票据不动"}
