"""Runtime 协议核心：五类能力、能力声明与拒绝语义（ALN-20-A）。

设计约束（ADR-010）：
- 执行权威留在 controller/react/workflows；本协议只描述 Runtime 需要
  提供的能力边界，不定义第二条编排。
- 能力不足必须显式拒绝（CapabilityNotSupportedError），禁止静默降级为
  纯模型 API 适配——模型适配解决不了取消/恢复/沙箱/账本语义。
- 领域状态用结果对象表达（ok/error 字段），不靠异常流；异常只用于
  能力缺失与权威透传的错误。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class RuntimeCapability(str, Enum):
    """Runtime 五类能力（任务卡验收第1条）。"""

    TOOL_INVOCATION = "tool_invocation"   # 工具调用：绑定既有注册表权威
    EVENT_STREAM = "event_stream"         # 事件流：发布/订阅/按运行查询
    CANCEL = "cancel"                     # 取消：协作式、幂等
    RECOVERY = "recovery"                 # 恢复：RUNNING 中断接管（ALN-04-R 语义）
    SANDBOX = "sandbox"                   # 沙箱边界：候选代码隔离执行


class CapabilityNotSupportedError(RuntimeError):
    """能力缺失时的显式拒绝。

    这是协议的拒绝语义核心：调用未声明能力时抛出本异常，
    不允许适配层伪造结果或静默退化为模型 API 调用。
    """

    def __init__(self, capability: RuntimeCapability, runtime_name: str,
                 reason: str = ""):
        self.capability = capability
        self.runtime_name = runtime_name
        self.reason = reason or "该 Runtime 未声明此能力"
        super().__init__(
            f"Runtime[{runtime_name}] 不支持能力 "
            f"{capability.value}: {self.reason}（显式拒绝，不静默降级）")


@dataclass
class CapabilityDeclaration:
    """单类能力的声明：是否支持、支撑组件与边界说明。"""

    capability: RuntimeCapability
    supported: bool
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.capability, RuntimeCapability):
            raise ValueError("unknown runtime capability")
        if not isinstance(self.supported, bool) or not isinstance(self.details, dict):
            raise ValueError("capability supported must be boolean and details an object")


@dataclass
class CancelOutcome:
    """取消结果（与 controller.cancel_task 返回同构：ok/error）。"""

    ok: bool
    task_id: str
    status: Optional[str] = None
    error: Optional[str] = None


@dataclass
class RecoveryOutcome:
    """恢复结果：接管成功返回新 run_id 与开始时间。"""

    ok: bool
    run_id: Optional[str] = None
    started_at: Optional[float] = None
    error: Optional[str] = None


@dataclass
class SandboxOutcome:
    """沙箱结果：不执行代码的 Runtime 必须显式拒绝而非返回假结果。"""

    ok: bool
    output: Optional[str] = None
    error: Optional[str] = None
    violation: bool = False


class RuntimeProtocol(ABC):
    """Runtime 适配协议。

    子类必须实现 `declarations()`；五类能力方法的第一行都必须是
    ``self.require(...)``——未声明能力一律 CapabilityNotSupportedError。
    """

    name: str = "runtime"

    @abstractmethod
    def declarations(self) -> List[CapabilityDeclaration]:
        """声明本 Runtime 的能力矩阵（五类能力逐项给出）。"""

    def declaration_of(self, capability: RuntimeCapability
                       ) -> Optional[CapabilityDeclaration]:
        declarations = self.declarations()
        if len({d.capability for d in declarations}) != len(declarations):
            raise ValueError("duplicate runtime capability declarations")
        for item in declarations:
            if item.capability == capability:
                return item
        return None

    def require(self, capability: RuntimeCapability) -> CapabilityDeclaration:
        """取能力声明；缺失或未支持时显式拒绝。"""
        item = self.declaration_of(capability)
        if item is None:
            raise CapabilityNotSupportedError(
                capability, self.name, "协议未声明该能力")
        if item.supported is not True:
            raise CapabilityNotSupportedError(
                capability, self.name,
                str(item.details.get("reason", "")))
        return item

    # ---- 五类能力（契约入口；实现必须先 require 再执行）----

    def invoke_tool(self, tool_id: str, args: Dict[str, Any],
                    **passthrough: Any) -> Any:
        """工具调用。权威（注册表/权限/预算/副作用账本）原样透传。"""
        self.require(RuntimeCapability.TOOL_INVOCATION)
        return self._invoke_tool(tool_id, args, **passthrough)

    def subscribe_events(self, fn: Callable[[Any], None]) -> None:
        """订阅事件流；订阅者异常不得破坏主流程（既有 EventBus 语义）。"""
        self.require(RuntimeCapability.EVENT_STREAM)
        self._subscribe_events(fn)

    def emit_event(self, **fields: Any) -> Any:
        """发布事件。字段以各实现的事件结构为准，契约要求按 run_id 可查。"""
        self.require(RuntimeCapability.EVENT_STREAM)
        return self._emit_event(**fields)

    def query_run_events(self, run_id: str, limit: int = 200
                         ) -> List[Any]:
        """按 run_id 查询事件（对账与恢复的输入）。"""
        self.require(RuntimeCapability.EVENT_STREAM)
        return self._query_run_events(run_id, limit)

    def cancel_run(self, task_id: str) -> CancelOutcome:
        """取消任务运行：协作式、幂等；未知/终态返回 ok=False 而非异常。"""
        self.require(RuntimeCapability.CANCEL)
        return self._cancel_run(task_id)

    def recover_run(self, task: Any, manifest: Optional[Dict[str, Any]] = None
                    ) -> RecoveryOutcome:
        """恢复：对已中断运行做接管（ALN-04-R 语义），返回新 run_id。"""
        self.require(RuntimeCapability.RECOVERY)
        return self._recover_run(task, manifest)

    def run_sandboxed(self, code: str, timeout: Optional[float] = None
                      ) -> SandboxOutcome:
        """候选代码隔离执行：一次性目录、无网络/子进程、超时强杀。"""
        self.require(RuntimeCapability.SANDBOX)
        return self._run_sandboxed(code, timeout)

    # ---- 实现端 ----

    @abstractmethod
    def _invoke_tool(self, tool_id: str, args: Dict[str, Any],
                     **passthrough: Any) -> Any: ...

    @abstractmethod
    def _subscribe_events(self, fn: Callable[[Any], None]) -> None: ...

    @abstractmethod
    def _emit_event(self, **fields: Any) -> Any: ...

    @abstractmethod
    def _query_run_events(self, run_id: str, limit: int) -> List[Any]: ...

    @abstractmethod
    def _cancel_run(self, task_id: str) -> CancelOutcome: ...

    @abstractmethod
    def _recover_run(self, task: Any,
                     manifest: Optional[Dict[str, Any]]) -> RecoveryOutcome: ...

    @abstractmethod
    def _run_sandboxed(self, code: str,
                       timeout: Optional[float]) -> SandboxOutcome: ...
