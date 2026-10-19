"""内置工具集。

- current_time / calculate / http_get / fs_list / file_read：低风险只读。
- file_write：有外部副作用（PRD 70），需高风险确认 + 幂等键。
"""

import datetime
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Dict

from ..tools.contract import ToolContract, ToolResult, class_error, validate_against_schema


def current_time_tool() -> ToolContract:
    def run(args: Dict) -> ToolResult:
        now = datetime.datetime.now()
        return ToolResult(ok=True, data=now.strftime("%Y-%m-%d %H:%M:%S"),
                          submitted=True, executed=True, contract_valid=True,
                          goal_reached=True, independently_verified=True,
                          evidence={"source": "local_clock"})
    return ToolContract(
        tool_id="current_time", description="获取当前本地时间",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        side_effect_level="none", idempotency_policy="none", timeout_policy=2.0,
    ), run


def calculate_tool() -> ToolContract:
    def run(args: Dict) -> ToolResult:
        expr = str(args.get("expression", "")).strip()
        if not re.fullmatch(r"[\d\s().+\-*/%]+", expr):
            return ToolResult.error_result("非法表达式", "invalid_arguments")
        try:
            val = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 白名单正则约束
        except Exception as e:  # noqa: BLE001
            return ToolResult.error_result(f"计算失败: {e}", "tool_defect")
        return ToolResult(ok=True, data=val, submitted=True, executed=True,
                          contract_valid=True, goal_reached=True,
                          independently_verified=True,
                          evidence={"expression": expr})
    return ToolContract(
        tool_id="calculate", description="四则运算计算器",
        input_schema={"type": "object", "required": ["expression"],
                      "properties": {"expression": {"type": "string"}}},
        output_schema={"type": "number"},
        side_effect_level="none", idempotency_policy="none",
    ), run


def http_get_tool(timeout: float = 15.0, allowed_hosts: list | None = None) -> ToolContract:
    """域名白名单默认放开；生产环境建议配置 allowed_hosts。"""
    def run(args: Dict) -> ToolResult:
        url = args.get("url", "")
        if not re.match(r"^https?://", url):
            return ToolResult.error_result("URL 非法", "invalid_arguments")
        host = re.match(r"^https?://([^/]+)/?", url)
        if allowed_hosts and host and host.group(1) not in allowed_hosts:
            return ToolResult.error_result("目标域名不在白名单", "auth_or_permission")
        try:
            import requests
            r = requests.get(url, timeout=timeout)
            ok = r.status_code < 400
            return ToolResult(ok=ok, data=r.text[:2000],
                              submitted=True, executed=True,
                              contract_valid=True,
                              goal_reached=ok,
                              independently_verified=True,
                              error=None if ok else f"HTTP {r.status_code}",
                              error_class=None if ok else "network_or_unavailable",
                              evidence={"status_code": r.status_code})
        except Exception as e:  # noqa: BLE001
            return ToolResult.error_result(str(e), class_error(e))
    return ToolContract(
        tool_id="http_get", description="HTTP GET 请求",
        input_schema={"type": "object", "required": ["url"],
                      "properties": {"url": {"type": "string"}}},
        output_schema={"type": "string"},
        side_effect_level="none", idempotency_policy="none",
        timeout_policy=timeout,
    ), run


def fs_list_tool(base: str = ".") -> ToolContract:
    def run(args: Dict) -> ToolResult:
        d = args.get("dir", ".")
        p = Path(d)
        if not p.exists():
            return ToolResult.error_result(f"目录不存在: {d}", "invalid_arguments")
        entries = [f"{x.name}{'/' if x.is_dir() else ''}" for x in sorted(p.iterdir())[:100]]
        return ToolResult(ok=True, data=entries, submitted=True, executed=True,
                          contract_valid=True, goal_reached=True,
                          independently_verified=True)
    return ToolContract(
        tool_id="fs_list", description="列出目录内容",
        input_schema={"type": "object"},
        side_effect_level="none", idempotency_policy="none",
    ), run


def file_read_tool() -> ToolContract:
    def run(args: Dict) -> ToolResult:
        path = args.get("path", "")
        p = Path(path)
        if not p.exists():
            return ToolResult.error_result(f"文件不存在: {path}", "invalid_arguments")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:5000]
        except Exception as e:  # noqa: BLE001
            return ToolResult.error_result(str(e), class_error(e))
        return ToolResult(ok=True, data=text, submitted=True, executed=True,
                          contract_valid=True, goal_reached=True,
                          independently_verified=True)
    return ToolContract(
        tool_id="file_read", description="读取文本文件（截断5KB）",
        input_schema={"type": "object", "required": ["path"],
                      "properties": {"path": {"type": "string"}}},
        side_effect_level="none", idempotency_policy="none",
    ), run


def file_write_tool(workdir: str = ".") -> ToolContract:
    """写文件 = 有本地副作用 + 不可逆风险 → 高风险确认（PRD 70）。"""
    def run(args: Dict) -> ToolResult:
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return ToolResult.error_result("缺少 path", "invalid_arguments")
        if ".." in path or path.startswith(("/", "\\")):
            return ToolResult.error_result("路径越界", "auth_or_permission")
        p = Path(workdir) / path
        try:
            existed = p.exists()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return ToolResult.error_result(str(e), class_error(e))
        # 独立验证：重新读取比对（PRD 4.1 不只信报告）
        verified = p.read_text(encoding="utf-8") == content
        digest = hashlib.sha256(content.encode()).hexdigest()[:16]
        return ToolResult(ok=verified, data=str(p),
                          submitted=True, executed=True,
                          contract_valid=True, goal_reached=verified,
                          independently_verified=verified,
                          side_effect_uncertain=False,
                          evidence={"sha256": digest, "overwrote": existed})
    return ToolContract(
        tool_id="file_write", description="写入文本文件",
        input_schema={"type": "object", "required": ["path", "content"],
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}}},
        side_effect_level="local", idempotency_policy="key",
    ), run


BUILTIN_FACTORIES = {
    "current_time": current_time_tool,
    "calculate": calculate_tool,
    "http_get": http_get_tool,
    "fs_list": fs_list_tool,
    "file_read": file_read_tool,
    "file_write": file_write_tool,
}
