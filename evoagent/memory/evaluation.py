"""记忆质量评测（PRD 28 / Architecture 8.2）。

覆盖：写入准确率、召回率、检索精度、过期记忆误用率、冲突处理正确率，
以及运行反馈中的记忆收益与记忆导致错误率。评测在独立临时库中执行，
不读取生产记忆，避免调优数据与线上数据互相污染。

缺样本的维度标记 insufficient_evidence，不把「没有数据」当成通过。
"""

import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .store import Credibility, MemoryItem, MemoryKind, MemoryStore, Scope


@dataclass
class MemoryEvalCase:
    case_id: str
    query: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    scope: Optional[str] = None
    scope_key: str = ""
    domains: List[str] = field(default_factory=list)
    limit: int = 5
    expected: List[str] = field(default_factory=list)
    expect_absent: List[str] = field(default_factory=list)
    conflict: bool = False


class MemoryEvaluator:
    """在临时 MemoryStore 上重放用例；不接触生产库。"""

    def evaluate(self, cases: Optional[List[MemoryEvalCase]] = None, *,
                 dataset_id: str = "builtin_memory_eval",
                 version: str = "1.0.0") -> Dict[str, Any]:
        cases = list(cases or builtin_memory_cases())
        results: List[Dict[str, Any]] = []
        write_rows: List[Dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="evoagent-memeval-") as td:
            for index, case in enumerate(cases):
                store = MemoryStore(os.path.join(td, f"case_{index}.db"))
                try:
                    write_rows.extend(self._seed(store, case.items))
                    results.append(self._run_case(store, case))
                finally:
                    # Windows 需先关闭连接才能清理临时库
                    store.close()
            write_accuracy = self._write_accuracy(write_rows)
        return _aggregate(dataset_id, version, results, write_rows,
                          write_accuracy)

    # ---- 写入 ----
    @staticmethod
    def _seed(store: MemoryStore,
              items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seeded: List[Dict[str, Any]] = []
        for raw in items:
            item = _build_item(raw)
            store.write(item)
            stored = store.get(item.memory_id)
            seeded.append({
                "memory_id": item.memory_id,
                "expected_status": item.status,
                "stored_status": stored.status if stored else None,
                "content_ok": bool(stored and stored.content == item.content),
            })
        return seeded

    @staticmethod
    def _write_accuracy(rows: List[Dict[str, Any]]) -> float:
        if not rows:
            return 1.0
        accurate = sum(1 for r in rows if r["content_ok"]
                       and r["stored_status"] == r["expected_status"])
        return round(accurate / len(rows), 4)

    # ---- 检索 ----
    def _run_case(self, store: MemoryStore,
                  case: MemoryEvalCase) -> Dict[str, Any]:
        scope = Scope(case.scope) if case.scope else None
        retrieved = store.search(
            case.query, scope=scope, scope_key=case.scope_key,
            domains=case.domains or None, limit=case.limit)
        ids = [m.memory_id for m in retrieved]
        expected = list(case.expected)
        absent = list(case.expect_absent)

        hits = [mid for mid in expected if mid in ids]
        recall = len(hits) / len(expected) if expected else None
        precision = (len(hits) / len(ids)) if ids else (0.0 if expected else None)
        mrr = 0.0
        for rank, mid in enumerate(ids, start=1):
            if mid in expected:
                mrr = 1.0 / rank
                break
        misuse = [mid for mid in absent if mid in ids]
        conflict_ok = True
        if case.conflict and expected:
            conflict_ok = expected[0] in ids and not misuse
        return {
            "case_id": case.case_id,
            "expected": expected,
            "retrieved": ids,
            "recall": recall,
            "precision": precision,
            "mrr": mrr,
            "expired_misuse": bool(misuse),
            "misused": misuse,
            "conflict": case.conflict,
            "conflict_correct": conflict_ok,
        }

    # ---- 运行反馈（PRD 28：记忆收益 / 记忆导致错误率）----
    def runtime_feedback(self, tasks: Iterable[Any], *,
                         feedback: Optional[List[Dict[str, Any]]] = None,
                         min_samples: int = 3) -> Dict[str, Any]:
        """基于真实任务上下文使用记录估计收益；样本不足时明确标注。"""
        with_memory: List[bool] = []
        without_memory: List[bool] = []
        for task in tasks:
            status = getattr(getattr(task, "task_status", None), "value", "")
            if status not in ("SUCCEEDED", "FAILED", "CANCELLED"):
                continue
            ctx = (getattr(task, "runtime_manifest", None) or {}).get("context") or {}
            ok = status == "SUCCEEDED"
            if int(ctx.get("memory_used") or 0) > 0:
                with_memory.append(ok)
            else:
                without_memory.append(ok)
        benefit: Dict[str, Any]
        if len(with_memory) >= min_samples and len(without_memory) >= min_samples:
            rate_with = sum(with_memory) / len(with_memory)
            rate_without = sum(without_memory) / len(without_memory)
            benefit = {
                "status": "measured",
                "value": round(rate_with - rate_without, 4),
                "success_rate_with_memory": round(rate_with, 4),
                "success_rate_without_memory": round(rate_without, 4),
                "samples_with": len(with_memory),
                "samples_without": len(without_memory),
            }
        else:
            benefit = {
                "status": "insufficient_evidence",
                "value": None,
                "samples_with": len(with_memory),
                "samples_without": len(without_memory),
                "min_samples": min_samples,
            }
        feedback = list(feedback or [])
        harmful = sum(1 for f in feedback
                      if str(f.get("outcome")) in ("harmful", "misleading"))
        uses = sum(1 for f in feedback if f.get("outcome") is not None)
        error_rate: Dict[str, Any]
        if uses:
            error_rate = {"status": "measured",
                          "value": round(harmful / uses, 4),
                          "harmful": harmful, "uses": uses}
        else:
            error_rate = {"status": "insufficient_evidence", "value": None,
                          "harmful": 0, "uses": 0}
        return {"memory_benefit": benefit,
                "memory_caused_error_rate": error_rate}


def _aggregate(dataset_id: str, version: str, results: List[Dict[str, Any]],
               seeded: List[Dict[str, Any]],
               write_accuracy: float) -> Dict[str, Any]:
    recalls = [r["recall"] for r in results if r["recall"] is not None]
    precisions = [r["precision"] for r in results
                  if r["precision"] is not None]
    mrrs = [r["mrr"] for r in results if r["recall"] is not None]
    conflicts = [r for r in results if r["conflict"]]
    return {
        "dataset_id": dataset_id,
        "version": version,
        "generated_at": time.time(),
        "case_count": len(results),
        "item_count": len(seeded),
        "write_accuracy": write_accuracy,
        "recall": round(sum(recalls) / len(recalls), 4) if recalls else None,
        "precision": round(sum(precisions) / len(precisions), 4)
        if precisions else None,
        "mrr": round(sum(mrrs) / len(mrrs), 4) if mrrs else None,
        "expired_misuse_rate": round(
            sum(1 for r in results if r["expired_misuse"])
            / len(results), 4) if results else None,
        "conflict_correctness": round(
            sum(1 for r in conflicts if r["conflict_correct"])
            / len(conflicts), 4) if conflicts else None,
        "cases": results,
        "thresholds": {
            "min_recall": 0.8,
            "min_precision": 0.6,
            "max_expired_misuse_rate": 0.0,
            "min_conflict_correctness": 1.0,
            "min_write_accuracy": 0.95,
        },
    }


def _build_item(raw: Dict[str, Any]) -> MemoryItem:
    return MemoryItem(
        content=raw["content"],
        kind=MemoryKind(raw.get("kind", "runtime_experience")),
        scope=Scope(raw.get("scope", "task")),
        scope_key=raw.get("scope_key", ""),
        status=Credibility(raw.get("status", "candidate")),
        source=raw.get("source", "memory_evaluation"),
        applicable_domains=list(raw.get("applicable_domains") or []),
        evidence=dict(raw.get("evidence") or {}),
        memory_id=raw["memory_id"],
        valid_until=raw.get("valid_until"),
    )


def builtin_memory_cases() -> List[MemoryEvalCase]:
    """内置确定性用例：覆盖召回、领域过滤、过期、冲突替代与范围隔离。"""
    return [
        MemoryEvalCase(
            case_id="recall_preference",
            query="界面主题 深色",
            items=[{
                "memory_id": "mem_pref_dark",
                "content": "用户偏好深色界面主题",
                "kind": "user_preference", "scope": "shared",
                "status": "verified", "evidence": {"source": "user"},
            }],
            expected=["mem_pref_dark"],
        ),
        MemoryEvalCase(
            case_id="domain_knowledge",
            query="计算器 工具",
            items=[{
                "memory_id": "mem_calc_tool",
                "content": "计算任务允许使用计算器工具",
                "kind": "domain_knowledge", "scope": "expert",
                "scope_key": "computation",
                "applicable_domains": ["computation"],
                "status": "verified", "evidence": {"task_id": "t-eval"},
            }],
            domains=["computation"],
            expected=["mem_calc_tool"],
        ),
        MemoryEvalCase(
            case_id="expired_not_used",
            query="部署脚本 旧版",
            items=[{
                "memory_id": "mem_old_deploy",
                "content": "当前项目使用旧版部署脚本",
                "kind": "project_state", "scope": "project",
                "status": "verified", "evidence": {"task_id": "t-old"},
                "valid_until": time.time() - 3600,
            }],
            expect_absent=["mem_old_deploy"],
        ),
        MemoryEvalCase(
            case_id="conflict_superseded",
            query="默认导出格式",
            items=[
                {"memory_id": "mem_export_pdf",
                 "content": "默认导出格式是 PDF",
                 "kind": "project_state", "scope": "project",
                 "status": "revoked", "evidence": {"task_id": "t-1"}},
                {"memory_id": "mem_export_csv",
                 "content": "默认导出格式是 CSV",
                 "kind": "project_state", "scope": "project",
                 "status": "verified", "evidence": {"task_id": "t-2"}},
            ],
            expected=["mem_export_csv"],
            expect_absent=["mem_export_pdf"],
            conflict=True,
        ),
        MemoryEvalCase(
            case_id="scope_isolation",
            query="临时任务 上下文",
            items=[{
                "memory_id": "mem_other_task",
                "content": "临时任务上下文只属于另一个任务",
                "kind": "project_state", "scope": "task",
                "scope_key": "task-other",
                "status": "verified", "evidence": {"task_id": "task-other"},
            }],
            scope="task", scope_key="task-current",
            expect_absent=["mem_other_task"],
        ),
        MemoryEvalCase(
            case_id="skill_recall",
            query="发送周报 运行测试",
            items=[{
                "memory_id": "mem_weekly_skill",
                "content": "发送周报前先运行测试",
                "kind": "skill_workflow", "scope": "shared",
                "status": "verified", "evidence": {"task_id": "t-skill"},
            }],
            expected=["mem_weekly_skill"],
        ),
    ]
