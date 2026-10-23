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
* **不猜**：只按 ``tool_id`` 字面归因；``file_write`` 与 ``file.append`` 是否
  等价由调用方判定，本模块把未匹配项原样暴露而非猜测等价。

本模块是 ALN-07 整包的第四项，纯新文件，不修改任何既有共享模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
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


class ReconciliationError(ValueError):
    """对账输入不合法（记录缺字段、计划不可读等）。"""


# ---------------------------------------------------------------- 数据契约

@dataclass(frozen=True)
class DeclaredEffect:
    """计划步骤声明的预期副作用（源自 ``PlanStep.tool_hint``）。"""

    step_id: str
    tool_id: str
    step_state: str = "pending"
    description: str = ""

    def sort_key(self) -> Tuple[str, str]:
        return (self.step_id, self.tool_id)

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "tool_id": self.tool_id,
                "step_state": self.step_state,
                "description": self.description}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DeclaredEffect":
        return cls(step_id=str(d.get("step_id") or ""),
                   tool_id=str(d.get("tool_id") or ""),
                   step_state=str(d.get("step_state") or "pending"),
                   description=str(d.get("description") or ""))


@dataclass(frozen=True)
class EffectRecord:
    """副作用账本记录的只读投影。

    对应 ``task_side_effects`` 的字段；缺失的键按空字符串处理，
    ``tool_id`` 为空的记录无法归因，会被判为 :class:`ReconciliationError`。
    """

    effect_id: str
    tool_id: str
    status: str
    idempotency_key: str = ""
    effect_summary: str = ""
    run_id: str = ""

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
                "run_id": self.run_id}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EffectRecord":
        return cls(effect_id=str(d.get("effect_id") or ""),
                   tool_id=str(d.get("tool_id") or ""),
                   status=str(d.get("status") or ""),
                   idempotency_key=str(d.get("idempotency_key") or ""),
                   effect_summary=str(d.get("effect_summary") or ""),
                   run_id=str(d.get("run_id") or ""))


@dataclass(frozen=True)
class MatchedEffect:
    """计划声明与账本记录的成功归因。"""

    step_id: str
    tool_id: str
    effect_id: str

    def sort_key(self) -> Tuple[str, str, str]:
        return (self.step_id, self.tool_id, self.effect_id)

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "tool_id": self.tool_id,
                "effect_id": self.effect_id}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MatchedEffect":
        return cls(step_id=str(d.get("step_id") or ""),
                   tool_id=str(d.get("tool_id") or ""),
                   effect_id=str(d.get("effect_id") or ""))


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


@dataclass
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
    ok: bool = False

    # -- 判定 ----------------------------------------------------------

    @property
    def divergence_free(self) -> bool:
        """与 :attr:`ok` 同义的可读别名：无越权、无缺失、无未决。"""
        return not (self.undeclared or self.unexecuted or self.uncertain)

    @property
    def summary(self) -> Dict[str, int]:
        return {
            "declared": len(self.declared),
            "matched": len(self.matched),
            "undeclared": len(self.undeclared),
            "unexecuted": len(self.unexecuted),
            "uncertain": len(self.uncertain),
            "pending_declarations": len(self.pending_declarations),
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
                f"声称已完成但无副作用证据: step={d.step_id} "
                f"tool={d.tool_id}")
        for r in self.uncertain:
            out.append(
                f"副作用结果未决（不算完成）: tool={r.tool_id} "
                f"effect={r.effect_id} status={r.status}")
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
            "summary": self.summary,
            "ok": self.ok,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ReconciliationReport":
        return cls(
            task_id=str(d.get("task_id") or ""),
            plan_id=str(d.get("plan_id") or ""),
            declared=[DeclaredEffect.from_dict(x)
                      for x in (d.get("declared") or []) if isinstance(x, dict)],
            matched=[MatchedEffect.from_dict(x)
                     for x in (d.get("matched") or []) if isinstance(x, dict)],
            undeclared=[UndeclaredEffect.from_dict(x)
                        for x in (d.get("undeclared") or [])
                        if isinstance(x, dict)],
            unexecuted=[DeclaredEffect.from_dict(x)
                        for x in (d.get("unexecuted") or [])
                        if isinstance(x, dict)],
            uncertain=[EffectRecord.from_dict(x)
                       for x in (d.get("uncertain") or [])
                       if isinstance(x, dict)],
            pending_declarations=[DeclaredEffect.from_dict(x)
                                  for x in (d.get("pending_declarations") or [])
                                  if isinstance(x, dict)],
            ok=bool(d.get("ok", False)),
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
        steps = getattr(plan, "steps", None) or []
        out: List[DeclaredEffect] = []
        for s in steps:
            hint = getattr(s, "tool_hint", None)
            if not hint:
                continue
            state = str(getattr(s, "state", "pending") or "pending")
            if state in SKIPPED_STATES:
                continue
            out.append(DeclaredEffect(
                step_id=str(getattr(s, "step_id", "") or ""),
                tool_id=str(hint),
                step_state=state,
                description=str(getattr(s, "description", "") or "")))
        out.sort(key=lambda d: d.sort_key())
        return out

    @staticmethod
    def records_from_rows(rows: Iterable[Dict[str, Any]]) -> List[EffectRecord]:
        """把账本行（``task_side_effects`` 投影）转成只读记录列表。"""
        out: List[EffectRecord] = []
        for row in rows or []:
            if not isinstance(row, dict):
                raise ReconciliationError("账本记录必须是字典")
            rec = EffectRecord.from_dict(row)
            if not rec.effect_id:
                raise ReconciliationError("账本记录缺少 effect_id")
            if not rec.tool_id:
                raise ReconciliationError(
                    f"账本记录 {rec.effect_id} 缺少 tool_id，无法归因")
            out.append(rec)
        out.sort(key=lambda r: r.sort_key())
        return out

    # ---------------- 主入口 ----------------

    def reconcile(self, plan: Any,
                  records: Sequence[Dict[str, Any]],
                  *, task_id: str = "") -> ReconciliationReport:
        """把计划声明与实际副作用记录对账（纯读、确定性）。

        参数
        ----
        plan:
            任意具备 ``steps``（每项含 ``step_id`` / ``tool_hint`` / ``state``）
            与可选 ``plan_id`` 的对象——``ExecutionPlan`` 即满足。
        records:
            ``SideEffectLedger.history(task_id)`` 返回的账本行列表。
        task_id:
            记账标识，仅用于报告标注。

        返回
        ----
        :class:`ReconciliationReport`
        """
        declared = self.declarations_from_plan(plan)
        rows = self.records_from_rows(records)

        # tool_id -> 尚未被消费的声明列表（多重集：同一工具可声明多次）
        remaining: Dict[str, List[DeclaredEffect]] = {}
        for d in declared:
            remaining.setdefault(d.tool_id, []).append(d)

        matched: List[MatchedEffect] = []
        undeclared: List[UndeclaredEffect] = []
        uncertain: List[EffectRecord] = []

        for rec in rows:
            if not rec.settled or rec.status != CONFIRMED_STATUS:
                # fail-closed：未定论或失败都归未决，绝不算「已确认发生」
                uncertain.append(rec)
                continue
            pool = remaining.get(rec.tool_id) or []
            if pool:
                decl = pool.pop(0)
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
            ok=False,
        )
        report.ok = report.divergence_free
        return report

    # ---------------- 便利只读入口 ----------------

    def reconcile_ledger(self, plan: Any, ledger: Any, *,
                         task_id: str = "") -> ReconciliationReport:
        """从账本读取历史再对账。

        只调用 ``ledger.history(task_id)``（只读查询）。本方法**不调用**
        transition / reconcile / record / begin / finish 中任何一个。
        """
        if not callable(getattr(ledger, "history", None)):
            raise ReconciliationError("账本对象缺少只读 history() 查询接口")
        rows = ledger.history(task_id)
        return self.reconcile(plan, rows, task_id=task_id)


def reconcile_plan(plan: Any, records: Sequence[Dict[str, Any]],
                   *, task_id: str = "") -> ReconciliationReport:
    """模块级便捷函数，等价于 :meth:`SideEffectReconciler.reconcile`。"""
    return SideEffectReconciler().reconcile(plan, records, task_id=task_id)
