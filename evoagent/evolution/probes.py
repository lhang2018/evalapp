"""诊断探针（PRD 43/44 / Architecture 9.4-9.5）。

固定探针，只读或有界本地副作用：
- 不访问外部网络；
- 文件写入只发生在临时目录；
- 单探针超时后返回不确定，不中断流程；
- 探针结论只作为假设的加减分证据，不直接宣布根因。
"""

import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..core.state_machine import ALLOWED_TRANSITIONS
from ..core.task import TaskStatus
from ..memory.context import ContextAssembler, ContextBudget, check_compression
from ..memory.store import Credibility, MemoryItem, MemoryKind, MemoryStore, Scope


@dataclass
class ProbeResult:
    passed: Optional[bool]              # None = 不确定
    evidence: Dict[str, Any] = field(default_factory=dict)
    cost: float = 0.0


def _tool_health(tools=None, **_: Any) -> ProbeResult:
    if tools is None:
        return ProbeResult(None, {"reason": "未注册工具"})
    health = tools.health()
    failed = [tid for tid, ok in health.items() if not ok]
    return ProbeResult(not failed, {"tools": health, "failed": failed})


def _calc_roundtrip(tools=None, **_: Any) -> ProbeResult:
    if tools is None:
        return ProbeResult(None, {"reason": "未注册工具"})
    result = tools.execute("calculate", {"expression": "1+1"})
    ok = bool(result.ok and result.data == 2 and result.independently_verified)
    return ProbeResult(ok, {
        "tool": "calculate", "ok": result.ok, "data": result.data,
        "error": result.error, "contract_valid": result.contract_valid})


def _file_write_sandbox(**_: Any) -> ProbeResult:
    with tempfile.TemporaryDirectory(prefix="evo_probe_") as d:
        p = Path(d) / "probe.txt"
        content = "evoagent-probe"
        p.write_text(content, encoding="utf-8")
        ok = p.read_text(encoding="utf-8") == content
        leftovers = [x.name for x in Path(d).iterdir()]
        return ProbeResult(ok, {
            "sandbox": "temporary", "verified": ok, "entries": leftovers})


def _state_machine_selfcheck(**_: Any) -> ProbeResult:
    terminals = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    bad_edges = [s.value for s in terminals
                 if ALLOWED_TRANSITIONS.get(s)]
    reachable = {TaskStatus.CREATED}
    frontier = [TaskStatus.CREATED]
    while frontier:
        cur = frontier.pop()
        for nxt in ALLOWED_TRANSITIONS.get(cur, frozenset()):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    unreachable = sorted(s.value for s in TaskStatus if s not in reachable)
    ok = not bad_edges and not unreachable
    return ProbeResult(ok, {
        "terminal_with_out_edges": bad_edges,
        "unreachable_states": unreachable})


def _memory_roundtrip(memory=None, **_: Any) -> ProbeResult:
    tmp = None
    try:
        if memory is None:
            tmp = tempfile.TemporaryDirectory(prefix="evo_mem_")
            memory = MemoryStore(str(Path(tmp.name) / "memory.db"))
        item = MemoryItem(content="diagnostic probe 往返验证 probe-roundtrip",
                          kind=MemoryKind.RUNTIME_EXPERIENCE,
                          scope=Scope.TASK, scope_key="__probe__",
                          status=Credibility.VERIFIED,
                          evidence={"probe": True})
        memory.write(item)
        hits = memory.search("probe-roundtrip 往返", scope=Scope.TASK,
                             scope_key="__probe__", limit=5)
        found = any(h.memory_id == item.memory_id for h in hits)
        memory.set_status(item.memory_id, Credibility.REVOKED, "probe cleanup")
        return ProbeResult(found, {"written": item.memory_id, "found": found})
    finally:
        if tmp is not None:
            tmp.cleanup()


def _context_constraint_retention(**_: Any) -> ProbeResult:
    constraint = "禁止调用 file_write 覆盖用户文件"
    assembler = ContextAssembler(None, ContextBudget(max_chars=80))
    before = assembler.assemble("上下文约束保留探针", constraints=[constraint],
                                acceptance=[{"id": "a", "kind": "result_nonempty"}])
    after = assembler.assemble("压缩后的目标", constraints=[constraint],
                               acceptance=[{"id": "a", "kind": "result_nonempty"}])
    dropped = check_compression(before, after)
    retained = constraint in after.hard_constraints
    return ProbeResult(not dropped and retained,
                       {"dropped": dropped, "retained": retained})


PROBES: Dict[str, Callable[..., ProbeResult]] = {
    "tool_health": _tool_health,
    "calc_roundtrip": _calc_roundtrip,
    "file_write_sandbox": _file_write_sandbox,
    "state_machine_selfcheck": _state_machine_selfcheck,
    "memory_roundtrip": _memory_roundtrip,
    "context_constraint_retention": _context_constraint_retention,
}


class ProbeRunner:
    """单例执行器：超时与异常都转为「不确定」。"""

    def __init__(self, tools=None, memory=None, config=None):
        self.tools = tools
        self.memory = memory
        self.config = config

    def run(self, method: str, fault: Optional[str] = None) -> ProbeResult:
        """fault: error | timeout | fail — 故障注入只影响本次探针（Arch 9.5）。"""
        fn = PROBES.get(method)
        if fn is None:
            return ProbeResult(None, {"reason": f"未知探针: {method}"})
        timeout = float(getattr(self.config, "max_probe_seconds", 10) or 10)
        box: Dict[str, Any] = {}

        def target() -> None:
            try:
                if fault == "error":
                    raise RuntimeError("injected probe fault")
                if fault == "timeout":
                    time.sleep(timeout * 2)
                result = fn(tools=self.tools, memory=self.memory,
                            config=self.config)
                if fault == "fail" and isinstance(result, ProbeResult):
                    result.passed = False
                box["result"] = result
            except Exception as e:  # noqa: BLE001
                box["error"] = f"{type(e).__name__}: {e}"
                box["traceback"] = traceback.format_exc(limit=3)

        t = threading.Thread(target=target, daemon=True)
        started = time.time()
        t.start()
        t.join(timeout)
        if t.is_alive():
            return ProbeResult(None, {"method": method, "reason": "timeout",
                                      "fault_injected": fault,
                                      "timeout_s": timeout,
                                      "elapsed_s": round(time.time() - started, 3)})
        if "error" in box:
            return ProbeResult(None, {"method": method, "reason": box["error"],
                                      "fault_injected": fault,
                                      "traceback": box.get("traceback", "")})
        result = box.get("result")
        if not isinstance(result, ProbeResult):
            return ProbeResult(None, {"method": method,
                                      "reason": "探针未返回结果"})
        result.evidence.setdefault("method", method)
        result.evidence.setdefault("elapsed_s", round(time.time() - started, 3))
        if fault:
            result.evidence["fault_injected"] = fault
        return result
