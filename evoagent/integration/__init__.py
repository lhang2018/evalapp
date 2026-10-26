"""装配收口与主链可达性（ALN-30-A）。

导出：
- ``Capability`` / ``CAPABILITIES``：能力→入口的可校验声明。
- ``audit_wiring``：审计已交付能力在运行主链上的入口，判定悬空。
- ``assert_wiring_complete``：悬空即报错，不允许静默通过。
"""
from .wiring import (CAPABILITIES, Capability, WiringError, api_routes,
                     assert_wiring_complete, audit_wiring, cli_commands)

__all__ = [
    "CAPABILITIES",
    "Capability",
    "WiringError",
    "api_routes",
    "assert_wiring_complete",
    "audit_wiring",
    "cli_commands",
]
