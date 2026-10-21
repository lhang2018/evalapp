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
        # 真探针（F-04 死字段修复①）：时钟 tick 一次且非空
        health_check=lambda: bool(
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
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
        # 真探针：计算引擎自检（1+1 必须等于 2）
        health_check=lambda: eval("1+1", {"__builtins__": {}}, {}) == 2
        # noqa: S307 与 run 同一白名单口径
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
        # 真探针：网络类工具**不做廉价探测**（会打外网/拖时延）——
        # health() 返回 None=unknown，不再被"恒真"伪装成全绿（F-04①）
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
        # 真探针：基目录可列（closure 捕获 base）
        health_check=lambda: Path(base or ".").is_dir(),
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
        # 真探针：工作区可读
        health_check=lambda: os.access(".", os.R_OK),
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
        # 真探针：workdir 真实可写（建+删一个临时文件）
        health_check=lambda: _probe_writable(workdir),
    ), run


_GIT_ACTIONS = {"log", "show", "status", "diff", "blame", "branch",
                "rev-parse"}
_SAFE_REF = re.compile(r"^[A-Za-z0-9_./~^@{}-]+$")


def git_read_tool(workdir: str = ".") -> ToolContract:
    """只读 Git 适配器（Architecture 17）：固定子命令，禁止写操作与 shell。

    参数经白名单与正则约束，避免选项注入；不访问网络。
    """
    import subprocess

    def run(args: Dict) -> ToolResult:
        action = str(args.get("action") or "")
        if action not in _GIT_ACTIONS:
            return ToolResult.error_result(
                f"不支持的动作: {action}（只读: {', '.join(sorted(_GIT_ACTIONS))}）",
                "auth_or_permission")
        ref = str(args.get("ref") or "")
        path = str(args.get("path") or "")
        if ref and (ref.startswith("-") or not _SAFE_REF.match(ref)):
            return ToolResult.error_result("非法 ref", "invalid_arguments")
        if path and (".." in path or path.startswith(("-", "/", "\\"))):
            return ToolResult.error_result("非法 path", "invalid_arguments")
        try:
            limit = max(1, min(200, int(args.get("limit") or 20)))
        except (TypeError, ValueError):
            limit = 20

        cmd = ["git", "-C", str(workdir), action]
        if action == "log":
            cmd += ["--oneline", "-n", str(limit)]
            if ref:
                cmd.append(ref)
        elif action == "status":
            cmd.append("--short")
        elif action in ("show", "blame"):
            cmd.append(ref or "HEAD")
        elif action == "rev-parse":
            cmd.append(ref or "HEAD")
        elif action == "diff" and ref:
            cmd.append(ref)
        if path and action in ("log", "diff", "blame", "show"):
            cmd += ["--", path]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            return ToolResult.error_result(str(e), class_error(e))
        out = (proc.stdout or "")[:4000]
        err = (proc.stderr or "").strip()[:500]
        ok = proc.returncode == 0
        return ToolResult(
            ok=ok, data=out, error=None if ok else (err or "git failed"),
            error_class=None if ok else "tool_defect_or_env",
            submitted=True, executed=True, contract_valid=True,
            goal_reached=ok, independently_verified=ok,
            evidence={"action": action, "returncode": proc.returncode})
    return ToolContract(
        tool_id="git_read", description="只读 Git 查询（log/show/status/diff/blame/branch/rev-parse）",
        input_schema={"type": "object", "required": ["action"],
                      "properties": {"action": {"type": "string"}}},
        output_schema={"type": "string"},
        side_effect_level="none", idempotency_policy="none",
        timeout_policy=12.0,
        # 真探针：git 可执行（--version 子进程退出码 0）
        health_check=lambda: _probe_git(),
    ), run


def _probe_git() -> bool:
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True,
                              timeout=5, check=False)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _probe_writable(workdir: str) -> bool:
    """file_write 健康探针：workdir 幂等自建后建删临时文件验写。
    （mkdir 与 run 行为一致——run 也会按需建目录。）"""
    try:
        base = Path(workdir or ".")
        base.mkdir(parents=True, exist_ok=True)
        probe = base / f".health_probe_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


BUILTIN_FACTORIES = {    "current_time": current_time_tool,
    "calculate": calculate_tool,
    "http_get": http_get_tool,
    "fs_list": fs_list_tool,
    "file_read": file_read_tool,
    "file_write": file_write_tool,
    "git_read": git_read_tool,
}
