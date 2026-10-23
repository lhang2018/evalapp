"""ALN-07-T/07-R/16-X 修复（f633130）第三方复核独立探针（claude-main）。

针对审核报告 CR24-01..07 中可独立复现的行为面，先读构造签名再写：

X1 (CR24-02) verify_digest=False 不再跳过校验——篡改 event_ids 任何路径都拒绝；
X2 (CR24-02) scope 换成另一 task → 拒绝（墓碑一致性）；
X3 (CR24-01) delete_task 返回嵌套 dict（真实 LayerManager 形态）不再崩溃；
X4 (CR24-03) event_ids 为空也按整 task 删除（诊断证据仍被清除）；
X5 (CR24-04) 导出目标已存在 → 拒绝（排他创建，不截断覆盖）；
X6 (CR24-05) 失败步骤的上游还有 pending 祖先 → requires 传播完整祖先链；
X7 (CR24-05) 依赖环计划 → compile_trace 显式 ValueError（不再列为可恢复）；
X8 (CR24-07) RecoveryPolicy 非法 action 构造即拒绝。
"""
import sys
import os
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def probe(name, fn):
    try:
        fn()
        results.append((name, "PASS"))
    except (AssertionError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        results.append((name, f"FAIL: {type(exc).__name__}: {exc}"))


def _event(eid, task_id="t1"):
    from evoagent.observability.contract import EvidenceEvent, EventRefs
    return EvidenceEvent(
        event_id=eid, event_type="tool.call", timestamp=1000.0 + len(eid),
        severity="info", privacy_level="internal", fault_level="none",
        origin="server", trust="trusted",
        refs=EventRefs(task_id=task_id, run_id="r1"))


def _plan(events, task_id="t1", actor="admin", reason="r"):
    from evoagent.observability.export import plan_deletion
    return plan_deletion(events, task_id=task_id, actor=actor, reason=reason)


# ---- X1: verify_digest=False 不再跳过 ----
def x1_verify_flag_cannot_bypass():
    from evoagent.observability.export import execute_deletion, DeletionScopeError
    events = [_event("e1"), _event("e2")]
    plan = _plan(events)
    tampered = type(plan)(scope=plan.scope, actor=plan.actor, reason=plan.reason,
                          event_ids=plan.event_ids + ("e99",),
                          digest=plan.digest, tombstone=plan.tombstone)
    d = _Deleter()
    try:
        execute_deletion(d, tampered, actor="admin", reason="r",
                         verify_digest=False)
    except DeletionScopeError:
        return
    raise AssertionError("verify_digest=False 绕过了校验并执行删除")


# ---- X2: scope 调包拒绝 ----
def x2_scope_swap_rejected():
    from evoagent.observability.export import execute_deletion, DeletionScopeError
    plan = _plan([_event("e1", "t1")], task_id="t1")
    swapped = type(plan)(scope="t2", actor=plan.actor, reason=plan.reason,
                         event_ids=plan.event_ids, digest=plan.digest,
                         tombstone=plan.tombstone)
    d = _Deleter()
    try:
        execute_deletion(d, swapped, actor="admin", reason="r")
    except DeletionScopeError:
        return
    raise AssertionError("把 scope 换成另一 task 仍执行了删除")


# ---- X3: 嵌套 dict 计数不崩溃 ----
def x3_nested_counts_ok():
    from evoagent.observability.export import execute_deletion
    plan = _plan([_event("e1")])
    class NestedDeleter:
        def delete_task(self, task_id):
            # 真实 LayerManager 形态：diagnostics 是嵌套 dict
            return {"runtime_event": 1, "execution_trace": 0,
                    "diagnostic_evidence": 0,
                    "diagnostics": {"cases": 2, "hypotheses": 1,
                                    "experiments": 0}}
    out = execute_deletion(NestedDeleter(), plan, actor="admin", reason="r")
    assert out["ok"] is True and out["total"] == 4, (
        f"嵌套计数未正确汇总: {out['total']}")


# ---- X4: 空 event_ids（合法计划）也整 task 删除 ----
def x4_empty_ids_still_deletes_task():
    from evoagent.observability.export import plan_deletion, execute_deletion
    # 正规路径：task 无任何事件 → plan_deletion 产出合法空计划
    plan = plan_deletion([], task_id="t-empty", actor="admin", reason="r")
    assert plan.event_ids == ()
    d = _Deleter()
    out = execute_deletion(d, plan, actor="admin", reason="r")
    assert d.calls == 1, "空 event_ids 的合法计划未执行整 task 删除"
    assert out["total"] >= 2, "诊断证据未被清除"
    # 反向：伪造空 event_ids + 不自洽 digest → 必须拒绝（防伪）
    from evoagent.observability.export import DeletionScopeError
    forged = type(plan)(scope="t1", actor="admin", reason="r", event_ids=(),
                        digest="deadbeef", tombstone={})
    try:
        execute_deletion(_Deleter(), forged, actor="admin", reason="r")
    except DeletionScopeError:
        return
    raise AssertionError("伪造的不自洽计划被放行")


# ---- X5: 导出排他创建 ----
def x5_export_no_clobber(tmp=None):
    from evoagent.observability.export import EvidenceExporter, ExportDenied, \
        export_to_file
    root = tempfile.mkdtemp(prefix="x5-")
    target = os.path.join(root, "report.json")
    with open(target, "w", encoding="utf-8") as f:
        f.write("precious existing content")
    try:
        export_to_file(EvidenceExporter(), [_event("e1")], "report.json",
                       actor="admin", reason="r", allowed_root=root)
    except ExportDenied:
        # 拒绝且原文件未被截断
        with open(target, encoding="utf-8") as f:
            assert f.read() == "precious existing content", (
                "拒绝前已截断原文件")
        return
    raise AssertionError("已存在目标被覆盖写")


class _Deleter:
    def __init__(self):
        self.calls = 0

    def delete_task(self, task_id):
        self.calls += 1
        n = 2 if self.calls == 1 else 0
        return {"runtime_event": n, "execution_trace": 0,
                "diagnostic_evidence": 0, "diagnostics": n}


# ---- X6: 失败上游的 pending 祖先传播 ----
def x6_pending_ancestor_propagates():
    from evoagent.planning.planner import ExecutionPlan, PlanStep
    from evoagent.planning.trace import compile_trace
    plan = ExecutionPlan(plan_id="p", goal="g", steps=[
        PlanStep(step_id="a", description="未完成"),
        PlanStep(step_id="b", description="失败", state="failed", deps=["a"]),
        PlanStep(step_id="c", description="失败", state="failed", deps=["b"]),
    ])
    tr = compile_trace(plan)
    by = {c.step_id: c for c in tr.recovery_candidates}
    # c 的失败上游 b 之上还有未完成祖先 a → c 的 requires 必须包含 a
    assert by["c"].requires and "a" in by["c"].requires, (
        f"pending 祖先未传播进 requires: {by['c'].to_dict()}")


# ---- X7: 依赖环显式拒绝 ----
def x7_cycle_rejected():
    from evoagent.planning.planner import ExecutionPlan, PlanStep
    from evoagent.planning.trace import compile_trace
    plan = ExecutionPlan(plan_id="p", goal="g", steps=[
        PlanStep(step_id="a", description="失败", state="failed", deps=["b"]),
        PlanStep(step_id="b", description="失败", state="failed", deps=["a"]),
    ])
    try:
        compile_trace(plan)
    except ValueError:
        return
    raise AssertionError("依赖环计划未被显式拒绝")


# ---- X8: RecoveryPolicy action 枚举 ----
def x8_bad_action_rejected():
    from evoagent.planning.planner import RecoveryPolicy
    try:
        RecoveryPolicy(category="c", action="auto_delete_everything")
    except ValueError:
        return
    raise AssertionError("非法 action 被接受")


probe("X1 verify-flag-cannot-bypass", x1_verify_flag_cannot_bypass)
probe("X2 scope-swap-rejected", x2_scope_swap_rejected)
probe("X3 nested-counts-ok", x3_nested_counts_ok)
probe("X4 empty-ids-still-deletes", x4_empty_ids_still_deletes_task)
probe("X5 export-no-clobber", x5_export_no_clobber)
probe("X6 pending-ancestor-propagates", x6_pending_ancestor_propagates)
probe("X7 cycle-rejected", x7_cycle_rejected)
probe("X8 bad-action-rejected", x8_bad_action_rejected)

print("=" * 56)
for name, status in results:
    print(f"{status:8} {name}")
failed = [n for n, s in results if s.startswith("FAIL")]
print("=" * 56)
print(f"total={len(results)} pass={len(results)-len(failed)} fail={len(failed)}")
sys.exit(1 if failed else 0)
