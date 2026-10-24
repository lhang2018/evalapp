"""Runtime 能力 × 实现兼容矩阵与采用判据（ALN-20-A）。

矩阵是交付物的一部分：能力 × 实现的支持级别 + 判据。测试用
`assert_matches_declarations` 保证矩阵与各实现运行时声明一致，
避免文档与代码漂移。

支持级别：
- full：能力由权威组件承载，语义原样透传；
- absent：未实现该能力，调用即 CapabilityNotSupportedError 显式拒绝；
- partial：有部分语义（说明见 notes）。
"""

from typing import Dict, List

from .protocol import RuntimeCapability, RuntimeProtocol

SUPPORT_LEVELS = ("full", "partial", "absent")

FULL = "full"
PARTIAL = "partial"
ABSENT = "absent"

# 能力 × 实现矩阵。
# "model_api_only" 是参照行：只有模型对话能力的适配（ADR-010 禁止把它
# 冒充 Runtime）——列出它会被拒绝的能力，作为"不静默降级"的对照。
MATRIX: Dict[str, Dict[RuntimeCapability, Dict[str, str]]] = {
    "mock": {
        RuntimeCapability.TOOL_INVOCATION: {
            "level": FULL, "notes": "内存工具表，契约夹具注入"},
        RuntimeCapability.EVENT_STREAM: {
            "level": FULL, "notes": "内存事件表，按 run_id 可查"},
        RuntimeCapability.CANCEL: {
            "level": FULL, "notes": "内存取消标记，幂等"},
        RuntimeCapability.RECOVERY: {
            "level": FULL, "notes": "内存恢复台账，返回新 run_id"},
        RuntimeCapability.SANDBOX: {
            "level": ABSENT,
            "notes": "Mock 不做真实隔离，从不执行候选代码（避免假证据）"},
    },
    "existing": {
        RuntimeCapability.TOOL_INVOCATION: {
            "level": FULL,
            "notes": "ToolRegistry.execute：权限/预算/幂等/副作用账本语义不变"},
        RuntimeCapability.EVENT_STREAM: {
            "level": FULL, "notes": "EventBus：subscribe/emit/query_run 直通"},
        RuntimeCapability.CANCEL: {
            "level": FULL,
            "notes": "controller.cancel_task：协作式、幂等、终态拒绝"},
        RuntimeCapability.RECOVERY: {
            "level": FULL,
            "notes": "RunManager.begin(recovery=True)：RUNNING 中断接管"},
        RuntimeCapability.SANDBOX: {
            "level": FULL,
            "notes": "evolution SandboxRunner：一次性目录/无网络/超时强杀"},
    },
    "model_api_only": {
        RuntimeCapability.TOOL_INVOCATION: {
            "level": ABSENT,
            "notes": "只有模型函数调用，无工具注册表/预算/账本权威"},
        RuntimeCapability.EVENT_STREAM: {
            "level": PARTIAL, "notes": "仅会话内消息记录，无 run 级事件账本"},
        RuntimeCapability.CANCEL: {
            "level": ABSENT, "notes": "无法协作取消已下发动作"},
        RuntimeCapability.RECOVERY: {
            "level": ABSENT, "notes": "无运行租约/清单，无法接管中断"},
        RuntimeCapability.SANDBOX: {
            "level": ABSENT, "notes": "无候选代码隔离执行"},
    },
}

# 采用/不采用判据：固定外部 Runtime commit 的 PoC 另卡按此裁决。
ADOPTION_CRITERIA: List[Dict[str, str]] = [
    {
        "id": "AC-1",
        "criterion": "五类能力全部 full 或有显式补位方案",
        "adopt_if": "缺失能力可用既有权威组件补位且语义等价（如外部 "
                    "Runtime 只缺沙箱，可注入 evolution SandboxRunner）",
        "reject_if": "核心能力（工具/账本/恢复）只能用模型 API 语义模拟",
    },
    {
        "id": "AC-2",
        "criterion": "权限、副作用账本与 Task 权威不被复制",
        "adopt_if": "外部 Runtime 接受注入既有 ToolRegistry/EventBus/"
                    "Controller 权威并按本协议透传",
        "reject_if": "外部 Runtime 自带第二套工具账本或 Task 状态机，"
                     "且无法关闭",
    },
    {
        "id": "AC-3",
        "criterion": "取消/恢复/沙箱语义可验证",
        "adopt_if": "契约夹具（tests/test_runtime_protocol.py 共享套件）"
                    "在外部 Runtime 上全部通过",
        "reject_if": "任一声明能力在夹具中行为与声明不符",
    },
    {
        "id": "AC-4",
        "criterion": "固定版本可复现",
        "adopt_if": "PoC 绑定外部 Runtime 的固定 commit 与摘要，"
                    "同 commit 重跑结论一致",
        "reject_if": "只能跟随浮动版本，无法固定证据链",
    },
]


def matrix_for(implementation: str
               ) -> Dict[RuntimeCapability, Dict[str, str]]:
    if implementation not in MATRIX:
        raise KeyError(f"未知实现: {implementation}")
    return MATRIX[implementation]


def assert_matches_declarations(runtime: RuntimeProtocol,
                                implementation: str) -> None:
    """矩阵行必须与运行时实际声明一致（测试与文档不漂移）。"""
    row = matrix_for(implementation)
    declarations = runtime.declarations()
    declared = {item.capability: item for item in declarations}
    if len(declared) != len(declarations):
        raise AssertionError("duplicate runtime capability declarations")
    if set(declared) != set(row):
        raise AssertionError(
            f"{implementation} 矩阵能力集与声明不一致: "
            f"矩阵={sorted(c.value for c in row)} "
            f"声明={sorted(c.value for c in declared)}")
    for capability, entry in row.items():
        item = declared[capability]
        if not isinstance(item.supported, bool):
            raise AssertionError("capability support must be boolean")
        if entry["level"] == ABSENT and item.supported:
            raise AssertionError(
                f"{implementation}.{capability.value}: 矩阵=absent 但声明支持")
        if entry["level"] in (FULL, PARTIAL) and not item.supported:
            raise AssertionError(
                f"{implementation}.{capability.value}: 矩阵="
                f"{entry['level']} 但声明不支持")
