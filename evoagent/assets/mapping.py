"""现有实体到统一 Asset 的映射（ALN-06-A 步骤 1）。

映射是**只读描述**，不自动晋升：现有专家/工作流/模型没有评测证据引用，
因此统一落在 candidate（不可用/停用条目映射为 retired 归档），
再由资产域按证据门槛显式晋升。缺失信息不猜，逐条写进 `missing`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ..experts.registry import CapabilityEntry, Expert
from ..models.base import ModelProfile
from ..workflows.models import WorkflowDef

#: 每个资产类型的默认 namespace（契约 §6 五域）。
DEFAULT_NAMESPACE: Dict[str, str] = {
    "expert": "domain_private",
    "workflow": "shared_validated",
    "model_profile": "shared_validated",
    "skill": "domain_private",
    "capability_pack": "domain_private",
    "tool": "shared_validated",
    "knowledge": "shared_validated",
    "evaluation_suite": "shared_validated",
}

#: 旧 `CapabilityEntry.status` 三值 → 八态。语义有损，必须保留原值备注。
LEGACY_STATUS_MAP: Dict[str, str] = {
    "active": "candidate",
    "experimental": "draft",
    "disabled": "retired",
}

#: 允许直接登记的初始状态。approved/active 必须走带证据门槛的迁移，
#: 否则“证据不足不能激活”可以被注册入口绕过。
REGISTERABLE_STATES = ("draft", "candidate", "deprecated", "retired")


def asset_id_for(asset_type: str, local_id: str) -> str:
    """稳定身份：类型前缀避免不同实体同名冲突。"""
    return f"{asset_type}:{local_id}"


@dataclass(frozen=True)
class AssetDraft:
    """登记前的资产描述（内容是确定性字典，尚未计算摘要）。"""

    asset_id: str
    asset_type: str
    namespace: str
    version: str
    content: Dict[str, Any]
    lifecycle_state: str = "candidate"
    source_refs: Tuple[str, ...] = ()
    dependency_refs: Tuple[str, ...] = ()
    compatibility: Dict[str, Any] = field(default_factory=dict)
    evaluation_evidence_refs: Tuple[str, ...] = ()
    rollback_ref: Optional[str] = None
    missing: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()


def _expert_content(expert: Expert) -> Dict[str, Any]:
    # created_at 不进入内容：同一配置在不同时刻登记必须得到同一摘要。
    return {
        "expert_id": expert.expert_id,
        "version": expert.version,
        "domain_description": expert.domain_description,
        "capabilities": list(expert.capabilities),
        "applicable_intents": list(expert.applicable_intents),
        "not_applicable": list(expert.not_applicable),
        "system_prompt": expert.system_prompt,
        "tool_whitelist": (None if expert.tool_whitelist is None
                           else list(expert.tool_whitelist)),
        "memory_scope_key": expert.memory_scope_key,
        "workflow_ids": list(expert.workflow_ids),
        "model_preference": expert.model_preference,
        "budget_class": expert.budget_class,
        "quality_metrics": list(expert.quality_metrics),
        "dependencies": list(expert.dependencies),
        "release_record": dict(expert.release_record),
    }


def _workflow_content(wf: WorkflowDef) -> Dict[str, Any]:
    return wf.to_dict()


def _model_content(profile: ModelProfile) -> Dict[str, Any]:
    return {
        "model_id": profile.model_id,
        "provider": profile.provider,
        "api_version": profile.api_version,
        "context_limit": profile.context_limit,
        "supports_tool_call": profile.supports_tool_call,
        "supports_structured_output": profile.supports_structured_output,
        "supports_vision": profile.supports_vision,
        "rpm_limit": profile.rpm_limit,
        "price_per_1k_input": profile.price_per_1k_input,
        "price_per_1k_output": profile.price_per_1k_output,
        "pricing_known": profile.pricing_known,
        "quality_score": profile.quality_score,
        "avg_latency_s": profile.avg_latency_s,
        "available": profile.available,
        "known_limitations": list(profile.known_limitations),
        "compat_tested": profile.compat_tested,
        # adapter_config 可能含端点；不进内容摘要，避免环境信息漂移。
    }


def expert_to_asset(expert: Expert,
                    dependency_versions: Optional[Mapping[str, str]] = None
                    ) -> AssetDraft:
    missing: List[str] = []
    notes: List[str] = []
    dep_versions = dict(dependency_versions or {})
    deps = []
    for dep in expert.dependencies:
        version = dep_versions.get(dep)
        if version:
            deps.append(f"{asset_id_for('skill', dep)}@{version}")
        else:
            missing.append(f"依赖 {dep} 缺少精确版本，未写入 dependency_refs")
    if not expert.quality_metrics:
        missing.append("缺少评测证据引用（quality_metrics 只是指标名，不是证据）")
    notes.append("现有 Expert 无 lifecycle_state，映射为 candidate，需证据后晋升")
    return AssetDraft(
        asset_id=asset_id_for("expert", expert.expert_id),
        asset_type="expert", namespace=DEFAULT_NAMESPACE["expert"],
        version=expert.version, content=_expert_content(expert),
        lifecycle_state="candidate",
        source_refs=(f"experts/registry/{expert.expert_id}",),
        dependency_refs=tuple(deps),
        compatibility={"pins": {}},
        missing=tuple(missing), notes=tuple(notes))


def workflow_to_asset(wf: WorkflowDef) -> AssetDraft:
    missing: List[str] = []
    notes: List[str] = []
    tool_nodes = sorted({n.tool for n in wf.nodes if n.tool})
    if tool_nodes:
        missing.append(
            "节点工具尚未登记为资产，无法生成精确依赖引用: "
            + ", ".join(tool_nodes))
    if any(n.fn is not None for n in wf.nodes):
        notes.append("函数节点不可序列化；资产内容只含工具节点（与工作流库一致）")
    notes.append("工作流 status 是执行态七值，不是生命周期八态；映射为 candidate")
    return AssetDraft(
        asset_id=asset_id_for("workflow", wf.workflow_id),
        asset_type="workflow", namespace=DEFAULT_NAMESPACE["workflow"],
        version=wf.version, content=_workflow_content(wf),
        lifecycle_state="candidate",
        source_refs=(f"workflows/library/{wf.workflow_id}",),
        compatibility={"pins": {}}, missing=tuple(missing), notes=tuple(notes))


def model_profile_to_asset(profile: ModelProfile) -> AssetDraft:
    missing: List[str] = []
    notes: List[str] = []
    version = profile.api_version or "unversioned"
    if not profile.api_version:
        missing.append("ModelProfile 无版本字段，使用 unversioned（需供应商档案补版本）")
    if not profile.pricing_known:
        notes.append("价格未知；资产内容不把未知价格写成免费")
    notes.append("compat_tested 只表示兼容性抽测，不等于评测证据")
    return AssetDraft(
        asset_id=asset_id_for("model_profile", profile.model_id),
        asset_type="model_profile",
        namespace=DEFAULT_NAMESPACE["model_profile"],
        version=version, content=_model_content(profile),
        lifecycle_state="candidate",
        source_refs=(f"models/registry/{profile.model_id}",),
        compatibility={"pins": {}}, missing=tuple(missing),
        notes=tuple(notes))


def capability_to_asset(cap: CapabilityEntry) -> AssetDraft:
    lifecycle = LEGACY_STATUS_MAP.get(cap.status, "draft")
    missing: List[str] = []
    notes: List[str] = [
        f"旧 status={cap.status!r} 映射为 {lifecycle!r}（三值与八态语义不等价）",
        "CapabilityEntry 无 namespace，按 domain_private 登记",
    ]
    if not cap.eval_suite:
        missing.append("缺少评测套件引用（eval_suite 为空）")
    deps = []
    for dep in cap.dependencies:
        missing.append(f"依赖 {dep} 缺少精确版本，未写入 dependency_refs")
    return AssetDraft(
        asset_id=asset_id_for("skill", cap.capability_id),
        asset_type="skill", namespace=DEFAULT_NAMESPACE["skill"],
        version=cap.version, content={
            "capability_id": cap.capability_id,
            "version": cap.version,
            "domain": cap.domain,
            "description": cap.description,
            "input_contract": cap.input_contract,
            "output_contract": cap.output_contract,
            "applicable": list(cap.applicable),
            "not_applicable": list(cap.not_applicable),
            "dependencies": list(cap.dependencies),
            "compatible_models": list(cap.compatible_models),
            "tool_ids": list(cap.tool_ids),
            "eval_suite": cap.eval_suite,
            "permission_requirements": list(cap.permission_requirements),
            "budget_policy": cap.budget_policy,
            "legacy_status": cap.status,
        },
        lifecycle_state=lifecycle,
        source_refs=(f"experts/capabilities/{cap.capability_id}",),
        dependency_refs=tuple(deps),
        compatibility={"pins": {}}, missing=tuple(missing),
        notes=tuple(notes))


def map_inventory(experts: Iterable[Expert] = (),
                  workflows: Iterable[WorkflowDef] = (),
                  models: Iterable[ModelProfile] = (),
                  capabilities: Iterable[CapabilityEntry] = ()
                  ) -> List[AssetDraft]:
    """把当前清单映射为 AssetDraft 列表（按类型/ID 排序，结果确定）。"""
    out: List[AssetDraft] = []
    out.extend(expert_to_asset(e) for e in experts)
    out.extend(workflow_to_asset(w) for w in workflows)
    out.extend(model_profile_to_asset(m) for m in models)
    out.extend(capability_to_asset(c) for c in capabilities)
    out.sort(key=lambda d: (d.asset_type, d.asset_id, d.version))
    return out
