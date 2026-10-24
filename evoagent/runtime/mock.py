"""Mock Runtime：协议契约的内存实现（ALN-20-A）。

与现有运行时适配层通过同一组契约夹具。边界：
- 工具/事件/取消/恢复为纯内存语义，形状与现有实现对齐；
- 沙箱是纯契约假件——**从不执行代码**，只记录调用并返回
  ok=False（未实现真实隔离），真实隔离以 evolution SandboxRunner 为准；
  任何"Mock 沙箱执行成功"的观察都是夹具错误。
"""

import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .protocol import (
    CapabilityDeclaration,
    CancelOutcome,
    RecoveryOutcome,
    RuntimeCapability,
    RuntimeProtocol,
    SandboxOutcome,
)


class MockRuntime(RuntimeProtocol):
    name = "mock"

    def __init__(self, *, sandbox_capable: bool = False):
        self._lock = threading.RLock()
        self._tools: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self._subscribers: List[Callable[[Any], None]] = []
        self._events: List[Any] = []
        self._tasks: Dict[str, Optional[str]] = {}
        self._recoveries: List[Dict[str, Any]] = []
        self._sandbox_capable = sandbox_capable

    # ---- 夹具注入 ----

    def register_tool(self, tool_id: str,
                      fn: Callable[[Dict[str, Any]], Any]) -> None:
        with self._lock:
            self._tools[tool_id] = fn

    def register_task(self, task_id: str) -> None:
        """登记已知任务（对应真实运行时的 Task 库）。"""
        with self._lock:
            self._tasks[task_id] = None

    def _invoke_tool(self, tool_id: str, args: Dict[str, Any],
                     **passthrough: Any) -> Any:
        with self._lock:
            fn = self._tools.get(tool_id)
        if fn is None:
            raise LookupError(f"工具未注册: {tool_id}")
        return fn(args)

    # ---- 事件流 ----

    def _subscribe_events(self, fn: Callable[[Any], None]) -> None:
        with self._lock:
            self._subscribers.append(fn)

    def _emit_event(self, **fields: Any) -> Dict[str, Any]:
        event = {"event_id": uuid.uuid4().hex,
                 "timestamp": time.time(), **fields}
        with self._lock:
            self._events.append(event)
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(event)
            except Exception:
                pass
        return event

    def _query_run_events(self, run_id: str, limit: int) -> List[Any]:
        with self._lock:
            events = [e for e in self._events if e.get("run_id") == run_id]
        return events[:limit] if limit >= 0 else events

    # ---- 能力声明 ----

    def declarations(self) -> List[CapabilityDeclaration]:
        return [
            CapabilityDeclaration(
                RuntimeCapability.TOOL_INVOCATION, True,
                {"note": "内存工具表；契约夹具注入探针工具"}),
            CapabilityDeclaration(
                RuntimeCapability.EVENT_STREAM, True,
                {"note": "内存事件表，按 run_id 可查"}),
            CapabilityDeclaration(
                RuntimeCapability.CANCEL, True,
                {"note": "已知任务幂等取消；未知任务 ok=False，与现有权威同构"}),
            CapabilityDeclaration(
                RuntimeCapability.RECOVERY, True,
                {"note": "内存恢复台账，返回新 run_id"}),
            CapabilityDeclaration(
                RuntimeCapability.SANDBOX, False,
                {"reason": "Mock 不做真实隔离；沙箱能力拒绝，避免假执行证据",
                 "note": "legacy sandbox_capable flag cannot enable real isolation"}),
        ]

    # ---- 取消 ----

    def _cancel_run(self, task_id: str) -> CancelOutcome:
        with self._lock:
            if task_id not in self._tasks:
                return CancelOutcome(
                    ok=False, task_id=task_id, error="任务不存在")
            previous = self._tasks[task_id]
            self._tasks[task_id] = previous or "CANCELLED"
        return CancelOutcome(ok=True, task_id=task_id, status="CANCELLED")

    # ---- 恢复 ----

    def _recover_run(self, task: Any,
                     manifest: Optional[Dict[str, Any]]) -> RecoveryOutcome:
        task_id = getattr(task, "task_id", None) or (
            task.get("task_id") if isinstance(task, dict) else None)
        if not task_id:
            return RecoveryOutcome(ok=False, error="缺少 task_id")
        run_id = "run_" + uuid.uuid4().hex
        started = time.time()
        with self._lock:
            self._recoveries.append({
                "task_id": task_id, "run_id": run_id,
                "manifest": manifest or {}, "at": started})
        return RecoveryOutcome(ok=True, run_id=run_id, started_at=started)

    # ---- 沙箱（纯契约假件，从不执行代码）----

    def _run_sandboxed(self, code: str,
                       timeout: Optional[float]) -> SandboxOutcome:
        return SandboxOutcome(
            ok=False,
            error="Mock Runtime 不执行候选代码；真实隔离执行以 "
                  "evolution SandboxRunner 为准（此结果不代表代码已运行）")
