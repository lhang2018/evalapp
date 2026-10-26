"""能力可达性注册表与审计（ALN-30-A）。

问题背景：2026-10-26 勘察发现 ``evoagent`` 24 个一级域中，``deployment`` /
``recovery`` / ``observability`` / ``knowledge`` 四个域在 ``app.py``、
``cli.py``、``api/`` 三处**均为零引用**——已交付并通过独立复核，但用户在
运行主链上触达不到。同类点名缺口还有 25-A 的 ``ProactiveAssistService``
与 11-P 的 ``IdentityRegistry`` 注入（都停在库层）。

本模块把「能力 → 入口」写成可机器校验的声明：

- 声明了却查不到任何入口 → 判定为**悬空**并报错，不允许静默通过；
- CLI 入口从 ``cli.py`` 源码解析（ast），新增子命令自动被识别，零维护；
- API 入口取 ``api.routes.registered()``，app 入口取 ``App`` 实例属性。

审计只判定「入口是否存在」；「入口跑起来是否正确」由行为测试负责，两者
不可互相替代。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "CAPABILITIES",
    "Capability",
    "WiringError",
    "api_routes",
    "assert_wiring_complete",
    "audit_wiring",
    "cli_commands",
]


class WiringError(RuntimeError):
    """已声明能力在运行主链上没有任何可达入口。"""


@dataclass(frozen=True)
class Capability:
    """一条「能力 → 入口」声明。

    三种入口可组合声明，至少命中一种即视为可达；三种全空或全缺即悬空。
    """

    name: str
    domain: str
    card: str
    app_attr: str = ""
    cli_command: str = ""
    api_route: str = ""
    note: str = ""
    entry_kinds: Tuple[str, ...] = field(default=(), compare=False)

    def declared_entries(self) -> List[Tuple[str, str]]:
        """返回已声明的入口 (kind, name) 列表。"""
        entries: List[Tuple[str, str]] = []
        if self.app_attr:
            entries.append(("app", self.app_attr))
        if self.cli_command:
            entries.append(("cli", self.cli_command))
        if self.api_route:
            entries.append(("api", self.api_route))
        return entries


# 已交付但曾无入口的能力 + 点名待闭合的装配缺口。
# 新增条目必须写清来源卡，避免无主声明。
CAPABILITIES: Tuple[Capability, ...] = (
    Capability(
        name="空机部署与版本固定",
        domain="deployment",
        card="ALN-17-P",
        app_attr="deployment",
        cli_command="host-deploy",
        note="17-P 交付后仅在库层，无任何运行主链入口",
    ),
    Capability(
        name="组合恢复与 RTO/RPO",
        domain="recovery",
        card="ALN-17-P",
        app_attr="recovery",
        cli_command="host-restore",
        note="恢复编排未接 CLI/API，21-A 交付报告已点名",
    ),
    Capability(
        name="凭据治理与泄漏拦截",
        domain="observability",
        card="ALN-29-C",
        app_attr="credentials",
        cli_command="credential-audit",
        note="29-C 交付的凭据登记簿与私钥片段拦截零引用",
    ),
    Capability(
        name="故障经验知识库",
        domain="knowledge",
        card="ALN-27-A",
        app_attr="knowledge",
        cli_command="knowledge-list",
        note="27-A 交付的知识条目与适用性门禁零引用",
    ),
    Capability(
        name="有来源的主动帮助",
        domain="goals",
        card="ALN-25-A",
        app_attr="assist",
        cli_command="assist-organize",
        note="25-A 交付时明确把装配留在保留缺口",
    ),
    Capability(
        name="生产身份登记簿",
        domain="release",
        card="ALN-11-P",
        app_attr="identity_registry",
        cli_command="identity-list",
        note="11-P 交付时未注入 ReleaseDelivery，端到端不生效",
    ),
)


def _default_cli_path() -> Path:
    return Path(__file__).resolve().parents[1] / "cli.py"


def cli_commands(source: Optional[Path] = None) -> FrozenSet[str]:
    """解析 ``cli.py`` 中已注册的全部子命令名。

    用 ast 解析而非手工清单：新增 ``add_parser("x")`` 自动被识别，不会因
    忘记登记而产生假悬空。动态名（循环内 ``add_parser(name)``）不是字面量，
    天然不在静态集合内——它们是工作流动态命令，不参与本审计。
    """
    path = Path(source) if source is not None else _default_cli_path()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_parser":
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.add(arg.value)
    return frozenset(names)


def api_routes() -> FrozenSet[str]:
    """列出 API 已注册路径（GET/POST/PUT 合并）。"""
    from ..api import routes as api_routes_module

    paths: set = set()
    for method in ("GET", "POST", "PUT"):
        paths.update(api_routes_module.registered(method))
    return frozenset(paths)


def audit_wiring(app: Any = None,
                 *,
                 capabilities: Sequence[Capability] = CAPABILITIES,
                 cli_source: Optional[Path] = None) -> Dict[str, Any]:
    """审计已声明能力的运行主链可达性。

    返回 ``{"ok", "total", "wired", "dangling", "capabilities"}``：
    ``dangling`` 列出既无入口声明、又查不到入口的能力。
    """
    commands = cli_commands(cli_source)
    try:
        paths = api_routes()
    except Exception:  # API 层不可导入时不伪造通过
        paths = frozenset()

    wired: List[Dict[str, Any]] = []
    dangling: List[Dict[str, Any]] = []
    details: List[Dict[str, Any]] = []

    for cap in capabilities:
        declared = cap.declared_entries()
        checks: List[Dict[str, Any]] = []
        for kind, target in declared:
            if kind == "app":
                present = app is not None and hasattr(app, target)
            elif kind == "cli":
                present = target in commands
            else:
                present = target in paths
            checks.append({"kind": kind, "name": target, "present": bool(present)})
        reachable = any(c["present"] for c in checks)
        record = {
            "capability": cap.name,
            "domain": cap.domain,
            "card": cap.card,
            "declared": [f"{k}:{n}" for k, n in declared],
            "entries": checks,
            "reachable": reachable,
            "note": cap.note,
        }
        details.append(record)
        if reachable:
            wired.append(record)
        else:
            dangling.append(record)

    return {
        "ok": not dangling,
        "total": len(capabilities),
        "wired": len(wired),
        "dangling": dangling,
        "capabilities": details,
    }


def assert_wiring_complete(app: Any = None,
                           *,
                           capabilities: Sequence[Capability] = CAPABILITIES,
                           cli_source: Optional[Path] = None) -> Dict[str, Any]:
    """审计并在存在悬空能力时抛错——不允许静默通过。"""
    report = audit_wiring(app, capabilities=capabilities, cli_source=cli_source)
    if not report["ok"]:
        names = ", ".join(item["capability"] for item in report["dangling"])
        raise WiringError(
            f"{len(report['dangling'])} 项已声明能力在运行主链上无入口: {names}")
    return report
