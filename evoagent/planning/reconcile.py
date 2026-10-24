"""ALN-07-E：计划 ↔ 副作用账本 纯读对账。

回答 07-T 答不了的问题：**计划的每一步实际有没有按声明产生副作用，
以及有没有产生计划之外的副作用。**

设计要点
--------
* **结构性纯读**：主入口 :meth:`SideEffectReconciler.reconcile` 只接受内存里的
  记录列表，不持有 ``SideEffectLedger`` 引用，因此在本模块内**不可能**写入账本
  （不调用 begin/finish/transition/reconcile/record 中的任何一个）。
* **fail-closed**：``planned`` / ``submitted`` / ``unknown`` 状态的记录一律归
  ``uncertain``，绝不计入 ``matched``——"没确认"不等于"没发生"。
* **确定性**：所有输出列表按天然键排序，报告的判定与传入记录/步骤次序无关。
* **不猜**：只按 ``tool_id`` 字面候选；同工具多步骤必须由宿主显式绑定，
  否则记为归因歧义。不会猜工具别名、参数、版本或步骤身份。

本模块是 ALN-07 整包的第四项，纯新文件，不修改任何既有共享模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 状态常量

#: 唯一可判定为「已确认发生」的账本状态
CONFIRMED_STATUS = "confirmed"

#: 副作用结果未决的状态——不能算完成，也不能算没发生（fail-closed）
UNSETTLED_STATUSES = frozenset({"planned", "submitted", "unknown"})

#: 计划步骤被视为「应当已产生副作用」的状态
DONE_STATES = frozenset({"done"})

#: 计划步骤明确不会有副作用的状态（不参与判定）
SKIPPED_STATES = frozenset({"skipped"})
PLAN_STATES = frozenset({"pending", "running", "done", "failed", "skipped"})
RECORD_STATUSES = UNSETTLED_STATUSES | {"confirmed", "failed", "compensated"}


class ReconciliationError(ValueError):
    """对账输入不合法（记录缺字段、计划不可读等）。"""


def _text(value, name, *, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ReconciliationError(f"{name}必须是{'非空' if required else ''}字符串")
    return value


def _items(data, name):
    values = data.get(name, [])
    if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
        raise ReconciliationError(f"{name}必须是对象列表，不能丢弃损坏记录")
    return values


class _SnapshotList(tuple):
    """Immutable sequence preserving legacy comparisons with JSON lists."""
    def __eq__(self, other):
        return tuple(self) == tuple(other) if isinstance(other, (list, tuple)) else False

    def __ne__(self, other):
        return not self == other

    __hash__ = tuple.__hash__


# ---------------------------------------------------------------- 数据契约

@dataclass(frozen=True)
class DeclaredEffect:
    """计划步骤声明的预期副作用（源自 ``PlanStep.tool_hint``）。"""

    step_id: str
    tool_id: str
    step_state: str = "pending"
    description: str = ""

    def __post_init__(self):
        _text(self.step_id, "step_id", required=True)
        _text(self.tool_id, "tool_id", required=True)
        _text(self.step_state, "step_state", required=True)
        if self.step_state not in PLAN_STATES:
            raise ReconciliationError("未知计划步骤状态")

    def sort_key(self) -> Tuple[str, str]:
        return (self.step_id, self.tool_id)

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "tool_id": self.tool_id,
                "step_state": self.step_state,
                "description": self.description}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DeclaredEffect":
        return cls(step_id=_text(d.get("step_id"), "step_id", required=True),
                   tool_id=_text(d.get("tool_id"), "tool_id", required=True),
                   step_state=_text(d.get("step_state", "pending"), "step_state", required=True),
                   description=_text(d.get("description"), "description"))


@dataclass(frozen=True)
class EffectRecord:
    """副作用账本记录的只读投影。

    对应 ``task_side_effects`` 的字段；task/effect/tool/status为必填，
    非法状态或重复effect_id拒绝，不能拿别的任务或重复记录抵扣声明。
    """

    effect_id: str
    tool_id: str
    status: str
    idempotency_key: str = ""
    effect_summary: str = ""
    run_id: str = ""
    task_id: str = ""

    def __post_init__(self):
        _text(self.effect_id, "effect_id", required=True)
        _text(self.tool_id, "tool_id", required=True)
        _text(self.status, "status", required=True)
        if self.status not in RECORD_STATUSES:
            raise ReconciliationError("未知副作用状态")
        _text(self.task_id, "task_id")

    @property
    def settled(self) -> bool:
        """副作用结果是否已定论（非未决状态）。"""
        return self.status not in UNSETTLED_STATUSES

    def sort_key(self) -> Tuple[str, str]:
        return (self.tool_id, self.effect_id)

    def to_dict(self) -> Dict[str, Any]:
        return {"effect_id": self.effect_id, "tool_id": self.tool_id,
                "status": self.status,
                "idempotency_key": self.idempotency_key,
                "effect_summary": self.effect_summary,
                "run_id": self.run_id, "task_id": self.task_id}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EffectRecord":
        return cls(effect_id=_text(d.get("effect_id"), "effect_id", required=True),
                   tool_id=_text(d.get("tool_id"), "tool_id", required=True),
                   status=_text(d.get("status"), "status", required=True),
                   idempotency_key=_text(d.get("idempotency_key"), "idempotency_key"),
                   effect_summary=_text(d.get("effect_summary"), "effect_summary"),
                   run_id=_text(d.get("run_id"), "run_id"),
                   task_id=_text(d.get("task_id"), "task_id", required=True))


@dataclass(frozen=True)
class MatchedEffect:
    """计划声明与账本记录的成功归因。"""

    step_id: str
    tool_id: str
    effect_id: str

    def __post_init__(self):
        for name in ("step_id", "tool_id", "effect_id"):
            _text(getattr(self, name), name, required=True)

    def sort_key(self) -> Tuple[str, str, str]:
        return (self.step_id, self.tool_id, self.effect_id)

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "tool_id": self.tool_id,
                "effect_id": self.effect_id}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MatchedEffect":
        return cls(step_id=_text(d.get("step_id"), "step_id", required=True),
                   tool_id=_text(d.get("tool_id"), "tool_id", required=True),
                   effect_id=_text(d.get("effect_id"), "effect_id", required=True))


@dataclass(frozen=True)
class UndeclaredEffect:
    """账本里有执行、计划里没声明该项（越权或计划失真）。"""

    record: EffectRecord
    reason: str = "no_matching_declaration"

    def sort_key(self) -> Tuple[str, str]:
        return self.record.sort_key()

    def to_dict(self) -> Dict[str, Any]:
        return {"record": self.record.to_dict(), "reason": self.reason}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UndeclaredEffect":
        return cls(record=EffectRecord.from_dict(d.get("record") or {}),
                   reason=str(d.get("reason") or "no_matching_declaration"))


@dataclass(frozen=True)
class ReconciliationReport:
    """计划与副作用账本的对账结论（只读视图）。"""

    task_id: str = ""
    plan_id: str = ""
    declared: List[DeclaredEffect] = field(default_factory=list)
    matched: List[MatchedEffect] = field(default_factory=list)
    undeclared: List[UndeclaredEffect] = field(default_factory=list)
    unexecuted: List[DeclaredEffect] = field(default_factory=list)
    uncertain: List[EffectRecord] = field(default_factory=list)
    pending_declarations: List[DeclaredEffect] = field(default_factory=list)
    ambiguous: List[EffectRecord] = field(default_factory=list)
    resolved: List[EffectRecord] = field(default_factory=list)
    ok: bool = field(default=False, init=False)

    def __post_init__(self):
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(self, "plan_id", _text(self.plan_id, "plan_id"))
        types = {"declared": DeclaredEffect, "matched": MatchedEffect,
                 "undeclared": UndeclaredEffect, "unexecuted": DeclaredEffect,
                 "uncertain": EffectRecord, "pending_declarations": DeclaredEffect,
                 "ambiguous": EffectRecord, "resolved": EffectRecord}
        for name, item_type in types.items():
            values = getattr(self, name)
            if not isinstance(values, (list, tuple)) or any(not isinstance(v, item_type) for v in values):
                raise ReconciliationError(f"{name}包含损坏对象")
            object.__setattr__(self, name, _SnapshotList(values))
        declared = {d.step_id: d for d in self.declared}
        if len(declared) != len(self.declared):
            raise ReconciliationError("计划声明step_id重复")
        covered = []
        for item in self.matched:
            if item.step_id not in declared or item.tool_id != declared[item.step_id].tool_id:
                raise ReconciliationError("归因不对应计划声明")
            covered.append(item.step_id)
        for item in (*self.unexecuted, *self.pending_declarations):
            if declared.get(item.step_id) != item:
                raise ReconciliationError("未完成声明与原计划不一致")
            covered.append(item.step_id)
        if set(covered) != set(declared) or len(covered) != len(declared):
            raise ReconciliationError("计划声明被遗漏或重复归因")
        records = [u.record for u in self.undeclared] + list(self.uncertain) + list(self.ambiguous) + list(self.resolved)
        ids = [m.effect_id for m in self.matched] + [r.effect_id for r in records]
        if len(ids) != len(set(ids)):
            raise ReconciliationError("报告副作用记录重复")
        if ids and not self.task_id:
            raise ReconciliationError("副作用报告必须包含任务范围")
        if any(r.task_id != self.task_id for r in records):
            raise ReconciliationError("报告包含其他任务记录")
        if (any(r.status not in UNSETTLED_STATUSES for r in self.uncertain)
                or any(r.status != CONFIRMED_STATUS for r in self.ambiguous)
                or any(r.status not in {"failed", "compensated"} for r in self.resolved)
                or any(u.record.status != CONFIRMED_STATUS for u in self.undeclared)
                or any(d.step_state != "done" for d in self.unexecuted)
                or any(d.step_state in {"done", "skipped"} for d in self.pending_declarations)):
            raise ReconciliationError("对账分类与状态不一致")
        object.__setattr__(self, "ok", self.divergence_free)

    # -- 判定 ----------------------------------------------------------

    @property
    def divergence_free(self) -> bool:
        """与ok同义：无未声明/缺失/未决/歧义，不表示所有pending均完成。"""
        return not (self.undeclared or self.unexecuted or self.uncertain or self.ambiguous)

    @property
    def summary(self) -> Dict[str, int]:
        return {
            "declared": len(self.declared),
            "matched": len(self.matched),
            "undeclared": len(self.undeclared),
            "unexecuted": len(self.unexecuted),
            "uncertain": len(self.uncertain),
            "pending_declarations": len(self.pending_declarations),
            "ambiguous": len(self.ambiguous),
            "resolved": len(self.resolved),
        }

    def findings(self) -> List[str]:
        """人类可读的偏差清单（空列表代表完全一致）。"""
        out: List[str] = []
        for u in self.undeclared:
            out.append(
                f"未声明的执行: tool={u.record.tool_id} "
                f"effect={u.record.effect_id} ({u.reason})")
        for d in self.unexecuted:
            out.append(
                f"计划完成但无已确认归因证据: step={d.step_id} "
                f"tool={d.tool_id}")
        for r in self.uncertain:
            out.append(
                f"副作用结果未决（不算完成）: tool={r.tool_id} "
                f"effect={r.effect_id} status={r.status}")
        for r in self.ambiguous:
            out.append(f"副作用已确认但步骤归因不唯一: tool={r.tool_id} effect={r.effect_id}")
        return out

    # -- 序列化 --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "declared": [d.to_dict() for d in self.declared],
            "matched": [m.to_dict() for m in self.matched],
            "undeclared": [u.to_dict() for u in self.undeclared],
            "unexecuted": [d.to_dict() for d in self.unexecuted],
            "uncertain": [r.to_dict() for r in self.uncertain],
            "pending_declarations": [d.to_dict()
                                     for d in self.pending_declarations],
            "ambiguous": [r.to_dict() for r in self.ambiguous],
            "resolved": [r.to_dict() for r in self.resolved],
            "summary": self.summary,
            "ok": self.ok,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ReconciliationReport":
        if not isinstance(d, dict) or ("ok" in d and not isinstance(d["ok"], bool)):
            raise ReconciliationError("对账报告格式不合法")
        return cls(
            task_id=_text(d.get("task_id"), "task_id"),
            plan_id=_text(d.get("plan_id"), "plan_id"),
            declared=[DeclaredEffect.from_dict(x)
                      for x in _items(d, "declared")],
            matched=[MatchedEffect.from_dict(x)
                     for x in _items(d, "matched")],
            undeclared=[UndeclaredEffect.from_dict(x)
                        for x in _items(d, "undeclared")],
            unexecuted=[DeclaredEffect.from_dict(x)
                        for x in _items(d, "unexecuted")],
            uncertain=[EffectRecord.from_dict(x)
                       for x in _items(d, "uncertain")],
            pending_declarations=[DeclaredEffect.from_dict(x)
                                  for x in _items(d, "pending_declarations")],
            ambiguous=[EffectRecord.from_dict(x) for x in _items(d, "ambiguous")],
            resolved=[EffectRecord.from_dict(x) for x in _items(d, "resolved")],
        )


# ---------------------------------------------------------------- 纯读服务

class SideEffectReconciler:
    """计划 ↔ 副作用账本对账（纯读，零写入权限）。

    所有方法只读，不修改传入参数、不接触账本对象。
    """

    # ---------------- 输入规整 ----------------

    @staticmethod
    def declarations_from_plan(plan: Any) -> List[DeclaredEffect]:
        """从 ExecutionPlan 抽取带工具提示的步骤作为「计划声明」。

        只有声明了 ``tool_hint`` 的步骤才预期产生副作用；``skipped``
        状态的步骤明确不执行，不纳入判定。
        """
        steps = getattr(plan, "steps", None)
        if not isinstance(steps, (list, tuple)) or not steps:
            raise ReconciliationError("计划缺少非空steps")
        seen = set()
        states = {}
        out: List[DeclaredEffect] = []
        for s in steps:
            sid = _text(getattr(s, "step_id", None), "step_id", required=True)
            state = _text(getattr(s, "state", None), "state", required=True)
            if sid in seen or state not in PLAN_STATES:
                raise ReconciliationError("计划步骤重复或状态无效")
            seen.add(sid)
            states[sid] = state
            hint = getattr(s, "tool_hint", None)
            if hint is None or hint == "":
                continue
            _text(hint, "tool_hint", required=True)
            if state in SKIPPED_STATES:
                continue
            out.append(DeclaredEffect(
                step_id=sid,
                tool_id=_text(hint, "tool_hint", required=True),
                step_state=state,
                description=str(getattr(s, "description", "") or "")))
        completed = getattr(plan, "completed_work", [])
        if not isinstance(completed, (list, tuple)) or any(
                not isinstance(sid, str) or states.get(sid) != "done" for sid in completed):
            raise ReconciliationError("完成证据与计划步骤状态冲突")
        out.sort(key=lambda d: d.sort_key())
        return out

    @staticmethod
    def records_from_rows(rows: Iterable[Dict[str, Any]]) -> List[EffectRecord]:
        """把账本行（``task_side_effects`` 投影）转成只读记录列表。"""
        if rows is None:
            raise ReconciliationError("账本投影不可缺失")
        out: List[EffectRecord] = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ReconciliationError("账本记录必须是字典")
            rec = EffectRecord.from_dict(row)
            if rec.effect_id in seen:
                raise ReconciliationError("账本effect_id重复，不能重复抵扣声明")
            seen.add(rec.effect_id)
            out.append(rec)
        out.sort(key=lambda r: r.sort_key())
        return out

    # ---------------- 主入口 ----------------

    def reconcile(self, plan: Any,
                  records: Sequence[Dict[str, Any]],
                  *, task_id: str = "", step_bindings: Optional[Mapping[str, str]] = None) -> ReconciliationReport:
        """把计划声明与实际副作用记录对账（纯读、确定性）。

        参数
        ----
        plan:
            任意具备 ``steps``（每项含 ``step_id`` / ``tool_hint`` / ``state``）
            与可选 ``plan_id`` 的对象——``ExecutionPlan`` 即满足。
        records:
            ``SideEffectLedger.history(task_id)`` 返回的账本行列表。
        task_id:
            目标任务范围；给出时必须与每条记录一致。省略时仅可从单一
            非空task_id推导，混合/缺失任务引用拒绝。
        step_bindings:
            可选effect_id→step_id只读快照，由宿主提供真实关联；本模块
            不把映射视为授权凭据。相同工具多个声明且无绑定时记ambiguous。

        tool_hint在本契约中是预期账本声明。宿主应先筛选实际有副作用的
        工具；本模块不读取注册表猜测纯读工具，也不验证参数/版本相等。
        failed/compensated为已定论resolved，不冒充uncertain或matched。

        返回
        ----
        :class:`ReconciliationReport`
        """
        declared = self.declarations_from_plan(plan)
        rows = self.records_from_rows(records)
        task_id = _text(task_id, "task_id")
        tasks = {r.task_id for r in rows}
        if len(tasks) > 1 or (task_id and tasks and tasks != {task_id}):
            raise ReconciliationError("账本投影不属于同一个目标任务")
        task_id = task_id or next(iter(tasks), "")
        if step_bindings is not None and not isinstance(step_bindings, Mapping):
            raise ReconciliationError("step_bindings必须是effect_id到step_id的映射")
        bindings = dict(step_bindings or {})
        declarations = {d.step_id: d for d in declared}
        by_effect = {r.effect_id: r for r in rows}
        for eid, sid in bindings.items():
            if not isinstance(eid, str) or not isinstance(sid, str) or eid not in by_effect or sid not in declarations:
                raise ReconciliationError("步骤绑定引用不存在")
            if by_effect[eid].tool_id != declarations[sid].tool_id:
                raise ReconciliationError("步骤绑定工具不一致")

        # tool_id -> 尚未被消费的声明列表（多重集：同一工具可声明多次）
        remaining: Dict[str, List[DeclaredEffect]] = {}
        for d in declared:
            remaining.setdefault(d.tool_id, []).append(d)

        matched: List[MatchedEffect] = []
        undeclared: List[UndeclaredEffect] = []
        uncertain: List[EffectRecord] = []
        ambiguous: List[EffectRecord] = []
        resolved: List[EffectRecord] = []
        original_counts = {tool: len(pool) for tool, pool in remaining.items()}

        for rec in rows:
            if not rec.settled:
                uncertain.append(rec)
                continue
            if rec.status != CONFIRMED_STATUS:
                # Native ledger failed/compensated are known terminal results,
                # never unresolved and never evidence of the desired effect.
                resolved.append(rec)
                continue
            pool = remaining.get(rec.tool_id) or []
            if rec.effect_id in bindings:
                target = declarations[bindings[rec.effect_id]]
                decl = next((d for d in pool if d.step_id == target.step_id), None)
            elif original_counts.get(rec.tool_id, 0) > 1:
                ambiguous.append(rec)
                continue
            else:
                decl = pool[0] if pool else None
            if decl is not None:
                pool.remove(decl)
                matched.append(MatchedEffect(step_id=decl.step_id,
                                             tool_id=rec.tool_id,
                                             effect_id=rec.effect_id))
            else:
                # 计划根本没有这项声明（或声明次数已用完）
                undeclared.append(UndeclaredEffect(
                    record=rec,
                    reason="exceeds_declaration_count"
                    if rec.tool_id in remaining else "no_matching_declaration"))

        # 未被任何记录消费的声明：done 步骤算「声称完成却无证据」，
        # 其余状态只是时机未到，如实区分而不是混为一谈。
        unexecuted: List[DeclaredEffect] = []
        pending_declarations: List[DeclaredEffect] = []
        for decl in declared:
            still_open = any(decl is d for pool in remaining.values()
                             for d in pool)
            if not still_open:
                continue
            if decl.step_state in DONE_STATES:
                unexecuted.append(decl)
            else:
                pending_declarations.append(decl)

        matched.sort(key=lambda m: m.sort_key())
        undeclared.sort(key=lambda u: u.sort_key())
        uncertain.sort(key=lambda r: r.sort_key())
        unexecuted.sort(key=lambda d: d.sort_key())
        pending_declarations.sort(key=lambda d: d.sort_key())

        report = ReconciliationReport(
            task_id=task_id,
            plan_id=str(getattr(plan, "plan_id", "") or ""),
            declared=declared, matched=matched, undeclared=undeclared,
            unexecuted=unexecuted, uncertain=uncertain,
            pending_declarations=pending_declarations,
            ambiguous=ambiguous, resolved=resolved,
        )
        return report

    # ---------------- 便利只读入口 ----------------

    def reconcile_ledger(self, plan: Any, ledger: Any, *,
                         task_id: str = "", step_bindings: Optional[Mapping[str, str]] = None) -> ReconciliationReport:
        """从账本读取历史再对账。

        只调用 ``ledger.history(task_id)``（只读查询）。本方法**不调用**
        transition / reconcile / record / begin / finish 中任何一个。
        """
        if not callable(getattr(ledger, "history", None)):
            raise ReconciliationError("账本对象缺少只读 history() 查询接口")
        _text(task_id, "task_id", required=True)
        rows = ledger.history(task_id)
        return self.reconcile(plan, rows, task_id=task_id, step_bindings=step_bindings)


def reconcile_plan(plan: Any, records: Sequence[Dict[str, Any]],
                   *, task_id: str = "", step_bindings: Optional[Mapping[str, str]] = None) -> ReconciliationReport:
    """模块级便捷函数，等价于 :meth:`SideEffectReconciler.reconcile`。"""
    return SideEffectReconciler().reconcile(plan, records, task_id=task_id, step_bindings=step_bindings)
