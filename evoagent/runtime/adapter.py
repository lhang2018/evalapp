"""现有运行时适配层：对既有权威组件的透传代理（ALN-20-A）。

注入组件（registry/bus/controller/runs/sandbox）即权威：本适配层
不复制、不包装语义，能力方法直接委托：
- 工具调用 → ToolRegistry.execute（权限/预算/幂等/副作用账本语义不变）
- 事件流   → EventBus（subscribe/emit/query_run）
- 取消     → controller.cancel_task（协作式、终态/未知返回 ok=False）
- 恢复     → RunManager.begin(recovery=True)（ALN-04-R 中断接管）
- 沙箱     → evolution SandboxRunner.run_code

能力声明由注入组件是否存在决定：缺组件 = 缺能力 = 显式拒绝。
替换 Runtime 不改变权限、副作用账本或 Task 权威——因为本层没有自己的。
"""

from typing import Any, Callable, Dict, List, Optional

from .protocol import (
    CapabilityDeclaration,
    CancelOutcome,
    RecoveryOutcome,
    RuntimeCapability,
    RuntimeProtocol,
    SandboxOutcome,
)


class ExistingRuntimeAdapter(RuntimeProtocol):
    """把既有 controller/react 执行面适配为 Runtime 协议的透传层。"""

    def __init__(self, *, registry: Any = None, bus: Any = None,
                 controller: Any = None, runs: Any = None,
                 sandbox: Any = None, name: str = "existing"):
        self.name = name
        self.registry = registry
        self.bus = bus
        self.controller = controller
        self.runs = runs
        self.sandbox = sandbox

    # ---- 能力声明：有权威组件才声明支持 ----

    def declarations(self) -> List[CapabilityDeclaration]:
        def decl(cap: RuntimeCapability, backing: Any, details: Dict[str, Any]
                 ) -> CapabilityDeclaration:
            methods = {
                RuntimeCapability.TOOL_INVOCATION: ("execute",),
                RuntimeCapability.EVENT_STREAM: ("subscribe", "emit", "query_run"),
                RuntimeCapability.CANCEL: ("cancel_task",),
                RuntimeCapability.RECOVERY: ("begin",),
                RuntimeCapability.SANDBOX: ("run_code",),
            }[cap]
            supported = backing is not None and all(callable(getattr(backing, name, None)) for name in methods)
            detail = dict(details)
            detail["backing"] = type(backing).__name__ if backing is not None \
                else None
            if not supported:
                detail["reason"] = (
                    f"未注入 {detail.get('authority', cap.value)} 权威组件")
            return CapabilityDeclaration(cap, supported, detail)

        return [
            decl(RuntimeCapability.TOOL_INVOCATION, self.registry, {
                "authority": "ToolRegistry",
                "note": "委托 registry.execute；权限/预算/账本语义原样",
            }),
            decl(RuntimeCapability.EVENT_STREAM, self.bus, {
                "authority": "EventBus",
                "note": "subscribe/emit/query_run 直通",
            }),
            decl(RuntimeCapability.CANCEL, self.controller, {
                "authority": "Controller.cancel_task",
                "note": "协作式取消；未知/终态返回 ok=False",
            }),
            decl(RuntimeCapability.RECOVERY, self.runs, {
                "authority": "RunManager.begin(recovery=True)",
                "note": "RUNNING 中断接管（ALN-04-R）",
            }),
            decl(RuntimeCapability.SANDBOX, self.sandbox, {
                "authority": "evolution SandboxRunner",
                "note": "一次性目录、无网络/子进程、超时强杀",
            }),
        ]

    # ---- 五类能力：委托权威组件 ----

    def _invoke_tool(self, tool_id: str, args: Dict[str, Any],
                     **passthrough: Any) -> Any:
        return self.registry.execute(tool_id, args, **passthrough)

    def _subscribe_events(self, fn: Callable[[Any], None]) -> None:
        self.bus.subscribe(fn)

    def _emit_event(self, **fields: Any) -> Any:
        return self.bus.emit(**fields)

    def _query_run_events(self, run_id: str, limit: int) -> List[Any]:
        return self.bus.query_run(run_id, limit=limit)

    def _cancel_run(self, task_id: str) -> CancelOutcome:
        raw = self.controller.cancel_task(task_id)
        return CancelOutcome(
            ok=bool(raw.get("ok")),
            task_id=task_id,
            status=raw.get("status"),
            error=raw.get("error"))

    def _recover_run(self, task: Any,
                     manifest: Optional[Dict[str, Any]]) -> RecoveryOutcome:
        run_id, started = self.runs.begin(
            task, manifest or {}, recovery=True)
        return RecoveryOutcome(ok=True, run_id=run_id, started_at=started)

    def _run_sandboxed(self, code: str,
                       timeout: Optional[float]) -> SandboxOutcome:
        result = self.sandbox.run_code(
            code, **({"timeout": timeout} if timeout is not None else {}))
        status = getattr(result, "status", "failed")
        return SandboxOutcome(
            ok=status == "passed",
            output=getattr(result, "stdout", None),
            error=getattr(result, "stderr", None) or None,
            violation=bool(getattr(result, "violations", None)))
