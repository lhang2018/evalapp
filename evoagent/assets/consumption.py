"""运行时精确消费绑定（ALN-06-R）。

ALN-06-A 建立了不可变资产仓储（只支持精确版本读取，没有 latest/别名）；
本模块把执行步真正绑到资产的精确 AssetRef（asset_id@version +
content_digest）上，补齐「存储与声明版本」到「执行消费版本」的最后一跳：

- 新绑定只接受可消费状态（approved/active）；retired/draft/candidate
  一律拒绝——退役资产不可被新运行绑定；
- 绑定解析依赖闭包，逐依赖同样校验状态，全部 ref/digest 记入绑定集；
- 绑定一旦写入运行清单即冻结：恢复/续跑只验证「存在 + 摘要一致」，
  不重新解析、不因资产事后退役而阻断旧运行（旧 run 版本保持固定）；
- 工作流库/专家注册表内容与绑定资产的摘要不一致 = 库漂移，拒绝执行
  （漂移检测由调用方用 digest 对比完成，本模块提供 binding 记录）。

本模块不做任何执行，不复制工具/账本权威；它是 controller 与不可变
仓储之间的绑定与校验协议。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .entity import Asset, AssetError, AssetRef
from .mapping import asset_id_for
from .repository import AssetRepository

#: 可被新运行绑定的生命周期状态（与仓储 DEPENDENCY_REQUIRED_STATES 同口径）。
CONSUMABLE_STATES = ("approved", "active")


class ConsumptionError(AssetError):
    """运行时消费绑定失败。"""


class AssetNotConsumable(ConsumptionError):
    """资产处于不可消费状态（retired/draft/candidate 等）。"""


class AssetBindingMissing(ConsumptionError):
    """旧运行绑定的资产已不存在（被回收或从未登记）。"""


@dataclass(frozen=True)
class AssetBinding:
    """一条冻结的消费绑定：执行以此 ref + digest 为准。"""

    asset_id: str
    version: str
    content_digest: str
    asset_type: str
    selected_from: str
    bound_at: float

    @property
    def ref(self) -> str:
        return f"{self.asset_id}@{self.version}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "asset_id": self.asset_id,
            "version": self.version,
            "content_digest": self.content_digest,
            "asset_type": self.asset_type,
            "selected_from": self.selected_from,
            "bound_at": self.bound_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AssetBinding":
        ref = AssetRef.parse(str(data.get("ref") or ""))
        return cls(
            asset_id=ref.asset_id, version=ref.version,
            content_digest=str(data.get("content_digest") or ""),
            asset_type=str(data.get("asset_type") or ""),
            selected_from=str(data.get("selected_from") or ""),
            bound_at=float(data.get("bound_at") or 0.0))


def _version_sort_key(version: str) -> tuple:
    """确定性版本序：数字段按数值、其余按字符串，保证多版本选择可复现。"""
    key: list = []
    for piece in str(version).replace("-", ".").replace("+", ".").split("."):
        key.append((0, int(piece), "") if piece.isdigit() else (1, 0, piece))
    return tuple(key)


class RuntimeAssetBinder:
    """controller 与不可变资产仓储之间的绑定协议（可选注入）。

    未注入时 controller 行为与既有完全一致（按 ID 查库，无资产校验）；
    注入后执行步在冻结执行清单时绑定精确版本，恢复/续跑只验证不重绑。
    """

    def __init__(self, repository: AssetRepository,
                 now: Optional[Callable[[], float]] = None):
        self.repository = repository
        self._now = now or (lambda: time.time())

    # ---- 新绑定 ----

    def bind(self, asset_type: str, local_id: str, *,
             prefer_ref: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """把本地实体（expert/workflow/...）绑定到精确资产版本。

        返回 {ref: binding_dict}，含依赖闭包；ref 即 asset_id@version。
        prefer_ref 指定时绑定该精确引用（调用方已持有版本决策）。
        """
        asset_id = asset_id_for(asset_type, local_id)
        if prefer_ref:
            parsed = AssetRef.parse(prefer_ref)
            if parsed.asset_id != asset_id:
                raise ConsumptionError(
                    f"显式引用 {prefer_ref} 与实体 {asset_id} 不一致")
            asset = self.repository.get_exact(prefer_ref)
            selected_from = "explicit_ref"
        else:
            asset, selected_from = self._select_consumable(asset_id)
        bindings: Dict[str, Dict[str, Any]] = {}
        self._bind_closure(asset, selected_from, bindings)
        return bindings

    def _select_consumable(self, asset_id: str):
        for state in ("active", "approved"):
            candidates = self.repository.versions_of(
                asset_id, lifecycle_state=state)
            if candidates:
                # 多个可消费版本：确定性取最高版本并记录选择依据
                chosen = max(candidates,
                             key=lambda a: (_version_sort_key(a.version),
                                            a.version))
                return chosen, state
        versions = self.repository.versions_of(asset_id)
        if versions:
            raise AssetNotConsumable(
                f"{asset_id} 仅有 "
                f"{sorted({v.lifecycle_state for v in versions})} 状态版本，"
                "无可消费版本（退役不可新绑定）")
        raise AssetBindingMissing(f"{asset_id} 未登记任何资产版本")

    def _bind_closure(self, asset: Asset, selected_from: str,
                      bindings: Dict[str, Dict[str, Any]]) -> None:
        self._require_consumable(asset)
        if str(asset.ref) in bindings:
            return
        bindings[str(asset.ref)] = AssetBinding(
            asset_id=asset.asset_id, version=asset.version,
            content_digest=asset.content_digest,
            asset_type=asset.asset_type, selected_from=selected_from,
            bound_at=self._now()).to_dict()
        for dep_ref in asset.dependency_refs:
            try:
                dep = self.repository.get_exact(dep_ref)
            except AssetError as exc:
                raise AssetBindingMissing(
                    f"依赖闭包缺失: {dep_ref}（{exc}）") from exc
            self._bind_closure(dep, "dependency_pin", bindings)

    @staticmethod
    def _require_consumable(asset: Asset) -> None:
        if asset.lifecycle_state not in CONSUMABLE_STATES:
            raise AssetNotConsumable(
                f"{str(asset.ref)} 状态 {asset.lifecycle_state} 不可消费；"
                "retired/draft 不可被新运行绑定")

    # ---- 旧绑定校验（恢复/续跑：不重解析、不看状态） ----

    def verify_pinned(self, bindings: Optional[Dict[str, Any]]) -> None:
        """验证既有绑定集：资产仍存在且内容摘要与绑定时不一致即拒绝。

        生命周期状态刻意不检查——资产事后退役不得阻断已绑定它的旧运行
        （旧 run 版本保持固定）。
        """
        for ref, record in (bindings or {}).items():
            binding = record if isinstance(record, AssetBinding) \
                else AssetBinding.from_dict(record)
            if binding.ref != ref:
                raise ConsumptionError(f"绑定记录身份与索引不一致: {ref}")
            try:
                asset = self.repository.get_exact(ref)
            except AssetError as exc:
                raise AssetBindingMissing(
                    f"旧运行绑定的资产已不存在: {ref}（{exc}）") from exc
            if asset.content_digest != binding.content_digest:
                raise ConsumptionError(
                    f"{ref} 内容摘要与绑定时不一致"
                    f"（{asset.content_digest[:12]}… != "
                    f"{binding.content_digest[:12]}…），拒绝续跑")
            if binding.asset_type != asset.asset_type:
                raise ConsumptionError(f"绑定资产类型不一致: {ref}")
            for dependency in asset.dependency_refs:
                if dependency not in bindings:
                    raise AssetBindingMissing(f"旧运行缺少依赖绑定: {dependency}")
