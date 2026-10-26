"""动态团队与跨专家协作契约（ALN-26-B）。

复用 `ExpertRegistry` 的能力目录做团队角色、输入/输出、冲突与失败恢复，
不重写注册表与路由；本模块只提供**协作契约层**：

1. **成组**：给定意图/领域与必需能力，从真实能力目录指派角色；能力缺失
   或归属歧义时**拒绝成组**并列出缺口/候选（可解释冲突）。
2. **唯一状态写入者**：每个状态物（artifact）在契约里只有一个写入角色；
   契约校验会拒绝重复写入者；状态推进只能由对应写入者发起。
3. **跨专家交接**：显式 `Handoff`（生产角色 → 消费角色、允许的输入引用、
   上下文范围、预算份额）；未声明/越权/自交接一律拒绝。
4. **失败恢复**：局部失败只标记该角色失败；恢复**不自动重放外部动作**
   （`auto_replay=False`，有外部动作时要求人工确认）；失败角色的外部动作
   对其它角色不可代执行（不越权）。

契约本身无副作用；真正的运行时接线（planning/workflows 主链）不在本卡并
行修改范围内，见交付文档「保留缺口」。
"""

import hashlib
import json
import math
from copy import deepcopy
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .registry import Expert, ExpertRegistry

STATUSES = ("proposed", "active", "completed", "aborted")


def _strings(values, name):
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(v, str) or not v.strip() for v in values
    ):
        raise TeamContractError(name + "必须为非空字符串数组")
    return tuple(values)


class TeamContractError(ValueError):
    """契约请求非法或契约不再满足（角色/状态/字段问题）。"""


class TeamFormationError(TeamContractError):
    """无法成组（能力缺失、归属冲突、超出角色上限等）。"""


@dataclass(frozen=True)
class TeamRole:
    role_id: str
    expert_id: str
    domain: str
    capabilities: Sequence[str]
    responsibilities: Sequence[str] = field(default_factory=list)
    outputs: Sequence[str] = field(default_factory=list)
    state_artifacts: Sequence[str] = field(default_factory=list)
    external_actions: Sequence[str] = field(default_factory=list)
    expert_version: str = ""  # Empty only for legacy/manual declarations.

    def __post_init__(self):
        if not isinstance(self.expert_version, str):
            raise TeamContractError("专家版本必须是字符串")
        for name in (
            "capabilities",
            "responsibilities",
            "outputs",
            "state_artifacts",
            "external_actions",
        ):
            object.__setattr__(self, name, _strings(getattr(self, name), name))

    def to_dict(self) -> Dict[str, Any]:
        return {
            k: list(v) if isinstance(v, tuple) else v for k, v in asdict(self).items()
        }


@dataclass(frozen=True)
class Handoff:
    handoff_id: str
    from_role: str
    to_role: str
    input_refs: Sequence[str]
    context_scope: Sequence[str] = field(default_factory=list)
    budget_units: float = 0.0

    def __post_init__(self):
        for name in ("input_refs", "context_scope"):
            object.__setattr__(self, name, _strings(getattr(self, name), name))
        if (
            type(self.budget_units) not in (int, float)
            or not math.isfinite(self.budget_units)
            or self.budget_units < 0
        ):
            raise TeamContractError("交接预算必须为有限非负数")

    def to_dict(self) -> Dict[str, Any]:
        return {
            k: list(v) if isinstance(v, tuple) else v for k, v in asdict(self).items()
        }


class TeamContract:
    def __init__(
        self,
        *,
        contract_id: str,
        task_intent: str,
        domain: str,
        roles: List[TeamRole],
        task_ref: str = "",
        clock=None,
    ):
        self.contract_id = contract_id
        self.task_intent = task_intent
        self.domain = domain
        self.task_ref = task_ref
        self.roles = tuple(roles)
        self.handoffs: List[Handoff] = []
        self.state_writers: Dict[str, str] = {}
        for role in self.roles:
            for artifact in role.state_artifacts:
                self.state_writers.setdefault(artifact, role.role_id)
        self.status = "proposed"
        self.failures: Dict[str, str] = {}
        self.transitions: List[Dict[str, Any]] = []
        self._completed_roles = set()
        self.created_at = (clock or time.time)()
        self.revision = 1

    # ---- 序列化与校验 ----
    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "task_intent": self.task_intent,
            "domain": self.domain,
            "task_ref": self.task_ref,
            "roles": [role.to_dict() for role in self.roles],
            "handoffs": [item.to_dict() for item in self.handoffs],
            "state_writers": dict(self.state_writers),
            "status": self.status,
            "completed_roles": sorted(self._completed_roles),
            "failures": dict(self.failures),
            "transitions": deepcopy(self.transitions),
            "created_at": self.created_at,
            "revision": self.revision,
        }

    def digest(self) -> str:
        body = {
            key: value
            for key, value in self.to_dict().items()
            if key not in ("transitions", "created_at", "revision")
        }
        return hashlib.sha256(
            json.dumps(
                body, ensure_ascii=False, sort_keys=True, allow_nan=False
            ).encode()
        ).hexdigest()

    def validate(self) -> Dict[str, Any]:
        errors: List[str] = []
        role_ids = [role.role_id for role in self.roles]
        if not self.roles or any(
            not isinstance(v, str) or not v.strip() for v in role_ids
        ):
            errors.append("角色列表与身份不能为空")
        if self.status not in STATUSES:
            errors.append("契约状态非法")
        if len(set(role_ids)) != len(role_ids):
            errors.append("角色 ID 重复")
        writers: Dict[str, str] = {}
        for role in self.roles:
            if not role.capabilities:
                errors.append(f"角色 {role.role_id} 没有能力声明")
            for artifact in role.state_artifacts:
                if artifact in writers and writers[artifact] != role.role_id:
                    errors.append(
                        f"状态物 {artifact} 存在多个写入者: "
                        f"{writers[artifact]} / {role.role_id}"
                    )
                writers[artifact] = role.role_id
        if writers != self.state_writers:
            errors.append("状态写入者映射与角色声明不一致")
        if not self.state_writers:
            errors.append("契约没有状态写入者")
        for artifact, writer in self.state_writers.items():
            if writer not in role_ids:
                errors.append(f"状态物 {artifact} 的写入者不存在: {writer}")
        seen_handoffs = set()
        for handoff in self.handoffs:
            if handoff.handoff_id in seen_handoffs:
                errors.append("交接 ID 重复: " + handoff.handoff_id)
            seen_handoffs.add(handoff.handoff_id)
            if handoff.from_role not in role_ids or handoff.to_role not in role_ids:
                errors.append("交接引用了不存在的角色: " + handoff.handoff_id)
            if handoff.from_role == handoff.to_role:
                errors.append("角色不能交接给自己: " + handoff.handoff_id)
            producer = next(
                (r for r in self.roles if r.role_id == handoff.from_role), None
            )
            if producer is not None:
                if not handoff.input_refs or set(handoff.input_refs) - set(
                    producer.outputs
                ):
                    errors.append("交接输入不属于生产角色输出: " + handoff.handoff_id)
                allowed_context = set(producer.outputs) | (
                    {self.task_ref} if self.task_ref else set()
                )
                if set(handoff.context_scope) - allowed_context:
                    errors.append(
                        "交接上下文超出本任务与生产角色输出: " + handoff.handoff_id
                    )
            if (
                type(handoff.budget_units) not in (int, float)
                or not math.isfinite(handoff.budget_units)
                or handoff.budget_units < 0
            ):
                errors.append("交接预算无效: " + handoff.handoff_id)
        return {"ok": not errors, "errors": errors}

    # ---- 唯一状态写入者推进 ----
    def writer_of(self, artifact: str) -> Optional[str]:
        return self.state_writers.get(artifact)

    def advance(self, *, role_id: str, actor: str = "") -> Dict[str, Any]:
        check = self.validate()
        if not check["ok"]:
            return {"ok": False, "reason": "契约无效: " + "; ".join(check["errors"])}
        if self.status == "active" and self.failures:
            return {"ok": False, "reason": "仍有失败角色，不能完成整个契约"}
        if role_id not in {role.role_id for role in self.roles}:
            return {"ok": False, "reason": "角色不在契约中: " + role_id}
        if role_id in self.failures:
            return {"ok": False, "reason": "失败角色不能推进契约状态: " + role_id}
        is_writer = role_id in set(self.state_writers.values())
        if not is_writer:
            return {
                "ok": False,
                "reason": "非唯一状态写入者，不能推进契约状态: " + role_id,
            }
        order = {"proposed": "active", "active": "completed"}
        if self.status not in order:
            return {"ok": False, "reason": "契约已处于终态: " + self.status}
        if self.status == "active":
            pending = set(self.state_writers.values()) - (
                self._completed_roles | {role_id}
            )
            if pending:
                if role_id not in self._completed_roles:
                    self._completed_roles.add(role_id)
                    self.revision += 1
                    self.transitions.append(
                        {
                            "from": "active",
                            "to": "active",
                            "role_id": role_id,
                            "actor": actor,
                            "at": time.time(),
                            "event": "role_completed",
                        }
                    )
                return {
                    "ok": True,
                    "status": "active",
                    "previous": "active",
                    "awaiting_roles": sorted(pending),
                }
            self._completed_roles.add(role_id)
        previous, self.status = self.status, order[self.status]
        self.transitions.append(
            {
                "from": previous,
                "to": self.status,
                "role_id": role_id,
                "actor": actor,
                "at": time.time(),
            }
        )
        self.revision += 1
        return {"ok": True, "status": self.status, "previous": previous}

    # ---- 失败与恢复 ----
    def fail(self, role_id: str, reason: str) -> Dict[str, Any]:
        if self.status not in ("proposed", "active"):
            raise TeamContractError("终态契约不能修改失败记录")
        if role_id not in {role.role_id for role in self.roles}:
            raise TeamContractError("角色不在契约中: " + role_id)
        if not reason.strip():
            raise TeamContractError("失败原因不得为空")
        self._completed_roles.discard(role_id)
        self.failures[role_id] = reason
        self.revision += 1
        return {"ok": True, "role_id": role_id, "reason": reason}

    def recovery(self) -> Dict[str, Any]:
        failed_roles = sorted(self.failures)
        external = []
        for role in self.roles:
            if role.role_id in self.failures:
                external.extend(
                    {"role_id": role.role_id, "action": action}
                    for action in role.external_actions
                )
        return {
            "failed_roles": failed_roles,
            "auto_replay": False,
            "requires_confirmation": bool(external),
            "external_actions": external,
            "note": (
                "局部失败不自动重放外部动作；需人工确认后由原角色（唯一写入者）恢复"
            )
            if external
            else "无外部动作，可安全重试",
        }

    def authorize_external_action(
        self, *, role_id: str, action: str, confirmed: bool = False
    ) -> Dict[str, Any]:
        check = self.validate()
        if not check["ok"]:
            return {"ok": False, "reason": "契约无效: " + "; ".join(check["errors"])}
        if type(confirmed) is not bool:
            return {"ok": False, "reason": "确认必须是明确布尔值"}
        role = next((item for item in self.roles if item.role_id == role_id), None)
        if role is None:
            return {"ok": False, "reason": "角色不在契约中: " + role_id}
        if role_id in self._completed_roles:
            return {"ok": False, "reason": "角色已确认完成，禁止追加外部动作"}
        if role_id in self.failures and not confirmed:
            return {
                "ok": False,
                "reason": "角色已失败，外部动作需人工确认后重放: " + action,
            }
        if action not in role.external_actions:
            return {
                "ok": False,
                "reason": "该动作不属于此角色（不越权代执行）: " + action,
            }
        if self.status not in ("active",):
            return {"ok": False, "reason": "契约未激活，禁止执行外部动作"}
        return {"ok": True, "role_id": role_id, "action": action}


class TeamPlanner:
    """按真实能力目录成组，并校验交接与唯一写入者。"""

    def __init__(self, registry: ExpertRegistry, *, clock=None):
        if registry is None:
            raise TeamContractError("TeamPlanner 需要 ExpertRegistry")
        self.registry = registry
        self.clock = clock or time.time

    def _expert_pool(self) -> List[Expert]:
        return list(self.registry.all())

    def _capability_owners(self) -> Dict[str, List[Expert]]:
        owners: Dict[str, List[Expert]] = {}
        for expert in self._expert_pool():
            for capability in expert.capabilities:
                owners.setdefault(capability, []).append(expert)
        for capability in self.registry.capabilities():
            if capability.status != "active":
                owners[capability.capability_id] = []
            else:
                owners.setdefault(capability.capability_id, [])
        return owners

    def _pick_expert(
        self, capability: str, intent: str, domain: str, owners: Dict[str, List[Expert]]
    ) -> Expert:
        candidates = owners.get(capability) or []
        if not candidates:
            raise TeamFormationError("能力目录缺少能力: " + capability)
        if len(candidates) == 1:
            return candidates[0]
        intent_hits = [
            expert for expert in candidates if intent in expert.applicable_intents
        ]
        if len(intent_hits) == 1:
            return intent_hits[0]
        domain_hits = [expert for expert in candidates if expert.expert_id == domain]
        if len(domain_hits) == 1:
            return domain_hits[0]
        raise TeamFormationError(
            "能力归属冲突，无法唯一指派: "
            + capability
            + " 候选="
            + ",".join(sorted(expert.expert_id for expert in candidates))
        )

    def form(
        self,
        *,
        intent: str,
        domain: str,
        required_capabilities: Sequence[str],
        task_ref: str = "",
        contract_id: str = "",
    ) -> TeamContract:
        if any(not isinstance(value, str) for value in (intent, domain, task_ref, contract_id)):
            raise TeamFormationError("团队意图、领域与引用必须是字符串")
        if not isinstance(required_capabilities, list) or not required_capabilities:
            raise TeamFormationError("required_capabilities 必须是非空数组")
        if any(
            not isinstance(cap, str) or not cap.strip() for cap in required_capabilities
        ):
            raise TeamFormationError("能力 id 必须是非空字符串")
        required_capabilities = sorted(set(required_capabilities))
        owners = self._capability_owners()
        missing = sorted({cap for cap in required_capabilities if not owners.get(cap)})
        if missing:
            raise TeamFormationError("能力缺口，无法成组: " + ", ".join(missing))
        assigned: Dict[str, List[str]] = {}
        for capability in required_capabilities:
            expert = self._pick_expert(capability, intent, domain, owners)
            assigned.setdefault(expert.expert_id, []).append(capability)
        roles: List[TeamRole] = []
        for expert_id, capabilities in sorted(assigned.items()):
            expert = self.registry.get(expert_id)
            role_id = "role_" + expert_id
            roles.append(
                TeamRole(
                    role_id=role_id,
                    expert_id=expert_id,
                    domain=(expert.domain_description or "")[:40],
                    capabilities=sorted(capabilities),
                    responsibilities=[
                        f"负责能力: {cap}" for cap in sorted(capabilities)
                    ],
                    outputs=[
                        f"{role_id}:output:" + cap for cap in sorted(capabilities)
                    ],
                    state_artifacts=[
                        f"{role_id}:artifact:" + cap for cap in sorted(capabilities)
                    ],
                    external_actions=[],
                    expert_version=expert.version,
                )
            )
        if len(roles) > len(self._expert_pool()):
            raise TeamFormationError("角色数超过专家数上限")
        contract = TeamContract(
            contract_id=contract_id
            or (
                "team_"
                + hashlib.sha256(
                    json.dumps({
                        "intent": intent, "domain": domain, "task_ref": task_ref,
                        "required_capabilities": required_capabilities,
                        "roles": [role.to_dict() for role in roles],
                    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                        allow_nan=False).encode("utf-8")
                ).hexdigest()[:24]
            ),
            task_intent=intent,
            domain=domain,
            roles=roles,
            task_ref=task_ref,
            clock=self.clock,
        )
        check = contract.validate()
        if not check["ok"]:
            raise TeamFormationError("契约校验失败: " + "; ".join(check["errors"]))
        return contract

    def add_handoff(
        self,
        contract: TeamContract,
        *,
        from_role: str,
        to_role: str,
        input_refs: Sequence[str],
        context_scope: Optional[List[str]] = None,
        budget_units: float = 0.0,
    ) -> Dict[str, Any]:
        if contract.status not in ("proposed", "active"):
            raise TeamContractError("终态契约不能添加交接")
        before = contract.validate()
        if not before["ok"]:
            raise TeamContractError("契约无效: " + "; ".join(before["errors"]))
        roles = {role.role_id: role for role in contract.roles}
        if from_role not in roles or to_role not in roles:
            raise TeamContractError("交接角色不存在")
        if from_role == to_role:
            raise TeamContractError("角色不能交接给自己")
        input_refs = list(_strings(input_refs, "input_refs"))
        if not input_refs:
            raise TeamContractError("交接必须声明输入引用")
        producer_outputs = set(roles[from_role].outputs)
        unknown = [ref for ref in input_refs if ref not in producer_outputs]
        if unknown:
            raise TeamContractError("交接输入不属于生产角色输出: " + ", ".join(unknown))
        scope = list(_strings(context_scope or (), "context_scope"))
        if contract.task_ref and contract.task_ref not in scope:
            scope.append(contract.task_ref)
        if (
            type(budget_units) not in (int, float)
            or not math.isfinite(budget_units)
            or budget_units < 0
        ):
            raise TeamContractError("交接预算必须为有限非负数")
        handoff = Handoff(
            handoff_id="ho_"
            + hashlib.sha256(
                (
                    contract.contract_id
                    + "|"
                    + from_role
                    + "|"
                    + to_role
                    + "|"
                    + ",".join(input_refs)
                ).encode()
            ).hexdigest()[:10],
            from_role=from_role,
            to_role=to_role,
            input_refs=list(input_refs),
            context_scope=scope,
            budget_units=float(budget_units),
        )
        contract.handoffs.append(handoff)
        check = contract.validate()
        if not check["ok"]:
            contract.handoffs.pop()
            raise TeamContractError("交接破坏契约: " + "; ".join(check["errors"]))
        contract.revision += 1
        return {"ok": True, "handoff": handoff.to_dict(), "digest": contract.digest()}
