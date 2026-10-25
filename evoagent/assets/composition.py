"""Validated linear composition drafts, backed by exact repository assets.

Composition never grants execution or promotes the new asset: callers still
need evaluation/lifecycle approval and RuntimeAssetBinder for runtime pins.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple
from .entity import AssetError, AssetRef
from .repository import AssetRepository
from .content import content_digest
from .consumption import CONSUMABLE_STATES


class ComposeError(ValueError):
    """Missing dependencies, incompatible interfaces or invalid composition."""


@dataclass(frozen=True)
class AssetIO:
    required_inputs: Tuple[str, ...] = ()
    produced_outputs: Tuple[str, ...] = ()
    input_types: Dict[str, str] = field(default_factory=dict)
    output_types: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        for key in ("required_inputs", "produced_outputs"):
            values = getattr(self, key)
            if not isinstance(values, (list, tuple)) or any(
                not isinstance(v, str) or not v.strip() for v in values
            ):
                raise ComposeError("接口字段必须是非空字符串数组")
            if len(values) != len(set(values)):
                raise ComposeError("接口字段不能重复")
            object.__setattr__(self, key, tuple(values))
        for key, names in (
            ("input_types", self.required_inputs),
            ("output_types", self.produced_outputs),
        ):
            mapping = getattr(self, key)
            if (
                not isinstance(mapping, dict)
                or set(mapping) - set(names)
                or any(
                    not isinstance(v, str) or not v.strip() for v in mapping.values()
                )
            ):
                raise ComposeError("接口类型声明无效")
            object.__setattr__(self, key, dict(mapping))


@dataclass(frozen=True)
class ComposableAsset:
    asset_ref: str
    io: AssetIO = field(default_factory=AssetIO)
    dependency_refs: Tuple[str, ...] = ()
    composed_from: Tuple[str, ...] = ()
    content_digest: str = ""
    dependency_digests: Dict[str, str] = field(default_factory=dict)


def _get(repo, ref):
    try:
        asset = repo.get_exact(ref)
        if content_digest(asset.content) != asset.content_digest:
            raise ComposeError("依赖正文与摘要不一致: " + ref)
        return asset
    except AssetError as exc:
        raise ComposeError("依赖不存在或无效: " + str(ref)) from exc


def dependency_closure(repo: AssetRepository, asset_ref: str) -> List[str]:
    """Topological exact-version closure; cycles are rejected explicitly."""
    states = {}
    order = []
    stack = [(asset_ref, False)]
    while stack:
        ref, exit_node = stack.pop()
        if exit_node:
            states[ref] = 2
            order.append(ref)
            continue
        if states.get(ref) == 2:
            continue
        if states.get(ref) == 1:
            raise ComposeError("依赖成环: " + ref)
        asset = _get(repo, ref)
        states[ref] = 1
        stack.append((ref, True))
        for dep in reversed(sorted(asset.dependency_refs)):
            stack.append((dep, False))
    return order


def check_io_compatibility(upstream: AssetIO, downstream: AssetIO) -> List[str]:
    problems = []
    for name in downstream.required_inputs:
        if name not in upstream.produced_outputs:
            problems.append("下游需要输入 '" + name + "' 但上游不产出")
            continue
        up_type = upstream.output_types.get(name)
        down_type = downstream.input_types.get(name)
        if down_type and up_type != down_type:
            problems.append("'" + name + "' 类型不兼容或上游类型未声明")
    return problems


def _io(asset):
    body = asset.content
    if not isinstance(body, dict):
        raise ComposeError("组合接口载荷必须是对象")
    return AssetIO(
        required_inputs=body.get("required_inputs", ()),
        produced_outputs=body.get("produced_outputs", ()),
        input_types=body.get("input_types", {}),
        output_types=body.get("output_types", {}),
    )


def compose(
    repo: AssetRepository,
    asset_refs: List[str],
    *,
    new_asset_id: str,
    new_version: str,
    io_overrides: Optional[AssetIO] = None,
    namespace: str = "project",
) -> ComposableAsset:
    """Check a linear chain and register an immutable draft, never an active asset."""
    if (
        not isinstance(asset_refs, list)
        or not asset_refs
        or any(not isinstance(r, str) for r in asset_refs)
    ):
        raise ComposeError("组合至少需要一个精确资产引用数组")
    if len(set(asset_refs)) != len(asset_refs):
        raise ComposeError("组合包含重复资产引用")
    if any(
        not isinstance(v, str) or not v.strip() for v in (new_asset_id, new_version)
    ):
        raise ComposeError("组合资产身份不能为空")
    try:
        target = AssetRef(new_asset_id, new_version)
    except AssetError as exc:
        raise ComposeError("组合资产身份无效") from exc
    with repo._locked():
        all_deps = set()
        for ref in asset_refs:
            all_deps.update(dependency_closure(repo, ref))
        if str(target) in all_deps:
            raise ComposeError("组合不能覆盖其来源资产")
        assets = {ref: _get(repo, ref) for ref in sorted(all_deps)}
        for ref, asset in assets.items():
            if (
                asset.lifecycle_state not in CONSUMABLE_STATES
                or not asset.evaluation_evidence_refs
            ):
                raise ComposeError("依赖未经验证或不可消费: " + ref)
        interfaces = [_io(assets[ref]) for ref in asset_refs]
        for up, down in zip(interfaces, interfaces[1:]):
            problems = check_io_compatibility(up, down)
            if problems:
                raise ComposeError("; ".join(problems))
        combined = AssetIO(
            interfaces[0].required_inputs,
            interfaces[-1].produced_outputs,
            interfaces[0].input_types,
            interfaces[-1].output_types,
        )
        if io_overrides is not None and io_overrides != combined:
            raise ComposeError("接口覆盖不能绕过或伪造真实组合接口")
        pins = {ref: asset.content_digest for ref, asset in assets.items()}
        body = {
            **asdict(combined),
            "kind": "linear_composition",
            "composed_from": list(asset_refs),
            "dependency_digests": pins,
        }
        try:
            saved = repo.register(
                asset_id=target.asset_id,
                asset_type="skill",
                namespace=namespace,
                version=target.version,
                content=body,
                dependency_refs=tuple(sorted(all_deps)),
                source_refs=tuple(asset_refs),
            )
        except AssetError as exc:
            raise ComposeError("组合登记失败: " + str(exc)) from exc
        return ComposableAsset(
            str(saved.ref),
            combined,
            tuple(sorted(all_deps)),
            tuple(asset_refs),
            saved.content_digest,
            dict(pins),
        )
