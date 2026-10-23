"""轨迹编译（ALN-07-T）回归。

夹具先证缺口：直接 import `evoagent.planning.trace`——编译器被删除即红。
覆盖任务卡 §验收 1-6：全状态覆盖、恢复候选分类、确定性、序列化往返、
修订历史进入轨迹、已完成证据不可重放。
"""
from evoagent.planning.planner import ExecutionPlan, PlanStep
from evoagent.planning.trace import PlanTrace, compile_trace


def _plan(**kw):
    steps = kw.pop("steps")
    return ExecutionPlan(plan_id="p", goal="g", steps=steps, **kw)


# ---- 验收 1：全状态覆盖 + 证据进入轨迹 ----
def test_all_states_compiled():
    plan = _plan(steps=[
        PlanStep(step_id="s1", description="完成", state="done"),
        PlanStep(step_id="s2", description="运行中", state="running"),
        PlanStep(step_id="s3", description="待定"),
        PlanStep(step_id="s4", description="失败", state="failed"),
        PlanStep(step_id="s5", description="跳过", state="skipped"),
    ], completed_work=["s1"])
    tr = compile_trace(plan)
    assert tr.states == {"s1": "done", "s2": "running", "s3": "pending",
                         "s4": "failed", "s5": "skipped"}
    assert tr.completed_work == ("s1",)
    assert [s.step_id for s in tr.steps] == ["s1", "s2", "s3", "s4", "s5"]


def test_completed_evidence_preserved():
    plan = _plan(steps=[
        PlanStep(step_id="s1", description="完成", state="done"),
        PlanStep(step_id="s2", description="失败", state="failed"),
    ], completed_work=["s1"], change_reasons=["恢复"],
       revisions=[{"level": "append", "reason": "恢复", "at": 1.0}])
    tr = compile_trace(plan)
    assert tr.completed_work == ("s1",)
    assert tr.revisions[0]["level"] == "append"
    assert tr.change_reasons == ("恢复",)


# ---- 验收 2：恢复候选分类 ----
def test_retryable_when_deps_done():
    plan = _plan(steps=[
        PlanStep(step_id="s1", description="完成", state="done"),
        PlanStep(step_id="s2", description="失败", state="failed",
                 deps=["s1"]),
    ], completed_work=["s1"])
    tr = compile_trace(plan)
    (c,) = tr.recovery_candidates
    assert c.step_id == "s2" and c.kind == "retryable"


def test_ready_after_when_dep_failed():
    plan = _plan(steps=[
        PlanStep(step_id="s1", description="失败", state="failed"),
        PlanStep(step_id="s2", description="失败", state="failed",
                 deps=["s1"]),
    ])
    tr = compile_trace(plan)
    by_id = {c.step_id: c for c in tr.recovery_candidates}
    assert by_id["s1"].kind == "retryable"      # s1 自己无失败依赖
    assert by_id["s2"].kind == "ready_after"
    assert by_id["s2"].requires == ("s1",)


def test_blocked_when_pending_deps():
    plan = _plan(steps=[
        PlanStep(step_id="s1", description="待定"),
        PlanStep(step_id="s2", description="跳过", state="skipped",
                 deps=["s1"]),
    ])
    tr = compile_trace(plan)
    (c,) = tr.recovery_candidates
    assert c.kind == "blocked"
    assert c.requires == ("s1",)


def test_infeasible_when_done_but_marked_failed():
    # 已完成步骤被异常标失败：infeasible，不允许重放成功副作用
    plan = _plan(steps=[
        PlanStep(step_id="s1", description="完成", state="failed"),
    ], completed_work=["s1"])
    tr = compile_trace(plan)
    (c,) = tr.recovery_candidates
    assert c.kind == "infeasible"
    assert "副作用" in c.reason


def test_done_steps_have_no_candidates():
    plan = _plan(steps=[
        PlanStep(step_id="s1", description="完成", state="done"),
        PlanStep(step_id="s2", description="完成", state="done"),
    ], completed_work=["s1", "s2"])
    tr = compile_trace(plan)
    assert tr.recovery_candidates == ()


# ---- 验收 3：确定性 ----
def test_deterministic_regardless_of_dep_order():
    a = _plan(steps=[
        PlanStep(step_id="s1", description="失败", state="failed",
                 deps=["x1", "x2", "x3"]),
        PlanStep(step_id="x1", description="完成", state="done"),
        PlanStep(step_id="x2", description="完成", state="done"),
        PlanStep(step_id="x3", description="完成", state="done"),
    ])
    b = _plan(steps=[
        PlanStep(step_id="s1", description="失败", state="failed",
                 deps=["x3", "x1", "x2"]),
        PlanStep(step_id="x1", description="完成", state="done"),
        PlanStep(step_id="x2", description="完成", state="done"),
        PlanStep(step_id="x3", description="完成", state="done"),
    ])
    ta, tb = compile_trace(a), compile_trace(b)
    assert ta.to_dict() == tb.to_dict()


def test_candidates_sorted_by_step_id():
    plan = _plan(steps=[
        PlanStep(step_id="z", description="失败", state="failed"),
        PlanStep(step_id="a", description="失败", state="failed"),
    ])
    tr = compile_trace(plan)
    assert [c.step_id for c in tr.recovery_candidates] == ["a", "z"]


# ---- 验收 4：序列化往返 ----
def test_roundtrip():
    plan = _plan(steps=[
        PlanStep(step_id="s1", description="完成", state="done",
                 tool_hint="t1"),
        PlanStep(step_id="s2", description="失败", state="failed",
                 deps=["s1"]),
    ], completed_work=["s1"],
       revisions=[{"level": "append", "reason": "r", "at": 2.0}],
       acceptance_criteria=[{"type": "result_nonempty"}])
    plan.execution_mode = "stable_workflow"
    plan.selection_rationale = "五项通过"
    d = compile_trace(plan).to_dict()
    back = PlanTrace.from_dict(d)
    assert back.to_dict() == d


# ---- 验收 5：修订历史进入轨迹 ----
def test_revisions_into_trace():
    plan = _plan(steps=[PlanStep(step_id="s1", description="x")],
                 change_reasons=["调整原因"])
    plan.revisions.append({"level": "replace", "reason": "调整原因",
                           "added": ["s2"], "removed": [], "at": 3.0})
    tr = compile_trace(plan)
    assert len(tr.revisions) == 1
    assert tr.revisions[0]["added"] == ["s2"]


# ---- 验收 6：模式与理由进入轨迹（07-W 联动） ----
def test_mode_fields_into_trace():
    plan = _plan(steps=[PlanStep(step_id="s1", description="x")])
    plan.execution_mode = "dynamic_planning"
    plan.selection_rationale = "无候选"
    tr = compile_trace(plan)
    assert tr.execution_mode == "dynamic_planning"
    assert tr.selection_rationale == "无候选"


# ---- 纯读 ----
def test_compile_is_read_only():
    plan = _plan(steps=[
        PlanStep(step_id="s1", description="失败", state="failed")])
    before = plan.to_dict()
    compile_trace(plan)
    assert plan.to_dict() == before
