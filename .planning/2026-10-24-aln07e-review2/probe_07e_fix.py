"""ALN-07-E 修复（26aaec7）第三方复核探针。

针对审核报告 PR07E-01..06 各写独立探针，验证我自己的原实现（c91621e）
确实存在这些缺陷。所有探针**不使用**修复后新增的 step_bindings 参数，
使判别力落在行为语义上，而不是「接口不存在」。
"""
from __future__ import annotations

import sys

RESULTS: list = []


def probe(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"[PASS] {name}")
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")


def _plan(steps):
    from evoagent.planning.planner import ExecutionPlan, PlanStep
    built = [PlanStep(sid, desc, tool_hint=hint, state=state)
             for sid, desc, hint, state in steps]
    return ExecutionPlan(plan_id="plan_main", goal="对账复核", steps=built)


def _rows(*specs):
    out = []
    for eid, tool, status, task in specs:
        out.append({"effect_id": eid, "task_id": task, "tool_id": tool,
                    "status": status, "idempotency_key": "k", "run_id": "r1"})
    return out


# ---------- PR07E-01：任务范围 ----------

def v1_foreign_task_record_rejected():
    """别的任务的 confirmed 记录不得满足本任务声明。"""
    from evoagent.planning.reconcile import ReconciliationError, reconcile_plan
    plan = _plan([("s1", "写", "file_write", "done")])
    rows = _rows(("e1", "file_write", "confirmed", "task-B"))
    try:
        rep = reconcile_plan(plan, rows, task_id="task-A")
    except ReconciliationError:
        return
    assert not rep.ok, "他任务记录满足了本任务声明（对账范围未校验）"
    assert not rep.matched, "他任务记录被计为已完成归因"


def v2_mixed_tasks_without_target_rejected():
    """记录跨多个任务且未指定目标时必须拒绝，不能猜。"""
    from evoagent.planning.reconcile import ReconciliationError, reconcile_plan
    plan = _plan([("s1", "写", "file_write", "done"),
                  ("s2", "写2", "file_write", "done")])
    rows = _rows(("e1", "file_write", "confirmed", "task-A"),
                 ("e2", "file_write", "confirmed", "task-B"))
    try:
        rep = reconcile_plan(plan, rows)          # 故意省略 task_id
    except ReconciliationError:
        return
    assert not rep.ok, "混合任务记录在未指定目标时被接受"


def v10_ledger_entry_requires_explicit_target():
    """实际账本入口必须要求明确目标（不能用空标签冒充归属）。"""
    from evoagent.planning.reconcile import ReconciliationError, SideEffectReconciler

    class Ledger:
        def history(self, task_id):
            return []

    plan = _plan([("s1", "写", "file_write", "done")])
    try:
        SideEffectReconciler().reconcile_ledger(plan, Ledger())   # 省略 task_id
    except ReconciliationError:
        return
    raise AssertionError("账本入口省略目标未被拒绝")


# ---------- PR07E-02：effect_id 唯一性 ----------

def v3_duplicate_effect_id_cannot_double_count():
    """同一 effect_id 重复输入不得抵扣两条声明。"""
    from evoagent.planning.reconcile import ReconciliationError, reconcile_plan
    plan = _plan([("s1", "写1", "file_write", "done"),
                  ("s2", "写2", "file_write", "done")])
    rows = _rows(("dup", "file_write", "confirmed", "t1"),
                 ("dup", "file_write", "confirmed", "t1"))
    try:
        rep = reconcile_plan(plan, rows, task_id="t1")
    except ReconciliationError:
        return
    assert not rep.ok, "重复 effect_id 被接受并凑成一致"
    assert len(rep.matched) < 2, "同一 effect_id 抵扣了两条声明"


# ---------- PR07E-03：多声明歧义 ----------

def v4_ambiguous_multi_declaration_not_forced():
    """同一工具多条声明、无显式绑定时不得按字典序强行配对。"""
    from evoagent.planning.reconcile import reconcile_plan
    plan = _plan([("s1", "写1", "file_write", "done"),
                  ("s2", "写2", "file_write", "done")])
    rows = _rows(("e1", "file_write", "confirmed", "t1"))
    rep = reconcile_plan(plan, rows, task_id="t1")
    assert not rep.ok, "多声明单记录被判为一致（假装知道是哪一步）"
    ambiguous = getattr(rep, "ambiguous", [])
    assert ambiguous, "歧义记录未被标出，而是被强行归因"


def v5_pending_declaration_cannot_steal_done_record():
    """字典序更小的 pending 声明不得抢走 done 步骤的记录。"""
    from evoagent.planning.reconcile import reconcile_plan
    plan = _plan([("s1", "已完成", "file_write", "done"),
                  ("s2", "待执行", "file_write", "pending")])
    rows = _rows(("e1", "file_write", "confirmed", "t1"))
    rep = reconcile_plan(plan, rows, task_id="t1")
    assert not rep.ok, "单条记录被确定性归因，掩盖了 pending 与 done 的歧义"
    assert all(m.step_id != "s2" for m in rep.matched), \
        "pending 步骤抢走了 done 步骤的记录"


# ---------- PR07E-04：报告不可变与 ok 派生 ----------

def v6_from_dict_does_not_trust_ok():
    """反序列化不得信任传入的 ok；明细与结论矛盾要拒绝。"""
    from evoagent.planning.reconcile import ReconciliationError, ReconciliationReport
    plan = _plan([("s1", "写", "file_write", "done")])
    from evoagent.planning.reconcile import reconcile_plan
    good = reconcile_plan(plan, _rows(("e1", "file_write", "confirmed", "t1")),
                          task_id="t1").to_dict()
    bad = dict(good)
    bad["ok"] = True
    bad["matched"] = []          # 清空归因，但声明仍在 → 矛盾
    bad["unexecuted"] = []
    try:
        restored = ReconciliationReport.from_dict(bad)
    except ReconciliationError:
        return
    assert restored.ok is False, "from_dict 信任了传入的 ok（明细为空却称一致）"


def v7_report_lists_are_frozen():
    """报告明细不可被调用方就地改写。"""
    from evoagent.planning.reconcile import reconcile_plan
    plan = _plan([("s1", "写", "file_write", "done")])
    rep = reconcile_plan(plan, _rows(("e1", "file_write", "confirmed", "t1")),
                         task_id="t1")
    try:
        rep.matched.clear()      # 若可清除，则明细与 ok 会脱节
    except AttributeError:
        return
    assert len(rep.matched) == 1, "报告明细可被就地清空（结论与明细脱节）"


# ---------- PR07E-05：损坏输入 ----------

def v8_none_plan_is_not_clean():
    """None 计划 + 空记录不得判为一致。"""
    from evoagent.planning.reconcile import ReconciliationError, reconcile_plan
    try:
        rep = reconcile_plan(None, [], task_id="t1")
    except (ReconciliationError, TypeError, AttributeError):
        return
    assert not rep.ok, "None 计划被当作干净计划，报告判为一致"


# ---------- PR07E-06：failed/compensated 归类 ----------

def v9_failed_is_resolved_not_uncertain():
    """failed 表示明确未执行，属已定论，不得混入未决。"""
    from evoagent.planning.reconcile import reconcile_plan
    plan = _plan([("s1", "写", "file_write", "done")])
    rep = reconcile_plan(plan, _rows(("e1", "file_write", "failed", "t1")),
                         task_id="t1")
    uncertain_ids = [r.effect_id for r in rep.uncertain]
    assert "e1" not in uncertain_ids, \
        "failed（已明确未执行）被归为未决，丢失了定论语义"
    resolved_ids = [r.effect_id for r in getattr(rep, "resolved", [])]
    assert "e1" in resolved_ids, "failed 未进入 resolved 分类"


# ---------- 护栏：正常一致场景仍然通过 ----------

def v11_clean_case_still_ok():
    """修复不得把所有场景都变成拒绝（成功路径护栏）。"""
    from evoagent.planning.reconcile import reconcile_plan
    plan = _plan([("s1", "写", "file_write", "done"),
                  ("s2", "发", "http_post", "done")])
    rows = _rows(("e1", "file_write", "confirmed", "t1"),
                 ("e2", "http_post", "confirmed", "t1"))
    rep = reconcile_plan(plan, rows, task_id="t1")
    assert rep.ok, f"干净场景被判为不一致: {rep.findings()}"
    assert len(rep.matched) == 2


def v12_bound_ambiguity_resolved_by_binding():
    """宿主给出显式绑定时，多声明应可确定归因（不留歧义）。"""
    from evoagent.planning.reconcile import reconcile_plan
    plan = _plan([("s1", "写1", "file_write", "done"),
                  ("s2", "写2", "file_write", "done")])
    rows = _rows(("e1", "file_write", "confirmed", "t1"),
                 ("e2", "file_write", "confirmed", "t1"))
    try:
        rep = reconcile_plan(plan, rows, task_id="t1",
                             step_bindings={"e1": "s2", "e2": "s1"})
    except TypeError:
        raise AssertionError("修复版应支持 step_bindings 显式绑定")
    assert rep.ok, f"有显式绑定仍判歧义: {rep.findings()}"
    pairs = sorted((m.effect_id, m.step_id) for m in rep.matched)
    assert pairs == [("e1", "s2"), ("e2", "s1")], f"绑定未按快照生效: {pairs}"


if __name__ == "__main__":
    probe("V1  foreign_task_record_rejected", v1_foreign_task_record_rejected)
    probe("V2  mixed_tasks_without_target_rejected",
          v2_mixed_tasks_without_target_rejected)
    probe("V10 ledger_entry_requires_explicit_target",
          v10_ledger_entry_requires_explicit_target)
    probe("V3  duplicate_effect_id_cannot_double_count",
          v3_duplicate_effect_id_cannot_double_count)
    probe("V4  ambiguous_multi_declaration_not_forced",
          v4_ambiguous_multi_declaration_not_forced)
    probe("V5  pending_declaration_cannot_steal_done_record",
          v5_pending_declaration_cannot_steal_done_record)
    probe("V6  from_dict_does_not_trust_ok", v6_from_dict_does_not_trust_ok)
    probe("V7  report_lists_are_frozen", v7_report_lists_are_frozen)
    probe("V8  none_plan_is_not_clean", v8_none_plan_is_not_clean)
    probe("V9  failed_is_resolved_not_uncertain",
          v9_failed_is_resolved_not_uncertain)
    probe("V11 clean_case_still_ok (护栏)", v11_clean_case_still_ok)
    probe("V12 bound_ambiguity_resolved_by_binding",
          v12_bound_ambiguity_resolved_by_binding)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\ntotal={len(RESULTS)} passed={passed} failed={len(RESULTS) - passed}")
    sys.exit(0 if passed == len(RESULTS) else 1)
