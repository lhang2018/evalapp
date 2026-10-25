"""发布通道、更新控制与回滚体验（ALN-29-E）。

复用已接受的发布切片（`ReleaseManager` 的签名/校验/部署/回滚/退役），
补用户可见的三件事：

1. **稳定 / 实验版本**：发布清单自带 `channel`；安装实例选择 `stable`
   或 `experimental`；稳定通道**不会**收到实验版本，实验通道需用户显式
   开启（`allow_experimental`）；可固定某个版本。
2. **更新控制**：`evaluate_update` 给出可解释的「装 / 不装 / 回滚」决定——
   拒绝坏包（签名/摘要校验失败，错误逐条可见）、拒绝降级（除非显式回滚）、
   拒绝已退役版本、遵守固定版本；**不做无约束热切换**：任何切换都返回
   `activation.requires_restart=True` 与「需重新绑定发布身份」的说明。
3. **真实回滚结果**：`rollback_with_evidence` 调 `ReleaseManager.rollback`，
   返回回滚目标版本/摘要/签名校验结果，并核对「回滚证据与目标版本绑定同一
   版本」（`same_version_binding`）；历史决定可查。

本模块只做通道/偏好/决定编排，不重写签名、部署、回滚与校验实现。
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .delivery import _version_tuple
from .manager import ReleaseManager

CHANNELS = ("stable", "experimental")
HOT_SWITCH_NOTE = ("运行绑定不可无约束热切换：切换版本需重启应用并重新绑定"
                   "该版本的发布身份（签名/摘要）后才生效")
DEFAULT_NOTES = [
    "稳定版本经过评测与门禁，默认只接收稳定通道更新",
    "实验版本可能不稳定，需显式开启；出问题可随时回滚",
    "更新前会校验发布签名与制品摘要；坏包会被拒绝并显示原因",
]


class ChannelError(ValueError):
    """通道/偏好/决定请求非法（空安装 ID、未知通道、缺理由等）。"""


@dataclass
class UpdatePreference:
    installation_id: str
    channel: str = "stable"
    auto_update: bool = True
    allow_experimental: bool = False
    pinned_release_id: Optional[str] = None
    actor: str = ""
    reason: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChannelError(label + "不得为空")
    return value.strip()


class ReleaseChannelService:
    def __init__(self, manager: ReleaseManager, *,
                 state_path: Optional[str] = None, clock=None):
        self.manager = manager
        self.state_path = Path(state_path) if state_path else None
        self.clock = clock or time.time
        self.preferences: Dict[str, UpdatePreference] = {}
        self.decisions: List[Dict[str, Any]] = []
        if self.state_path is not None and self.state_path.exists():
            self._load()

    # ---- 持久化（可选文件，不新增数据库迁移）----
    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in data.get("preferences") or []:
            pref = UpdatePreference(**{k: item[k] for k in
                                       UpdatePreference.__dataclass_fields__
                                       if k in item})
            self.preferences[pref.installation_id] = pref
        self.decisions = list(data.get("decisions") or [])

    def _save(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(
            {"preferences": [p.to_dict() for p in self.preferences.values()],
             "decisions": self.decisions[-200:]},
            ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 偏好 ----
    def preference(self, installation_id: str) -> Dict[str, Any]:
        iid = _require_text(installation_id, "installation_id")
        pref = self.preferences.get(iid) or UpdatePreference(installation_id=iid)
        return pref.to_dict()

    def set_preference(self, installation_id: str, *, channel: str,
                       auto_update: bool = True, allow_experimental: bool = False,
                       pinned_release_id: Optional[str] = None,
                       actor: str, reason: str) -> Dict[str, Any]:
        iid = _require_text(installation_id, "installation_id")
        if channel not in CHANNELS:
            raise ChannelError("未知通道: " + str(channel))
        if channel == "experimental" and not allow_experimental:
            raise ChannelError("选择实验通道必须显式开启 allow_experimental")
        actor = _require_text(actor, "actor")
        reason = _require_text(reason, "reason")
        pinned = (pinned_release_id or "").strip() or None
        if pinned is not None:
            manifest = self.manager.manifests.get(pinned)
            if manifest is None:
                raise ChannelError("固定版本不存在: " + pinned)
            if self.manager.is_retired(pinned):
                raise ChannelError("不能固定已退役版本: " + pinned)
        pref = UpdatePreference(
            installation_id=iid, channel=channel,
            auto_update=bool(auto_update),
            allow_experimental=bool(allow_experimental),
            pinned_release_id=pinned, actor=actor, reason=reason,
            updated_at=self.clock())
        self.preferences[iid] = pref
        self._save()
        return {"ok": True, "preference": pref.to_dict(),
                "explanation": self.explain(iid)}

    # ---- 用户可读解释 ----
    def _channel_versions(self, channel: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for manifest in self.manager.manifests.values():
            if self.manager.is_retired(manifest.release_id):
                continue
            if channel == "stable" and manifest.channel != "stable":
                continue
            rows.append({"release_id": manifest.release_id,
                         "version": manifest.version,
                         "channel": manifest.channel,
                         "artifact_digest": manifest.artifact_digest})
        rows.sort(key=lambda row: (_version_tuple(row["version"]) or (0,),
                                   row.get("release_id") or ""), reverse=True)
        return rows

    def _current(self) -> Dict[str, Any]:
        release_id = self.manager.current_release_id
        manifest = (self.manager.manifests.get(release_id)
                    if release_id else None)
        return {"release_id": release_id,
                "version": manifest.version if manifest else None,
                "channel": manifest.channel if manifest else None}

    def explain(self, installation_id: str) -> Dict[str, Any]:
        iid = _require_text(installation_id, "installation_id")
        pref = self.preferences.get(iid) or UpdatePreference(installation_id=iid)
        current = self._current()
        rollback = None
        if self.manager.rollback_records:
            last = self.manager.rollback_records[-1]
            target = self.manager.manifests.get(last.get("to"))
            check = (self.manager.verify(last["to"])
                     if last.get("to") else {"ok": False})
            rollback = {"from": last.get("from"), "to": last.get("to"),
                        "reason": last.get("reason"), "at": last.get("at"),
                        "target_version": target.version if target else None,
                        "verified": bool(check.get("ok")),
                        "reasons": check.get("errors") or []}
        notes = list(DEFAULT_NOTES)
        if pref.pinned_release_id:
            notes.append("当前已固定版本 " + pref.pinned_release_id)
        if not pref.auto_update:
            notes.append("自动更新已关闭，仅在用户确认后更新")
        if pref.channel == "experimental":
            notes.append("当前为实验通道：可能收到不稳定版本")
        return {"installation_id": iid,
                "channel": pref.channel,
                "auto_update": pref.auto_update,
                "allow_experimental": pref.allow_experimental,
                "pinned_release_id": pref.pinned_release_id,
                "current": current,
                "stable_versions": self._channel_versions("stable"),
                "experimental_versions": self._channel_versions("experimental"),
                "latest_rollback": rollback,
                "notes": notes}

    # ---- 更新决定 ----
    def _refuse(self, release_id: str, reason: str,
                **extra: Any) -> Dict[str, Any]:
        return {"ok": False, "decision": "refuse", "release_id": release_id,
                "reasons": [reason], **extra}

    def evaluate_update(self, installation_id: str, release_id: str, *,
                        as_rollback: bool = False,
                        now: Optional[float] = None) -> Dict[str, Any]:
        iid = _require_text(installation_id, "installation_id")
        pref = self.preferences.get(iid) or UpdatePreference(installation_id=iid)
        manifest = self.manager.manifests.get(release_id)
        if manifest is None:
            return self._refuse(release_id, "发布不存在，拒绝更新")
        if self.manager.is_retired(release_id):
            return self._refuse(release_id, "已退役版本不参与更新或回滚")
        if manifest.channel != "stable" and pref.channel == "stable":
            return self._refuse(
                release_id,
                "稳定通道安装不会收到实验版本（如需体验请显式切换到实验通道）")
        if manifest.channel == "experimental" and not pref.allow_experimental:
            return self._refuse(release_id, "未开启实验版本，拒绝更新")
        if pref.pinned_release_id and pref.pinned_release_id != release_id:
            return self._refuse(
                release_id,
                f"已固定版本 {pref.pinned_release_id}，拒绝切换到其他版本")
        current = self._current()
        current_tuple = _version_tuple(current["version"] or "")
        target_tuple = _version_tuple(manifest.version)
        if (current_tuple is not None and target_tuple is not None
                and target_tuple < current_tuple and not as_rollback):
            return self._refuse(
                release_id,
                "禁止无约束降级：如需回退请走回滚入口（as_rollback=True）")
        check = self.manager.verify(release_id)
        if not check.get("ok"):
            return self._refuse(
                release_id, "坏包/签名或摘要不匹配，拒绝更新",
                verification={"ok": False, "errors": check.get("errors") or [],
                              "artifact_digest": check.get("artifact_digest"),
                              "key_id": check.get("key_id")})
        if self.manager.current_release_id == release_id:
            return {"ok": True, "decision": "already_active",
                    "release_id": release_id,
                    "reasons": ["该版本已是当前运行版本，无需更新"],
                    "release": {"version": manifest.version,
                                "channel": manifest.channel},
                    "activation": {"requires_restart": False}}
        decision = "rollback" if as_rollback else "install"
        record = {"installation_id": iid, "release_id": release_id,
                  "decision": decision, "ok": True,
                  "at": now if now is not None else self.clock()}
        self.decisions.append(record)
        self._save()
        return {"ok": True, "decision": decision, "release_id": release_id,
                "reasons": [
                    "版本签名与摘要校验通过",
                    "实验版本" if manifest.channel != "stable" else "稳定版本"],
                "release": {"version": manifest.version,
                            "channel": manifest.channel,
                            "artifact_digest": manifest.artifact_digest},
                "verification": {"ok": True,
                                 "artifact_digest": check.get("artifact_digest"),
                                 "key_id": check.get("key_id")},
                "activation": {"requires_restart": True,
                               "must_rebind_release_identity": True,
                               "note": HOT_SWITCH_NOTE}}

    # ---- 回滚（真实结果 + 证据绑定）----
    def rollback_with_evidence(self, installation_id: str, *,
                               reason: str, actor: str = "api:user") -> Dict[str, Any]:
        iid = _require_text(installation_id, "installation_id")
        reason = _require_text(reason, "reason")
        actor = _require_text(actor, "actor")
        result = self.manager.rollback(reason)
        self.decisions.append({"installation_id": iid,
                               "decision": "rollback",
                               "ok": bool(result.get("ok")),
                               "at": self.clock(),
                               "to": result.get("to"),
                               "reason": reason})
        self._save()
        if not result.get("ok"):
            return {"ok": False, "rollback": result,
                    "reasons": [result.get("error") or "回滚失败"]}
        target_id = result["to"]
        manifest = self.manager.manifests[target_id]
        check = self.manager.verify(target_id)
        evidence = {
            "rollback": result,
            "target": {"release_id": target_id,
                       "version": manifest.version,
                       "channel": manifest.channel,
                       "artifact_digest": manifest.artifact_digest},
            "verification": {"ok": bool(check.get("ok")),
                             "artifact_digest": check.get("artifact_digest"),
                             "key_id": check.get("key_id"),
                             "signature_ok": check.get("signature_ok"),
                             "errors": check.get("errors") or []},
            "same_version_binding": (check.get("artifact_digest")
                                     == manifest.artifact_digest),
            "irreversible_warning": result.get("irreversible_warning"),
            "actor": actor,
            "explanation": self.explain(iid),
        }
        return {"ok": True, "evidence": evidence}

    def history(self, installation_id: str, *, limit: int = 20) -> Dict[str, Any]:
        iid = _require_text(installation_id, "installation_id")
        rows = [item for item in self.decisions if item.get("installation_id") == iid]
        return {"installation_id": iid, "decisions": rows[-max(1, limit):]}
