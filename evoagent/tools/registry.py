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
from ..core.approvals import ApprovalRequired
from ..core.leases import LeaseLost, assert_current_lease
from contextvars import copy_context
from ..core.side_effects import SideEffectLedger
from ..core.task import RiskLevel
from .contract import ToolContract, ToolResult, class_error, validate_against_schema

RETRYABLE_ERRORS = {"timeout", "network_or_unavailable", "tool_defect_or_env"}
# 主工具失败后可尝试备用工具的错误类别（Arch 6.4）
SUBSTITUTABLE_ERRORS = {"timeout", "network_or_unavailable", "tool_defect_or_env"}
_SIDE_EFFECT_RANK = {"none": 0, "local": 1, "external": 2, "destructive": 3}
# 契约 §4（GAP ⑤b）：风险级序（声明与执行同源取严格者用）
_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2,
          RiskLevel.CRITICAL: 3}


@dataclass
class RegisteredTool:
    contract: ToolContract
    runner: Callable[[Dict], ToolResult]


class ToolRegistry:
    def __init__(self, permissions: Optional[PermissionController] = None,
                 store: Optional[Any] = None):
        self.permissions = permissions
        self.store = store
        self.effects = SideEffectLedger(store) if store is not None else None
        self._tools: Dict[str, RegisteredTool] = {}

    # ---- 生命周期: 注册 ----
    _RISK_DERIVED = {"none": "low", "local": "medium",
                     "external": "high", "destructive": "critical"}

    def register(self, contract: ToolContract,
                 runner: Callable[[Dict], ToolResult]) -> None:
        # 契约 §4（GAP ⑤b）：risk_class 声明与执行同源——未声明时在
        # 注册单点按副作用级推导；显式声明走 fail-closed 抬升裁决
        risk_declared = bool(contract.risk_class)
        if contract.side_effect_level not in _SIDE_EFFECT_RANK:
            raise ValueError("未知副作用等级")
        if risk_declared and contract.declared_risk() is None:
            raise ValueError("未知工具风险等级")
        if not risk_declared:
            contract.risk_class = self._RISK_DERIVED.get(
                contract.side_effect_level, "medium")
        contract.risk_declared = risk_declared
        self._tools[contract.tool_id] = RegisteredTool(contract, runner)

    def version_inventory(self) -> List[Dict[str, str]]:
        """注册表工具版本清单（契约 §5 tool_versions[]，GAP ⑤c 真实来源）。"""
        return [{"tool_id": tid, "version": rt.contract.tool_version}
                for tid, rt in sorted(self._tools.items())]

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
        self.register(contract, runner)

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
                idempotency_key: Optional[str] = None, *,
                allowed_tools: Optional[List[str]] = None,
                task_id: str = "", run_id: str = "",
                effects: Optional[Any] = None,
                timeout_seconds: Optional[float] = None,
                require_confirmation: bool = False,
                authorization_context: Optional[Dict] = None) -> ToolResult:
        context = dict(allowed_tools=allowed_tools, task_id=task_id,
                       run_id=run_id, effects=effects,
                       timeout_seconds=timeout_seconds,
                       require_confirmation=require_confirmation,
                       authorization_context=authorization_context)
        result = self._execute_direct(tool_id, args, budget, idempotency_key,
                                      **context)
        if result.ok or result.executed or result.side_effect_uncertain:
            return result
        if result.error_class not in SUBSTITUTABLE_ERRORS:
            return result
        primary = self.contract(tool_id)
        if primary is None or not primary.substitutes:
            return result

        attempts: List[Dict[str, Any]] = []
        for sub_id in primary.substitutes:
            sub = self.contract(sub_id)
            if sub is None:
                attempts.append({"tool": sub_id, "status": "not_registered"})
                continue
            safe, reason = _substitute_safe(primary, sub)
            if not safe:
                # 不允许因主工具失败而升级权限/副作用（Arch 6.4）
                attempts.append({"tool": sub_id, "status": "rejected",
                                 "reason": reason})
                continue
            sub_result = self._execute_direct(sub_id, args, budget,
                                              idempotency_key, **context)
            attempts.append({
                "tool": sub_id,
                "status": "ok" if sub_result.ok else "failed",
                "error_class": sub_result.error_class})
            if sub_result.ok:
                sub_result.evidence["substituted_from"] = tool_id
                sub_result.evidence["substitution_attempts"] = attempts
                sub_result.evidence["original_error"] = {
                    "error": result.error,
                    "error_class": result.error_class}
                return sub_result
            # 备用工具已产生副作用（含状态不确定）时不得继续换下一个
            if sub_result.executed or sub_result.side_effect_uncertain:
                attempts[-1]["reason"] = (
                    "side_effect_uncertain" if sub_result.side_effect_uncertain
                    else "executed")
                break
        result.evidence["substitution_attempts"] = attempts
        return result

    def _execute_direct(self, tool_id: str, args: Dict,
                        budget: Optional[BudgetController] = None,
                        idempotency_key: Optional[str] = None, *,
                        allowed_tools: Optional[List[str]] = None,
                        task_id: str = "", run_id: str = "",
                        effects: Optional[Any] = None,
                        timeout_seconds: Optional[float] = None,
                        require_confirmation: bool = False,
                        authorization_context: Optional[Dict] = None) -> ToolResult:
        assert_current_lease()
        # The model's visible schema is advisory; enforce its scope here for
        # the primary tool and every substitute before any runner is invoked.
        if allowed_tools is not None and tool_id not in allowed_tools:
            return ToolResult.error_result(
                f"工具不在当前任务允许列表: {tool_id}", "auth_or_permission")
        rt = self._tools.get(tool_id)
        if rt is None:
            return ToolResult.error_result(f"工具未注册: {tool_id}", "tool_not_found")

        # 兼容性检查（Arch 6.2）：注册时已校验 schema；执行时核对契约仍在
        c = rt.contract
        err = validate_against_schema(args, c.input_schema)
        if err:
            return ToolResult.error_result(f"参数校验失败: {err}", "invalid_arguments")
        if budget:
            try:
                budget.check_tool_call(tool_id)
            except Exception as exc:
                return ToolResult.error_result(str(exc), "budget_exceeded")
        # 权限校验（契约 §4，GAP ⑤b）：声明与执行同源——取声明
        # risk_class 与副作用级推导的严格者（fail-closed，声明只能
        # 升高不能降低裁决风险）
        risk = RiskLevel.CRITICAL if c.side_effect_level == "destructive" else (
            RiskLevel.HIGH if c.side_effect_level in ("external", "local") else RiskLevel.LOW)
        declared = c.declared_risk() if c.risk_declared else None
        lifted = declared is not None and _ORDER[declared] > _ORDER[risk]
        if lifted:
            risk = declared
        if self.permissions:
            try:
                decision = self.permissions.check_tool(
                    tool_id, args, risk, side_effect=c.side_effect_level,
                    lifted=lifted, task_id=task_id, run_id=run_id,
                    require_confirmation=require_confirmation,
                    authorization_context={**(authorization_context or {}),
                        "tool_version": c.tool_version,
                        "side_effect": c.side_effect_level, "risk": risk.value,
                        "permissions": list(c.permission_requirements or [])})
            except ApprovalRequired as exc:
                result = ToolResult.error_result(str(exc), "approval_required")
                result.evidence["approval_id"] = exc.approval_id
                return result
            if decision != PermissionDecision.ALLOW:
                return ToolResult.error_result(f"工具被权限拒绝: {tool_id}",
                                               "auth_or_permission")
        elif require_confirmation:
            return ToolResult.error_result("缺少节点确认权限控制器", "auth_or_permission")

        # Synchronous confirmation may take time; recheck before execution.
        if budget:
            try:
                budget.check_tool_call(tool_id)
            except Exception as e:
                return ToolResult.error_result(str(e), "budget_exceeded")

        # 幂等键（PRD 16 / Arch 6.5）：键幂等工具命中日志时直接返回原结果
        key = idempotency_key or _default_idem_key(tool_id, args)
        ledger = effects if effects is not None else self.effects
        effect_id = None
        if ledger is not None and c.side_effect_level != "none":
            operation_task = task_id or ("tool_" + key if idempotency_key
                                        else "tool_" + uuid.uuid4().hex)
            try:
                pending = ledger.pending(operation_task)
                if any(row["status"] == "unknown"
                       or row["idempotency_key"] != key
                       or row["tool_id"] != tool_id for row in pending):
                    blocked = ToolResult.error_result(
                        "任务存在未对账副作用，禁止创建新副作用", "side_effect_unknown")
                    blocked.side_effect_uncertain = True
                    return blocked
                effect_id, claimed = ledger.begin(
                    operation_task, tool_id, key,
                    _default_idem_key(tool_id, args), run_id=run_id)
                if not claimed:
                    row = ledger.get(effect_id)
                    cached = ledger.db.get_tool_execution("effect:" + effect_id)
                    if row["status"] == "confirmed" and cached:
                        replay = ToolResult(**cached)
                        replay.evidence["replayed"] = True
                        return replay
                    blocked = ToolResult.error_result(
                        "副作用已提交或结果待对账，禁止重放", "side_effect_unknown")
                    blocked.side_effect_uncertain = True
                    blocked.idempotency_key = key
                    blocked.evidence.update(tool=tool_id, effect_id=effect_id,
                                            side_effect_level=c.side_effect_level)
                    return blocked
            except LeaseLost:
                raise
            except Exception as e:
                return ToolResult.error_result(
                    f"副作用预记账失败，拒绝执行: {type(e).__name__}",
                    "side_effect_ledger")
        if self.store is not None and c.idempotency_policy == "key":
            # Task-bound operations replay from their own ledger; never use
            # another task's cached result just because its arguments match.
            cached = (self.store.get_tool_execution(key)
                      if effect_id is None else None)
            if cached:
                replay = ToolResult(**cached)
                replay.idempotency_key = key
                replay.evidence["replayed"] = True
                replay.evidence.setdefault("tool", tool_id)
                return replay

        started = time.time()
        attempts = 0
        max_attempts = 1 + max(0, int(c.retry_policy))
        timeout = c.timeout_policy
        if timeout_seconds is not None and timeout_seconds > 0:
            timeout = min(timeout, timeout_seconds) if timeout > 0 else timeout_seconds
        while True:
            attempts += 1
            if budget:
                budget.check_tool_call(tool_id)
                budget.record_tool_call(tool_id)  # durable reservation before entering runner
            try:
                assert_current_lease()
                result = self._run_with_timeout(rt.runner, args,
                                                timeout)
                break
            except LeaseLost:
                raise
            except Exception as e:  # noqa: BLE001
                err_class = class_error(e)
                failed = ToolResult.error_result(
                    f"{type(e).__name__}: {e}", err_class)
                failed.side_effect_uncertain = (
                    c.side_effect_level in ("external", "local", "destructive"))
                failed.evidence["attempts"] = attempts
                if (attempts >= max_attempts or err_class not in RETRYABLE_ERRORS
                        or failed.side_effect_uncertain or not _retry_safe(c)):
                    failed.error = f"工具超时({timeout}s)" \
                        if err_class == "timeout" else failed.error
                    result = failed
                    break
                time.sleep(min(0.05 * attempts, 0.5))
        elapsed = time.time() - started

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
        if effect_id is not None:
            result.evidence["effect_id"] = effect_id
            try:
                ledger.finish(effect_id, result)
            except Exception as e:
                result.ok = False
                result.side_effect_uncertain = True
                result.error_class = "side_effect_unknown"
                result.error = f"副作用结果未可靠落账: {type(e).__name__}"

        if (self.store is not None and effect_id is None and c.idempotency_policy == "key"
                and result.ok and result.executed):
            self.store.save_tool_execution(key, tool_id, result.to_dict())
        return result

    def _run_with_timeout(self, runner, args, timeout: float) -> ToolResult:
        assert_current_lease()
        if not timeout or timeout <= 0:
            result = runner(args)
            if not isinstance(result, ToolResult):
                raise TypeError("工具执行器必须返回 ToolResult")
            return result
        holder: Dict[str, Any] = {}

        def target() -> None:
            try:
                assert_current_lease()
                holder["result"] = runner(args)
            except BaseException as e:  # noqa: BLE001
                holder["error"] = e

        th = threading.Thread(target=copy_context().run, args=(target,), daemon=True)
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
    def health(self) -> Dict[str, Optional[bool]]:
        """工具健康（F-04 死字段修复①，三值语义）。

        True/False 来自 contract.health_check 真探针；None=工具未声明
        探针（如网络类）→ 上报 **unknown**，不得再恒真伪装全绿。
        探针自身抛错按 False 计（fail-closed）。
        """
        out: Dict[str, Optional[bool]] = {}
        for tid, rt in self._tools.items():
            hc = rt.contract.health_check
            if hc is None:
                out[tid] = None
                continue
            try:
                out[tid] = bool(hc())
            except Exception:  # noqa: BLE001 —— 探针故障即不健康
                out[tid] = False
        return out


def _substitute_safe(primary: ToolContract,
                     sub: ToolContract) -> Tuple[bool, str]:
    """备用工具不得扩大权限或副作用，且必须接受主工具的必填参数（Arch 6.4）。"""
    if _SIDE_EFFECT_RANK.get(sub.side_effect_level, 3) > \
            _SIDE_EFFECT_RANK.get(primary.side_effect_level, 3):
        return False, "higher_side_effect"
    if set(sub.permission_requirements) - set(primary.permission_requirements):
        return False, "more_permissions"
    p_props = (primary.input_schema or {}).get("properties", {})
    s_props = (sub.input_schema or {}).get("properties", {})
    for field in (primary.input_schema or {}).get("required", []):
        expected = p_props.get(field, {})
        offered = s_props.get(field, {})
        if not offered:
            return False, f"missing_input:{field}"
        if expected.get("type") and offered.get("type") \
                and expected["type"] != offered["type"]:
            return False, f"incompatible_input:{field}"
    return True, ""


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
