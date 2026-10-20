"""工具注册表与执行生命周期（Architecture 6.2 / PRD 14）。

注册 → 兼容性检查 → 权限校验 → 参数验证 → 预算检查 → 执行 → 返回验证 → 业务结果验证 → 事件记录
"""

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.budget import BudgetController
from ..core.permissions import PermissionController, PermissionDecision
from ..core.task import RiskLevel
from .contract import ToolContract, ToolResult, class_error, validate_against_schema

RETRYABLE_ERRORS = {"timeout", "network_or_unavailable", "tool_defect_or_env"}


@dataclass
class RegisteredTool:
    contract: ToolContract
    runner: Callable[[Dict], ToolResult]


class ToolRegistry:
    def __init__(self, permissions: Optional[PermissionController] = None,
                 store: Optional[Any] = None):
        self.permissions = permissions
        self.store = store
        self._tools: Dict[str, RegisteredTool] = {}

    # ---- 生命周期: 注册 ----
    def register(self, contract: ToolContract,
                 runner: Callable[[Dict], ToolResult]) -> None:
        self._tools[contract.tool_id] = RegisteredTool(contract, runner)

    def register_mock_tool(self, tool_id: str, fn: Callable[[Dict], Any],
                           side_effect_level: str = "external") -> None:
        """注册一个简单 Mock 工具（演示/测试用；真实接入请用带契约的正式注册）。"""
        contract = ToolContract(tool_id=tool_id, description=f"mock {tool_id}",
                                input_schema={"type": "object"},
                                side_effect_level=side_effect_level)
        def runner(args: Dict) -> ToolResult:
            try:
                return ToolResult(ok=True, data=fn(args), submitted=True,
                                  executed=True, contract_valid=True,
                                  goal_reached=True, independently_verified=True,
                                  evidence={"mock": True})
            except Exception as e:  # noqa: BLE001
                return ToolResult.error_result(str(e), class_error(e))
        self._tools[tool_id] = RegisteredTool(contract, runner)

    def contract(self, tool_id: str) -> Optional[ToolContract]:
        rt = self._tools.get(tool_id)
        return rt.contract if rt else None

    def available_tools(self, whitelist: Optional[List[str]] = None) -> List[ToolContract]:
        return [rt.contract for tid, rt in self._tools.items()
                if whitelist is None or tid in whitelist]

    def schema_for_model(self, whitelist: Optional[List[str]] = None) -> List[Dict]:
        out = []
        for t in self.available_tools(whitelist):
            out.append({"tool_id": t.tool_id, "description": t.description,
                        "input_schema": t.input_schema})
        return out

    # ---- 生命周期: 执行 ----
    def execute(self, tool_id: str, args: Dict,
                budget: Optional[BudgetController] = None,
                idempotency_key: Optional[str] = None) -> ToolResult:
        rt = self._tools.get(tool_id)
        if rt is None:
            return ToolResult.error_result(f"工具未注册: {tool_id}", "tool_not_found")

        # 兼容性检查（Arch 6.2）：注册时已校验 schema；执行时核对契约仍在
        c = rt.contract
        # 权限校验
        risk = RiskLevel.CRITICAL if c.side_effect_level == "destructive" else (
            RiskLevel.HIGH if c.side_effect_level in ("external", "local") else RiskLevel.LOW)
        if self.permissions:
            decision = self.permissions.check_tool(
                tool_id, args, risk, side_effect=c.side_effect_level)
            if decision == PermissionDecision.DENY:
                return ToolResult.error_result(f"工具被权限拒绝: {tool_id}",
                                               "auth_or_permission")

        # 参数验证
        err = validate_against_schema(args, c.input_schema)
        if err:
            return ToolResult.error_result(f"参数校验失败: {err}", "invalid_arguments")

        # 预算检查
        if budget:
            try:
                budget.check_tool_call(tool_id)
            except Exception as e:
                return ToolResult.error_result(str(e), "budget_exceeded")

        # 幂等键（PRD 16 / Arch 6.5）：键幂等工具命中日志时直接返回原结果
        key = idempotency_key or _default_idem_key(tool_id, args)
        if self.store is not None and c.idempotency_policy == "key":
            cached = self.store.get_tool_execution(key)
            if cached:
                replay = ToolResult(**cached)
                replay.idempotency_key = key
                replay.evidence["replayed"] = True
                replay.evidence.setdefault("tool", tool_id)
                return replay

        started = time.time()
        attempts = 0
        max_attempts = 1 + max(0, int(c.retry_policy))
        while True:
            attempts += 1
            try:
                result = self._run_with_timeout(rt.runner, args,
                                                c.timeout_policy)
                break
            except Exception as e:  # noqa: BLE001
                if budget:
                    budget.record_tool_call(tool_id)
                err_class = class_error(e)
                failed = ToolResult.error_result(
                    f"{type(e).__name__}: {e}", err_class)
                failed.side_effect_uncertain = (
                    c.side_effect_level in ("external", "local", "destructive"))
                failed.evidence["attempts"] = attempts
                if (attempts >= max_attempts or err_class not in RETRYABLE_ERRORS
                        or not _retry_safe(c)):
                    failed.error = f"工具超时({c.timeout_policy}s)" \
                        if err_class == "timeout" else failed.error
                    return failed
                time.sleep(min(0.05 * attempts, 0.5))
        elapsed = time.time() - started
        if budget:
            budget.record_tool_call(tool_id)

        # 输出契约校验（Arch 6.2）：返回成功不等于符合契约
        if c.output_schema and result.ok:
            problem = validate_against_schema(result.data, c.output_schema)
            if problem:
                result.ok = False
                result.contract_valid = False
                result.error = f"输出契约校验失败: {problem}"
                result.error_class = "contract_violation"

        result.idempotency_key = key
        result.evidence["tool"] = tool_id
        result.evidence["attempts"] = attempts
        result.evidence["duration_s"] = round(elapsed, 3)
        result.evidence["side_effect_level"] = c.side_effect_level

        if (self.store is not None and c.idempotency_policy == "key"
                and result.ok and result.executed):
            self.store.save_tool_execution(key, tool_id, result.to_dict())
        return result

    def _run_with_timeout(self, runner, args, timeout: float) -> ToolResult:
        if not timeout or timeout <= 0:
            return runner(args)
        holder: Dict[str, Any] = {}

        def target() -> None:
            try:
                holder["result"] = runner(args)
            except BaseException as e:  # noqa: BLE001
                holder["error"] = e

        th = threading.Thread(target=target, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            raise TimeoutError(f"timeout after {timeout}s")
        if "error" in holder:
            raise holder["error"]
        result = holder.get("result")
        if not isinstance(result, ToolResult):
            raise TypeError("工具执行器必须返回 ToolResult")
        return result

    # ---- 诊断 ----
    def health(self) -> Dict[str, bool]:
        out = {}
        for tid, rt in self._tools.items():
            hc = rt.contract.health_check
            out[tid] = bool(hc()) if hc else True
        return out


def _retry_safe(contract: ToolContract) -> bool:
    """有副作用的工具只有在幂等键保护下才允许自动重试（PRD 16 / 35）。"""
    if contract.side_effect_level == "none":
        return True
    return contract.idempotency_policy == "key"


def _default_idem_key(tool_id: str, args: Dict) -> str:
    blob = json.dumps({"t": tool_id, "a": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def new_run_id() -> str:
    return uuid.uuid4().hex
