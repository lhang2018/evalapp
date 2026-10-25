"""代表性任务集（ALN-08-P：生产评测集）。

补的是「评测集从哪来、是否脱敏、覆盖了什么」三件事：

- **来源**：每个任务集必须声明 `source_ref`（脱敏真实任务样本的来源批次/工单）
  与采集时间，不接受无名数据集；
- **脱敏**：必须显式声明 ``anonymized=True`` 与脱敏方法，且样本正文里出现
  口令/令牌/证件等敏感标记时直接拒绝入库（防止把生产敏感内容当评测集）；
- **覆盖**：``coverage()`` 给出领域/意图/场景分布与缺口，供路由与成本评测
  说明「实际覆盖范围」，而不是只报一个总数。

任务集以整体 JSON 快照持久化（``task_sets`` 表，见 core/schema/cost_catalog.py）。
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

SENSITIVE = re.compile(
    r"(pass(word|wd)?|secret|token|api[-_]?key|cookie|authorization|bearer|"
    r"身份证|证件号|银行卡|信用卡|手机号)", re.IGNORECASE)
DEFAULT_DOMAINS = ("general", "travel", "finance", "coding", "device")


class TaskSetError(ValueError):
    """任务集非法（缺来源/未脱敏/含敏感内容/重复用例）。"""


@dataclass(frozen=True)
class TaskSample:
    case_id: str
    goal: str
    intent: str
    domain: str
    scenario: str = ""
    tags: Tuple[str, ...] = ()
    redactions: Tuple[str, ...] = ()

    def validate(self) -> "TaskSample":
        for name, value in (("case_id", self.case_id), ("goal", self.goal),
                            ("intent", self.intent), ("domain", self.domain)):
            if not isinstance(value, str) or not value.strip():
                raise TaskSetError(name + " 不得为空")
        if SENSITIVE.search(self.goal) or SENSITIVE.search(self.scenario):
            raise TaskSetError("样本含敏感标记，必须先脱敏: " + self.case_id)
        for tag in self.tags:
            if not isinstance(tag, str) or not tag.strip():
                raise TaskSetError("tags 必须是非空字符串")
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {"case_id": self.case_id, "goal": self.goal,
                "intent": self.intent, "domain": self.domain,
                "scenario": self.scenario, "tags": list(self.tags),
                "redactions": list(self.redactions)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskSample":
        return cls(case_id=str(data["case_id"]), goal=str(data["goal"]),
                   intent=str(data["intent"]), domain=str(data["domain"]),
                   scenario=str(data.get("scenario") or ""),
                   tags=tuple(data.get("tags") or ()),
                   redactions=tuple(data.get("redactions") or ()))


@dataclass
class RepresentativeTaskSet:
    set_id: str
    version: str
    source_ref: str
    collected_at: float
    anonymized: bool
    anonymization_method: str
    samples: List[TaskSample] = field(default_factory=list)
    notes: str = ""

    def validate(self) -> "RepresentativeTaskSet":
        for name, value in (("set_id", self.set_id), ("version", self.version)):
            if not isinstance(value, str) or not value.strip():
                raise TaskSetError(name + " 不得为空")
        if not isinstance(self.source_ref, str) or not self.source_ref.strip():
            raise TaskSetError("任务集必须有来源（脱敏批次/工单编号）")
        if self.anonymized is not True:
            raise TaskSetError("任务集必须显式声明已脱敏")
        if not isinstance(self.anonymization_method, str) or not self.anonymization_method.strip():
            raise TaskSetError("必须声明脱敏方法")
        if not self.samples:
            raise TaskSetError("任务集不能为空")
        identities = set()
        for sample in self.samples:
            sample.validate()
            if sample.case_id in identities:
                raise TaskSetError("重复用例: " + sample.case_id)
            identities.add(sample.case_id)
        return self

    def coverage(self, *, domains: Iterable[str] = DEFAULT_DOMAINS) -> Dict[str, Any]:
        by_domain: Dict[str, int] = {}
        by_intent: Dict[str, int] = {}
        by_scenario: Dict[str, int] = {}
        for sample in self.samples:
            by_domain[sample.domain] = by_domain.get(sample.domain, 0) + 1
            by_intent[sample.intent] = by_intent.get(sample.intent, 0) + 1
            scenario = sample.scenario or "(none)"
            by_scenario[scenario] = by_scenario.get(scenario, 0) + 1
        expected = list(dict.fromkeys(domains))
        return {"total": len(self.samples),
                "by_domain": dict(sorted(by_domain.items())),
                "by_intent": dict(sorted(by_intent.items())),
                "by_scenario": dict(sorted(by_scenario.items())),
                "missing_domains": [name for name in expected
                                    if name not in by_domain],
                "scope": "representative_samples"}

    # ---- 持久快照 ----
    def to_dict(self) -> Dict[str, Any]:
        return {"set_id": self.set_id, "version": self.version,
                "source_ref": self.source_ref,
                "collected_at": float(self.collected_at),
                "anonymized": bool(self.anonymized),
                "anonymization_method": self.anonymization_method,
                "notes": self.notes,
                "samples": [sample.to_dict() for sample in self.samples]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RepresentativeTaskSet":
        return cls(set_id=str(data["set_id"]), version=str(data["version"]),
                   source_ref=str(data["source_ref"]),
                   collected_at=float(data.get("collected_at") or 0.0),
                   anonymized=bool(data.get("anonymized")),
                   anonymization_method=str(data.get("anonymization_method") or ""),
                   samples=[TaskSample.from_dict(item)
                            for item in data.get("samples") or []],
                   notes=str(data.get("notes") or "")).validate()

    def save(self, db: Any) -> None:
        self.validate()
        with db._lock:
            db._conn.execute(
                "INSERT OR REPLACE INTO task_sets VALUES (?,?,?)",
                (self.set_id, self.version,
                 json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)))
            db._conn.commit()

    @classmethod
    def load(cls, db: Any, set_id: str, version: str) -> "RepresentativeTaskSet":
        row = db._conn.execute(
            "SELECT data FROM task_sets WHERE set_id=? AND version=?",
            (set_id, version)).fetchone()
        if row is None:
            raise TaskSetError(f"任务集不存在: {set_id}@{version}")
        return cls.from_dict(json.loads(row["data"]))


def build_sample_set(*, set_id: str, version: str, source_ref: str,
                     samples: List[TaskSample], collected_at: Optional[float] = None,
                     anonymization_method: str = "rules:mask_pii+drop_ids",
                     notes: str = "") -> RepresentativeTaskSet:
    """便捷构造并校验（默认声明规则脱敏；不做任何真实脱敏处理）。"""
    task_set = RepresentativeTaskSet(
        set_id=set_id, version=version, source_ref=source_ref,
        collected_at=time.time() if collected_at is None else float(collected_at),
        anonymized=True, anonymization_method=anonymization_method,
        samples=list(samples), notes=notes)
    return task_set.validate()
