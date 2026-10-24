"""ALN-07-P 运行时补审（aafca22）第三方复核探针。

针对 RT07-01..07 写独立探针。刻意区分两类判别力，报告里如实标注：

* **行为型**：同一 API 在新旧实现上都存在，但行为不同 → 强判别力
* **能力型**：旧实现压根没有该函数/校验 → 只能证明「能力补齐」，
  不能单独证明「行为修对了」

V7 是护栏：正常场景必须仍然工作，防止「全拦截」伪装成修好。
"""
from __future__ import annotations

import json
import sys
import threading
import time

RESULTS: list = []


def probe(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"[PASS] {name}")
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")


def _plan(steps, *, plan_id="plan_main"):
    from evoagent.planning.planner import ExecutionPlan, PlanStep
    return ExecutionPlan(plan_id=plan_id, goal="运行时复核", steps=[
        PlanStep(sid, desc, deps=list(deps), tool_hint=hint, state=state)
        for sid, desc, deps, hint, state in steps])


# ---------- RT07-01：依赖未就绪的步骤不得被选中（行为型）----------

def v1_unready_step_not_selected():
    from evoagent.react.engine import _current_step
    # s2 在存储顺序上更靠前，但它依赖还没 done 的 s1
    plan = _plan([
        ("s2", "依赖未满足", ["s1"], "risky_tool", "pending"),
        ("s1", "已就绪", [], "safe_tool", "pending"),
    ])
    cur = _current_step(plan)
    assert cur is not None, "有就绪步骤却没选中任何步骤"
    assert cur.step_id == "s1", (
        f"选中了依赖未满足的步骤 {cur.step_id}（仅按存储顺序取，未校验 deps）")


def v1b_failed_ancestor_blocks_branch():
    from evoagent.react.engine import _tool_requires_unready_branch
    plan = _plan([
        ("a", "失败祖先", [], "risky_tool", "failed"),
        ("b", "依赖失败祖先", ["a"], "risky_tool", "pending"),
    ])
    assert _tool_requires_unready_branch(plan, "risky_tool") is True, (
        "失败祖先分支上的工具未被拦截（可绕过失败前置继续执行）")


# ---------- RT07-03：submitted-only 不算完成（行为型）----------

def v2_submitted_only_is_not_success():
    from evoagent.react.engine import last_success
    from evoagent.tools.contract import ToolResult

    # 已提交但未执行：副作用状态未知，不能当步骤完成
    submitted_only = ToolResult(True, submitted=True, executed=False,
                                contract_valid=True)
    assert not last_success(submitted_only), (
        "submitted-only（未执行）被当作成功——提交不等于完成")

    # 对照：真正执行且契约通过才算
    real = ToolResult(True, submitted=True, executed=True,
                      contract_valid=True)
    assert last_success(real), "真正执行完成的结果被判为失败（护栏）"


def v2b_contract_invalid_is_not_success():
    from evoagent.react.engine import last_success
    from evoagent.tools.contract import ToolResult
    bad_contract = ToolResult(True, submitted=True, executed=True,
                              contract_valid=False)
    assert not last_success(bad_contract), (
        "契约校验失败的结果被当作步骤完成")


# ---------- RT07-04：无关工具失败不得误伤当前步骤（行为型）----------

def v3_unrelated_tool_failure_does_not_fail_step():
    from evoagent.react.engine import _mark_step_failed
    plan = _plan([("s1", "写文件", [], "file_write", "running")])
    _mark_step_failed(plan, "http_get")     # 完全无关的工具失败
    assert plan.steps[0].state == "running", (
        "无关工具失败把当前步骤标成 failed（串账）")
    assert not [s for s in plan.steps if s.step_id.startswith("r")], (
        "无关工具失败仍追加了恢复步骤")


def v3b_matching_tool_failure_marks_step():
    """护栏：匹配的工具失败必须仍能标记步骤（否则失败永远看不见）。"""
    from evoagent.react.engine import _mark_step_failed
    plan = _plan([("s1", "写文件", [], "file_write", "running")])
    _mark_step_failed(plan, "file_write")
    assert plan.steps[0].state == "failed", (
        "匹配工具的真实失败没能标记步骤")


# ---------- RT07-02：损坏计划必须拒绝而非静默降级（能力型）----------

def v4_duplicate_step_ids_rejected():
    from evoagent.planning.planner import PlanRevisionError
    plan = _plan([("s1", "一", [], None, "pending"),
                  ("s1", "重复", [], None, "pending")])
    try:
        plan.validate_execution_state()
    except PlanRevisionError:
        return
    except AttributeError:
        raise
    raise AssertionError("重复 step_id 的损坏计划未被拒绝")


def v4b_done_without_evidence_rejected():
    from evoagent.planning.planner import PlanRevisionError
    # completed_work 声明了 s1，但 s1 并不是 done —— 完成证据自相矛盾
    plan = _plan([("s1", "未完成", [], None, "pending")])
    plan.completed_work.append("s1")
    try:
        plan.validate_execution_state()
    except PlanRevisionError:
        return
    raise AssertionError("完成证据与步骤状态矛盾的损坏计划未被拒绝")


def v4c_valid_plan_passes_validation():
    """护栏：合法计划必须能通过校验。"""
    plan = _plan([("s1", "一", [], None, "done"),
                  ("s2", "二", ["s1"], None, "pending")])
    plan.completed_work.append("s1")
    plan.validate_execution_state()      # 不抛即通过


# ---------- RT07-07：域级 admin 拒绝必须返回 403 而非连接重置 ----------

def v5_domain_rejection_returns_403():
    import http.client

    from evoagent.api.server import ApiServer
    from evoagent.app import App

    app = App(data_dir=_tmpdir())
    app.config.api_token = "operator"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = b'{"policy":{}}'
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        conn.putrequest("POST", "/goal-policies")
        conn.putheader("Content-Length", str(len(payload)))
        conn.putheader("Authorization", "Bearer operator")
        conn.endheaders()
        time.sleep(0.03)                 # 让服务端先处理 header 再读 body
        conn.send(payload)
        resp = conn.getresponse()
        assert resp.status == 403, (
            f"域级 admin 拒绝返回了 {resp.status}，期望 403（未被消费的体导致重置？）")
        assert json.loads(resp.read())["error_code"]
    except (ConnectionError, ConnectionResetError, ConnectionAbortedError) as exc:
        raise AssertionError(
            f"拒绝时连接被重置而非返回 403：{type(exc).__name__}") from exc
    finally:
        conn.close()
        server.shutdown()
        thread.join(5)
        app.db.close()


def _tmpdir():
    import tempfile
    return tempfile.mkdtemp(prefix="aln07p-probe-")


# ---------- 护栏：正常计划仍能按依赖顺序推进 ----------

def v6_ready_chain_still_progresses():
    from evoagent.react.engine import _advance_plan, _current_step
    plan = _plan([("s1", "一", [], "file_write", "pending"),
                  ("s2", "二", ["s1"], "http_post", "pending")])
    assert _current_step(plan).step_id == "s1"
    assert _advance_plan(plan, "file_write") is True
    assert plan.steps[0].state == "done"
    assert _current_step(plan).step_id == "s2", "依赖满足后应推进到下一步"


if __name__ == "__main__":
    probe("V1  RT07-01 unready_step_not_selected (行为型)",
          v1_unready_step_not_selected)
    probe("V1b RT07-01 failed_ancestor_blocks_branch (能力型)",
          v1b_failed_ancestor_blocks_branch)
    probe("V2  RT07-03 submitted_only_is_not_success (行为型)",
          v2_submitted_only_is_not_success)
    probe("V2b RT07-03 contract_invalid_is_not_success (行为型)",
          v2b_contract_invalid_is_not_success)
    probe("V3  RT07-04 unrelated_tool_failure (行为型)",
          v3_unrelated_tool_failure_does_not_fail_step)
    probe("V3b RT07-04 matching_tool_failure (护栏)",
          v3b_matching_tool_failure_marks_step)
    probe("V4  RT07-02 duplicate_step_ids_rejected (能力型)",
          v4_duplicate_step_ids_rejected)
    probe("V4b RT07-02 done_without_evidence_rejected (能力型)",
          v4b_done_without_evidence_rejected)
    probe("V4c RT07-02 valid_plan_passes (护栏)", v4c_valid_plan_passes_validation)
    probe("V5  RT07-07 domain_rejection_returns_403 (行为型)",
          v5_domain_rejection_returns_403)
    probe("V6  guard ready_chain_still_progresses", v6_ready_chain_still_progresses)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\ntotal={len(RESULTS)} passed={passed} failed={len(RESULTS) - passed}")
    sys.exit(0 if passed == len(RESULTS) else 1)
