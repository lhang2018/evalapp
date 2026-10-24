"""Runtime 适配协议契约测试（ALN-20-A）。

同一组夹具（run_contract_suite）分别在 Mock 与现有运行时适配层上执行：
- 五类能力声明完整且与兼容矩阵一致；
- 未声明能力显式拒绝（CapabilityNotSupportedError），不静默降级；
- 工具/事件/取消/恢复行为契约一致，且适配层透传既有权威；
- 替换 Runtime 不改变权限、副作用账本或 Task 权威（同一性断言）。
"""

import shutil
import tempfile
import unittest

from evoagent.app import App
from evoagent.core.leases import CURRENT_LEASE
from evoagent.core.task import Task, TaskStatus
from evoagent.evolution.sandbox import SandboxRunner
from evoagent.runtime import (
    CapabilityNotSupportedError,
    ExistingRuntimeAdapter,
    MockRuntime,
    RuntimeCapability,
    assert_matches_declarations,
)

ALL_CAPABILITIES = list(RuntimeCapability)


def run_contract_suite(runtime, *, cancellable_hook=None, recovery_hook=None):
    """共享契约夹具：对任意 Runtime 执行同一段断言。

    cancellable_hook(): 返回一个可取消的 task_id；
    recovery_hook(runtime): 执行一次恢复并返回 RecoveryOutcome。
    未提供时对应段退化为声明/未知语义检查。
    """
    results = {}

    # 1. 声明完整：五类能力逐项声明，且与兼容矩阵一致
    decls = {d.capability: d for d in runtime.declarations()}
    assert set(decls) == set(ALL_CAPABILITIES), "五类能力必须逐项声明"
    assert_matches_declarations(runtime, runtime.name)

    # 2. 事件流：发布 → 订阅者收到同事件；按 run_id 可查
    assert decls[RuntimeCapability.EVENT_STREAM].supported
    received = []
    runtime.subscribe_events(received.append)
    event = runtime.emit_event(
        event_type="runtime_probe", task_id="task-x", run_id="run-x",
        payload={"probe": 1})
    evt_id = event["event_id"] if isinstance(event, dict) else event.event_id
    assert any(
        (e.get("event_id") if isinstance(e, dict) else e.event_id) == evt_id
        for e in received), "订阅者必须收到发布的事件"
    assert runtime.query_run_events("run-x"), "事件必须按 run_id 可查"
    results["events"] = True

    # 3. 取消：未知 id → ok=False（非异常）；可取消 id → ok=True 且幂等
    assert decls[RuntimeCapability.CANCEL].supported
    unknown = runtime.cancel_run("task-does-not-exist")
    assert unknown.ok is False and unknown.error
    if cancellable_hook is not None:
        task_id = cancellable_hook()
        first = runtime.cancel_run(task_id)
        assert first.ok, first.error
        second = runtime.cancel_run(task_id)
        assert second.ok or "终态" in (second.error or ""), \
            "重复取消必须幂等或给出终态说明"
    results["cancel"] = True

    # 4. 恢复：接管返回新 run 标识（既有运行时走真实租约语义）
    assert decls[RuntimeCapability.RECOVERY].supported
    if recovery_hook is not None:
        outcome = recovery_hook(runtime)
        assert outcome.ok, outcome.error
        assert outcome.run_id
    results["recovery"] = True

    # 5. 沙箱：声明支持 → 真实隔离执行可用；未声明 → 显式拒绝
    sandbox_decl = decls[RuntimeCapability.SANDBOX]
    if sandbox_decl.supported:
        outcome = runtime.run_sandboxed("print('runtime-probe')", timeout=15)
        assert outcome.ok and "runtime-probe" in (outcome.output or "")
        results["sandbox"] = "executed"
    else:
        try:
            runtime.run_sandboxed("print('nope')")
            raise AssertionError("未声明沙箱能力必须显式拒绝")
        except CapabilityNotSupportedError:
            results["sandbox"] = "rejected"
    return results


class TestRuntimeProtocolContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_rtproto_")
        self.app = App(data_dir=self.tmp)
        self.mock = MockRuntime()
        self.existing = ExistingRuntimeAdapter(
            registry=self.app.tools, bus=self.app.bus,
            controller=self.app.controller, runs=self.app.controller.runs,
            sandbox=SandboxRunner())
        self.mock.register_tool(
            "probe_tool", lambda args: {"output": args.get("x", 0) * 2})
        self.app.tools.register_mock_tool(
            "probe_tool", lambda args: {"output": args.get("x", 0) * 2},
            side_effect_level="none")

    def tearDown(self):
        self.app.db.close()
        self.app.memory.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _abandon_run(self, task):
        """制造失联运行（复用 test_run_ownership 的标准模拟）。"""
        import time as _time
        manager = self.app.controller.runs
        rid, started = manager.begin(task, {})
        token, reset, stop, thread = manager._handles.pop(rid)
        stop.set()
        thread.join(2)
        CURRENT_LEASE.reset(reset)
        manager.db._conn.execute(
            "UPDATE run_leases SET expires_at=0 WHERE task_id=?",
            (task.task_id,))
        # 恢复接管要求可核验的累计预算检查点（ALN-04-R 语义）
        manager.db.save_checkpoint(
            "cp-recovery", task.task_id, _time.time(),
            {"budget_state": {"usage": {"model_calls": 1}}})
        manager.db._conn.commit()
        return rid, started

    # ---- 夹具 ----

    def make_cancellable_task(self):
        task = Task(user_goal="runtime 协议取消探针")
        self.app.db.save_task(task)
        return task.task_id

    def cancellable_hook_mock(self):
        self.mock.register_task("task-mock-1")
        return "task-mock-1"

    def cancellable_hook_existing(self):
        return self.make_cancellable_task()

    def recovery_hook_mock(self, runtime):
        return runtime.recover_run({"task_id": "task-mock-r"}, {})

    def recovery_hook_existing(self, runtime):
        task = Task(user_goal="runtime 协议恢复探针")
        self.app.db.save_task(task)
        self._abandon_run(task)                    # 制造失联运行
        task.task_status = TaskStatus.RUNNING      # 强杀现场
        self.app.db.save_task(task)
        current = self.app.db.get_task(task.task_id)
        outcome = runtime.recover_run(current, {"note": "contract"})
        if outcome.ok:
            try:
                self.app.controller.runs.finish(
                    current, outcome.run_id, outcome.started_at)
            except Exception:
                pass  # 心跳清理失败不影响契约断言
        return outcome

    # ---- 共享契约夹具：同一套断言跑两个实现 ----

    def test_contract_suite_on_mock(self):
        out = run_contract_suite(
            self.mock, cancellable_hook=self.cancellable_hook_mock,
            recovery_hook=self.recovery_hook_mock)
        self.assertEqual(out["sandbox"], "rejected",
                         "Mock 沙箱默认拒绝：不做假执行")

    def test_contract_suite_on_existing(self):
        out = run_contract_suite(
            self.existing,
            cancellable_hook=self.cancellable_hook_existing,
            recovery_hook=self.recovery_hook_existing)
        self.assertEqual(out["sandbox"], "executed",
                         "现有运行时沙箱真实隔离执行")

    # ---- 声明与矩阵一致性 ----

    def test_declarations_complete_and_matrix_consistent(self):
        for runtime, impl in ((self.mock, "mock"),
                              (self.existing, "existing")):
            decls = {d.capability for d in runtime.declarations()}
            self.assertEqual(decls, set(ALL_CAPABILITIES))
            assert_matches_declarations(runtime, impl)

    # ---- 拒绝语义：能力不足显式拒绝，不静默降级 ----

    def test_mock_sandbox_rejected_not_degraded(self):
        with self.assertRaises(CapabilityNotSupportedError) as ctx:
            self.mock.run_sandboxed("print(1)")
        self.assertIn(RuntimeCapability.SANDBOX.value,
                      str(ctx.exception.capability.value))

    def test_minimal_existing_rejects_missing_backing(self):
        minimal = ExistingRuntimeAdapter(registry=self.app.tools)
        with self.assertRaises(CapabilityNotSupportedError):
            minimal.cancel_run("task-1")
        with self.assertRaises(CapabilityNotSupportedError):
            minimal.recover_run({"task_id": "task-1"}, {})
        with self.assertRaises(CapabilityNotSupportedError):
            minimal.run_sandboxed("print(1)")
        # 注入了权威的能力照常可用：声明与行为一致
        partial = ExistingRuntimeAdapter(registry=self.app.tools,
                                         bus=self.app.bus)
        events = []
        partial.subscribe_events(events.append)
        evt = partial.emit_event(event_type="probe", run_id="run-min")
        self.assertTrue(any(e.event_id == evt.event_id for e in events))
        with self.assertRaises(CapabilityNotSupportedError):
            partial.cancel_run("task-1")

    # ---- 权威不变性：透传而非复制 ----

    def test_tool_invocation_passthrough_authority(self):
        self.assertIs(self.existing.registry, self.app.tools,
                      "适配层必须持有注入的注册表权威，不得复制")
        result = self.existing.invoke_tool(
            "probe_tool", {"x": 2}, task_id="task-p", run_id="run-p")
        self.assertTrue(result.ok, result)
        self.assertEqual(result.data, {"output": 4})
        # 未注册工具：权威原样返回 ok=False，适配层不吞不改
        miss = self.existing.invoke_tool("no_such_tool", {},
                                         task_id="task-p", run_id="run-p")
        self.assertFalse(miss.ok)

    def test_authority_objects_are_injected_instances(self):
        self.assertIs(self.existing.bus, self.app.bus)
        self.assertIs(self.existing.controller, self.app.controller)
        self.assertIs(self.existing.runs, self.app.controller.runs)

    def test_cancel_and_recovery_via_authority(self):
        task_id = self.make_cancellable_task()
        first = self.existing.cancel_run(task_id)
        self.assertTrue(first.ok, first.error)
        again = self.existing.cancel_run(task_id)
        self.assertFalse(again.ok, "终态任务重复取消返回 ok=False")
        outcome = self.recovery_hook_existing(self.existing)
        self.assertTrue(outcome.ok)
        self.assertTrue(str(outcome.run_id).startswith("run_"))


if __name__ == "__main__":
    unittest.main()
