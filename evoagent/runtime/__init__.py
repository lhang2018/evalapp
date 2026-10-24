"""Runtime 适配协议（ALN-20-A，ADR-010：模型适配不等于 Runtime）。

纯契约包：定义工具调用、事件流、取消、恢复、沙箱边界五类能力的
声明/错误/拒绝语义，以及 Mock 与现有运行时两路适配。本包不接管执行，
不引入第二条工具/账本/Task 权威——现有运行时适配层只做注入组件的
透传代理，权限、副作用账本与 Task 权威语义原样保留。

固定外部 Runtime commit 的实际 PoC 与 ADR 正式条目另卡（ALN-20 整包）。
"""

from .protocol import (
    CapabilityDeclaration,
    CapabilityNotSupportedError,
    RuntimeCapability,
    RuntimeProtocol,
)
from .mock import MockRuntime
from .adapter import ExistingRuntimeAdapter
from .matrix import (
    ADOPTION_CRITERIA,
    SUPPORT_LEVELS,
    assert_matches_declarations,
    matrix_for,
)

__all__ = [
    "CapabilityDeclaration",
    "CapabilityNotSupportedError",
    "RuntimeCapability",
    "RuntimeProtocol",
    "MockRuntime",
    "ExistingRuntimeAdapter",
    "ADOPTION_CRITERIA",
    "SUPPORT_LEVELS",
    "assert_matches_declarations",
    "matrix_for",
]
