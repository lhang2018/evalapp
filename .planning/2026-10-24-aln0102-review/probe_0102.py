"""ALN-01-A / ALN-02-A 独立复核探针（claude-main 自写，不引用被审方测试）。

每根探针验证一条交付声明中的硬不变量。凡「声明 X 应被拒绝」的，
必须同时验证副作用未发生（len(calls)==0 / effect 未落账），
否则「拒绝了但仍执行」会漏判。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

RESULTS: list[tuple[str, bool, str]] = []


def probe(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"[PASS] {name}")
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")


def _ok(data=None):
    from evoagent.tools.contract import ToolResult
    return ToolResult(True, data=data, submitted=True, executed=True,
                      contract_valid=True, independently_verified=True,
                      goal_reached=True)


def _gateway(tag):
    """新建独立 Registry + Database（每次隔离）。"""
    from evoagent.core.persistence import Database
    from evoagent.core.permissions import PermissionController
    from evoagent.tools.registry import ToolRegistry
    tmp = tempfile.mkdtemp(prefix=f"aln0102-{tag}-")
    db = Database(os.path.join(tmp, "probe.db"))
    return ToolRegistry(PermissionController(high_risk_mode="allow"), db), db


def _grant(gw, **overrides):
    policy = {"tools": ["action"], "permissions": []}
    policy.update(overrides)
    return gw.authorizations.issue("task", policy, actor="probe-admin")


# ---------------------------------------------------------------- ALN-01-A

def p1_permission_requirement_is_not_a_grant():
    """工具声明的 permission_requirements 不能自我授权。"""
    from evoagent.tools.contract import ToolContract
    gw, db = _gateway("p1")
    try:
        calls = []
        gw.register(ToolContract("action", permission_requirements=["device.admin"]),
                    lambda a: calls.append(a) or _ok())
        _grant(gw)
        denied = gw.execute("action", {}, task_id="task")
        assert not denied.ok, "未授权却被放行：声明了所需权限不等于获得授权"
        assert not calls, "拒绝却仍执行了工具（副作用已发生）"
    finally:
        db.close()


def p2_path_prefix_lookalike_not_contained():
    """'inside-other' 与 'inside' 前缀相似，不能因字符串前缀而放行。"""
    from evoagent.tools.contract import ToolContract
    gw, db = _gateway("p2")
    try:
        calls = []
        gw.register(ToolContract("action", resource_bindings={"path": "path"}),
                    lambda a: calls.append(a) or _ok())
        root = tempfile.mkdtemp(prefix="aln0102-roots-")
        inside = os.path.join(root, "inside")
        lookalike = os.path.join(root, "inside-other")
        for d in (inside, lookalike):
            os.makedirs(d, exist_ok=True)
        _grant(gw, path_roots=[inside])
        ok_in = gw.execute("action", {"path": os.path.join(inside, "f")},
                           task_id="task")
        ok_out = gw.execute("action", {"path": os.path.join(lookalike, "f")},
                            task_id="task")
        assert ok_in.ok, "授权目录内的合法路径被误拒（对照组应通过）"
        assert not ok_out.ok, "前缀相似目录被放行——范围检查退化成字符串前缀比较"
        assert len(calls) == 1, f"越界路径也执行了: calls={len(calls)}"
    finally:
        db.close()


def p3_path_traversal_rejected():
    """'..' 穿越出授权根即拒绝。"""
    from evoagent.tools.contract import ToolContract
    gw, db = _gateway("p3")
    try:
        calls = []
        gw.register(ToolContract("action", resource_bindings={"path": "path"}),
                    lambda a: calls.append(a) or _ok())
        root = tempfile.mkdtemp(prefix="aln0102-trav-")
        inside = os.path.join(root, "inside")
        os.makedirs(inside, exist_ok=True)
        _grant(gw, path_roots=[inside])
        res = gw.execute(
            "action", {"path": os.path.join(inside, "..", "outside")},
            task_id="task")
        assert not res.ok, "路径穿越出授权根被放行"
        assert not calls, "拒绝却仍执行了工具"
    finally:
        db.close()


def p4_task_grant_cannot_expand_expert_whitelist():
    """调用方允许列表为空时，任务授权不得反向扩大可调用集。"""
    from evoagent.tools.contract import ToolContract
    gw, db = _gateway("p4")
    try:
        calls = []
        gw.register(ToolContract("action"), lambda a: calls.append(a) or _ok())
        _grant(gw)
        res = gw.execute("action", {}, task_id="task", allowed_tools=[])
        assert not res.ok, "空允许列表被任务授权扩大"
        assert not calls, "拒绝却仍执行了工具"
    finally:
        db.close()


def p5_explicit_empty_list_means_no_resource():
    """显式空 targets=[] = 不允许该类资源，不得解释为无限制。"""
    from evoagent.tools.contract import ToolContract
    gw, db = _gateway("p5")
    try:
        calls = []
        gw.register(ToolContract("action"), lambda a: calls.append(a) or _ok())
        _grant(gw, targets=[])
        res = gw.execute("action", {"target": "a"}, task_id="task")
        assert not res.ok, "显式空数组被解释为无限制（空集合≠无限制）"
        assert not calls
    finally:
        db.close()


def p6_revocation_blocks_new_action():
    """撤销后，同一任务的新动作必须被拒。"""
    from evoagent.tools.contract import ToolContract
    gw, db = _gateway("p6")
    try:
        calls = []
        gw.register(ToolContract("action"), lambda a: calls.append(a) or _ok())
        ref = _grant(gw)
        assert gw.execute("action", {}, task_id="task").ok, "撤销前对照组应通过"
        calls.clear()
        gw.authorizations.revoke(ref)
        res = gw.execute("action", {}, task_id="task")
        assert not res.ok, "撤销后仍放行动作"
        assert not calls, "撤销后仍执行了工具"
    finally:
        db.close()


def p7_unknown_policy_field_rejected():
    """未知授权字段不得静默忽略（如 routes/schemas 之类未在白名单的键）。"""
    gw, db = _gateway("p7")
    try:
        try:
            gw.authorizations.issue("task", {"tools": ["action"],
                                             "permissions": [],
                                             "routes": ["*"]},
                                    actor="probe-admin")
        except ValueError:
            return
        raise AssertionError("未知授权字段被静默接受（可能绕过范围白名单）")
    finally:
        db.close()


# ---------------------------------------------------------------- ALN-02-A

def _unknown(gw, reconciler=None):
    from evoagent.tools.contract import ToolContract, ToolResult
    gw.register(ToolContract("send", side_effect_level="external",
                             reconcile=reconciler),
                lambda a: ToolResult(False, side_effect_uncertain=True,
                                     error="reply lost"))
    res = gw.execute("send", {"target": "recipient"}, task_id="task",
                     idempotency_key="operation")
    return res.evidence["effect_id"]


def _confirmed(operation, receipt):
    result = _ok({"restored_artifact": "receipt-output"})
    result.evidence["external_receipt_ref"] = "provider:receipt-1"
    return result


def r1_no_adapter_keeps_unknown():
    """未注册 reconcile 适配器 → 明确拒绝，状态保持 unknown。"""
    gw, db = _gateway("r1")
    try:
        eid = _unknown(gw, reconciler=None)
        try:
            gw.reconcile_effect(eid, {"ref": "receipt-1"})
        except ValueError:
            pass
        else:
            raise AssertionError("无适配器却接受了对账")
        assert gw.effects.get(eid)["status"] == "unknown", \
            "无适配器对账后状态脱离 unknown（伪释放 pending）"
    finally:
        db.close()


def r2_unverified_receipt_keeps_unknown():
    """未被独立验证的回执不得释放 pending。"""
    from evoagent.tools.contract import ToolResult
    gw, db = _gateway("r2")
    try:
        def weak(operation, receipt):
            r = _confirmed(operation, receipt)
            r.independently_verified = False
            return r
        eid = _unknown(gw, reconciler=weak)
        try:
            gw.reconcile_effect(eid, {"ref": "receipt-1"})
        except ValueError:
            pass
        else:
            raise AssertionError("未独立验证的回执被接受")
        assert gw.effects.get(eid)["status"] == "unknown"
    finally:
        db.close()


def r3_missing_receipt_ref_keeps_unknown():
    """回执缺少可核验引用（版本号/收据号）不得释放 pending。"""
    gw, db = _gateway("r3")
    try:
        def nosref(operation, receipt):
            r = _confirmed(operation, receipt)
            r.evidence.clear()
            return r
        eid = _unknown(gw, reconciler=nosref)
        try:
            gw.reconcile_effect(eid, {})
        except ValueError:
            pass
        else:
            raise AssertionError("无收据引用的回执被接受")
        assert gw.effects.get(eid)["status"] == "unknown"
    finally:
        db.close()


def r4_version_mismatch_keeps_unknown():
    """工具版本不符不得复用操作键释放 pending。"""
    gw, db = _gateway("r4")
    try:
        eid = _unknown(gw, reconciler=_confirmed)
        gw.contract("send").tool_version = "2"
        try:
            gw.reconcile_effect(eid, {})
        except ValueError:
            pass
        else:
            raise AssertionError("版本不符却接受了回执")
        assert gw.effects.get(eid)["status"] == "unknown"
    finally:
        db.close()


def r5_verified_receipt_replays_without_reexecution():
    """护栏：真正可核验的回执应恢复结果且不重复执行第二遍。"""
    gw, db = _gateway("r5")
    try:
        eid = _unknown(gw, reconciler=_confirmed)
        gw.reconcile_effect(eid, {"ref": "receipt-1"})
        assert gw.effects.get(eid)["status"] != "unknown", \
            "可核验回执未能释放 pending（成功路径缺失，套件可能只测了拒绝）"
        calls = []
        from evoagent.tools.contract import ToolContract
        gw.register(ToolContract("send", side_effect_level="external"),
                    lambda a: calls.append(a) or _ok())
        res = gw.execute("send", {"target": "recipient"}, task_id="task",
                         idempotency_key="operation")
        assert calls == [], "已确认结果被重复执行第二遍"
        assert res.evidence.get("replayed"), "恢复结果未标记为 replayed"
        assert res.data == {"restored_artifact": "receipt-output"}
    finally:
        db.close()


if __name__ == "__main__":
    probe("P1 permission_requirement_is_not_a_grant",
          p1_permission_requirement_is_not_a_grant)
    probe("P2 path_prefix_lookalike_not_contained",
          p2_path_prefix_lookalike_not_contained)
    probe("P3 path_traversal_rejected", p3_path_traversal_rejected)
    probe("P4 task_grant_cannot_expand_expert_whitelist",
          p4_task_grant_cannot_expand_expert_whitelist)
    probe("P5 explicit_empty_list_means_no_resource",
          p5_explicit_empty_list_means_no_resource)
    probe("P6 revocation_blocks_new_action", p6_revocation_blocks_new_action)
    probe("P7 unknown_policy_field_rejected", p7_unknown_policy_field_rejected)
    probe("R1 no_adapter_keeps_unknown", r1_no_adapter_keeps_unknown)
    probe("R2 unverified_receipt_keeps_unknown",
          r2_unverified_receipt_keeps_unknown)
    probe("R3 missing_receipt_ref_keeps_unknown",
          r3_missing_receipt_ref_keeps_unknown)
    probe("R4 version_mismatch_keeps_unknown",
          r4_version_mismatch_keeps_unknown)
    probe("R5 verified_receipt_replays_without_reexecution",
          r5_verified_receipt_replays_without_reexecution)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\ntotal={len(RESULTS)} passed={passed} failed={len(RESULTS) - passed}")
    sys.exit(0 if passed == len(RESULTS) else 1)
