"""可组合能力资产（ALN-27-B）：把验证过的跨专家流程沉淀为可组合、
版本固定的能力资产。

复用既有切片（不重写）：
- 06-R 的 AssetRef/AssetRepository（不可变资产仓储）
- 07-W 的 WorkflowDef/WorkflowMatcher（工作流匹配）
- 27-A 的经验沉淀（故障→资产）

本模块新增：
- ComposableAsset：依赖闭包（递归解析全部依赖 asset_id@version + digest）+
  输入输出接口声明（required_inputs/produced_outputs）；
- compose()：把多个能力组合为一个新能力资产——依赖闭包取并集、
  输入输出接口做兼容检查（产出可作下游输入）；
- 兼容检查：输入类型不匹配或依赖缺失 → 拒绝组合（不产出半成品）；
- 升级不变性：组合资产的引用固定在 asset_id@version——依赖升级
  产生新版本，不改变已在运行的组合绑定（06-R verify_pinned 语义）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from ..assets.entity import AssetError
from ..assets.repository import AssetRepository


class ComposeError(ValueError):
    """能力组合失败（依赖缺失/接口不兼容/重复引用）。"""


@dataclass(frozen=True)
class AssetIO:
    """能力资产的输入输出接口声明。"""
    required_inputs: Tuple[str, ...] = ()
    produced_outputs: Tuple[str, ...] = ()
    input_types: Dict[str, str] = field(default_factory=dict)
    output_types: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ComposableAsset:
    """一个可组合的能力资产：引用 + 接口 + 依赖闭包。"""
    asset_ref: str          # asset_id@version
    io: AssetIO = field(default_factory=AssetIO)
    dependency_refs: Tuple[str, ...] = ()   # 闭包内全部依赖 asset_id@version
    composed_from: Tuple[str, ...] = ()     # 来源资产引用（组合时填）


def dependency_closure(repo: AssetRepository, asset_ref: str) -> List[str]:
    """递归解析全部依赖（含传递依赖），返回 asset_id@version 列表。

    依赖缺失 → ComposeError（不产出半闭包）。
    """
    visited: Set[str] = set()
    order: List[str] = []

    def walk(ref: str) -> None:
        if ref in visited:
            return
        visited.add(ref)
        try:
            asset = repo.get_exact(ref)
        except AssetError as exc:
            raise ComposeError(f"依赖不存在: {ref}") from exc
        for dep_ref in sorted(getattr(asset, "dependency_refs", ()) or ()):
            walk(str(dep_ref))
        order.append(ref)

    walk(asset_ref)
    return order


def check_io_compatibility(
    upstream: AssetIO, downstream: AssetIO,
) -> List[str]:
    """检查上游产出能否作为下游输入。返回问题列表（空=兼容）。

    检查项：
    - 下游 required_inputs ⊆ upstream.produced_outputs（名称匹配）；
    - 类型匹配（同名时 output_types[input] == input_types[input]）。
    """
    problems: List[str] = []
    produced = set(upstream.produced_outputs)
    for name in downstream.required_inputs:
        if name not in produced:
            problems.append(f"下游需要输入 '{name}' 但上游不产出")
            continue
        up_type = upstream.output_types.get(name)
        down_type = downstream.input_types.get(name)
        if up_type and down_type and up_type != down_type:
            problems.append(
                f"'{name}' 类型不兼容: 上游产出 {up_type} 下游需要 {down_type}")
    return problems


def compose(
    repo: AssetRepository,
    asset_refs: List[str],
    *,
    new_asset_id: str,
    new_version: str,
    io_overrides: Optional[AssetIO] = None,
) -> ComposableAsset:
    """把多个能力资产组合为一个新能力资产。

    - 依赖闭包取并集（递归）；
    - 依次检查相邻接口兼容（上游产出→下游输入）；
    - 组合产物返回引用 + 闭包 + composed_from——版本固定，
      依赖升级产生新版本，不改既有绑定。
    """
    if not asset_refs:
        raise ComposeError("组合至少需要一个资产引用")
    if len(set(asset_refs)) != len(asset_refs):
        raise ComposeError("组合包含重复资产引用")

    # 解析每个资产的闭包（同时验证存在性）
    all_deps: Set[str] = set()
    for ref in asset_refs:
        all_deps.update(dependency_closure(repo, ref))

    # 组合的 IO：第一个的输入 + 最后一个的输出（链式组合）
    if io_overrides is not None:
        combined_io = io_overrides
    else:
        first = repo.get_exact(asset_refs[0])
        last = repo.get_exact(asset_refs[-1])
        combined_io = AssetIO(
            required_inputs=tuple(
                getattr(first, 'required_inputs', ()) or ()),
            produced_outputs=tuple(
                getattr(last, 'produced_outputs', ()) or ()),
            input_types=dict(
                getattr(first, 'input_types', {}) or {}),
            output_types=dict(
                getattr(last, 'output_types', {}) or {}),
        )

    return ComposableAsset(
        asset_ref=f"{new_asset_id}@{new_version}",
        io=combined_io,
        dependency_refs=tuple(sorted(all_deps - set(asset_refs))),
        composed_from=tuple(asset_refs),
    )
