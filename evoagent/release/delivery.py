"""版本对账与应用结果回执（双端协同 §5/§7/§10）。

服务端只判断“该安装实例是否兼容、是否在灰度范围内、应拿到哪个已发布版本”，
并记录客户端下载/校验/激活/回滚结果。候选生成、评测与发布权仍属于各自控制器，
本模块不能改变发布状态（Arch 13.4）。

ALN-11-P 增量：
- ACTIONS 增加 ``retired``：安装实例退役某个已发布版本的回执；
- 回执绑定生产签名身份：``verified``/``activated`` 等动作按服务端清单解析
  ``artifact_signing_key_id`` 并核对 IdentityRegistry（未登记/已撤销/过期
  一律拒绝），回执记录解析后的身份状态，不采信客户端自报密钥；
- reconcile 增加退役检查与签名身份信任检查，两者失败都记 blocked。
"""

import hashlib
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Set

PLATFORMS = ("android", "ios", "windows")
ACTIONS = ("download_started", "verified", "activation_pending", "activated",
           "activation_failed", "health_check", "rolled_back", "retired")
# 这些动作意味着“该版本将被/已被安装实例使用”，必须绑定可用的生产签名身份
TRUST_BOUND_ACTIONS = frozenset({
    "verified", "activation_pending", "activated", "rolled_back",
})
SLOT_KINDS = {"capability", "workflow", "model_adapter"}
APP_VERSION_RE = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
# 只同步已通过评测的候选阶段；评测前的假设与证据属于服务端内部状态
CANDIDATE_SYNC_STATUSES = frozenset({
    "APPROVAL_PENDING", "RELEASE_READY", "DEPLOYING", "MONITORING",
    "PROMOTED", "ROLLED_BACK",
})


def _version_tuple(raw: str) -> Optional[tuple]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    match = APP_VERSION_RE.match(raw.strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def gray_bucket(installation_id: str) -> int:
    """安装实例在灰度中的稳定分桶（0-99）；同一实例结果可重放。"""
    digest = hashlib.sha256(installation_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def _candidate_summary(cand: Any, status: str) -> Dict[str, Any]:
    log = list(getattr(cand, "transition_log", []) or [])
    release_id = ""
    for entry in log:
        evidence = entry.get("evidence") or {}
        if isinstance(evidence, dict) and evidence.get("release_id"):
            release_id = str(evidence["release_id"])
    updated_at = float(getattr(cand, "created_at", 0) or 0)
    if log:
        updated_at = float(log[-1].get("at") or updated_at)
    return {
        "candidate_id": getattr(cand, "candidate_id", ""),
        "status": status,
        "target_component": getattr(cand, "target_component", ""),
        "target_domain": getattr(cand, "target_domain", ""),
        "risk_level": getattr(cand, "risk_level", ""),
        "publish_suggestion": getattr(cand, "publish_suggestion", ""),
        "release_id": release_id or None,
        "created_at": getattr(cand, "created_at", None),
        "updated_at": updated_at,
    }


class ReleaseDelivery:
    def __init__(self, releases: Any, store: Any,
                 tool_ids: Optional[Set[str]] = None,
                 identity_registry: Optional[Any] = None):
        self.releases = releases
        self.store = store
        self.tool_ids = set(tool_ids or ())
        # 生产身份登记簿（ALN-03-P identity.py）；None = 未接入信任根，
        # 此时跳过身份核对，保持既有部署行为不变。
        self.identity_registry = identity_registry

    def set_identity_registry(self, registry: Optional[Any]) -> None:
        """接入/更换生产身份登记簿（供宿主在装配期注入）。"""
        self.identity_registry = registry

    # ---- 生产签名身份核对（ALN-11-P）----
    def identity_status(self, key_id: Optional[str]) -> str:
        """服务端视角的签名身份状态。

        返回值：``usable`` / ``unregistered`` / ``revoked`` /
        ``not_yet_valid`` / ``expired`` / ``no_registry`` / ``unknown_key``。
        """
        if not key_id:
            return "unknown_key"
        registry = self.identity_registry
        if registry is None:
            return "no_registry"
        identity = registry.get(key_id)
        if identity is None:
            return "unregistered"
        if getattr(identity, "revoked", False):
            return "revoked"
        if hasattr(identity, "usable"):
            try:
                if identity.usable():
                    return "usable"
            except Exception:  # 登记簿自身异常按不可信处理
                return "unregistered"
        now = time.time()
        not_before = getattr(identity, "not_before", None)
        not_after = getattr(identity, "not_after", None)
        if not_before is not None and now < not_before:
            return "not_yet_valid"
        if not_after is not None and now >= not_after:
            return "expired"
        return "usable"

    # ---- 版本对账（双端协同 §5.2 / §10.1）----
    def reconcile(self, *, installation_id: str, device_id: str = "",
                  platform: str = "android", app_version: str = "",
                  current_release_id: Optional[str] = None,
                  tool_contracts: Optional[List[str]] = None,
                  ) -> Dict[str, Any]:
        installation_id = (installation_id or "").strip()
        if not installation_id:
            raise ValueError("缺少 installation_id")
        platform = (platform or "").strip().lower()
        if platform not in PLATFORMS:
            raise ValueError(f"不支持的平台: {platform or '-'}")
        if not (app_version or "").strip():
            raise ValueError("缺少 app_version")

        previous = self.store.get_installation(installation_id) or {}
        previous_data = previous.get("data") or {}
        current = (current_release_id or previous.get("current_release_id")
                   or None)
        manifest = self._active_manifest()
        checks: List[Dict[str, Any]] = []
        reasons: List[str] = []

        def check(name: str, ok: bool, detail: str = "") -> bool:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})
            if not ok:
                reasons.append(f"{name}: {detail}" if detail else name)
            return bool(ok)

        check("release_available", manifest is not None,
              "" if manifest else "尚未发布任何版本")
        if manifest is not None:
            retired = bool(hasattr(self.releases, "is_retired")
                           and self.releases.is_retired(manifest.release_id))
            check("release_not_retired", not retired,
                  "当前发布版本已退役，停止作为更新目标下发")
            check("compatibility_declared",
                  self.releases.compatibility_complete(manifest),
                  "发布兼容声明必须完整")
            allowed = list(manifest.target_platforms or [])
            check("platform_compatible", not allowed or platform in allowed,
                  f"适用平台 {allowed or ['全部']}，当前 {platform}")

            required = _version_tuple(manifest.min_app_version or "0.0.0")
            actual = _version_tuple(app_version)
            check("min_app_version",
                  required is not None and actual is not None and
                  actual >= required,
                  f"要求 >= {manifest.min_app_version or '0.0.0'}，"
                  f"当前 {app_version}")

            available = set(self.tool_ids)
            for item in tool_contracts or []:
                if isinstance(item, str) and item.strip():
                    available.add(item.strip())
            missing = sorted(set(manifest.required_tool_contracts or []) -
                             available)
            check("tool_contracts", not missing,
                  "工具契约缺失: " + ", ".join(missing) if missing
                  else "工具契约满足")

            verdict = self.releases.verify(manifest.release_id)
            check("artifact_integrity", bool(verdict.get("ok")),
                  "签名与制品摘要校验通过" if verdict.get("ok")
                  else verdict.get("error") or "签名或制品摘要校验失败")
            identity_status = self.identity_status(
                manifest.artifact_signing_key_id)
            # no_registry = 服务端未接入身份登记簿：不静默放行也不误伤
            # 既有部署——显式记录该状态；制品字节的强制验签关卡在端侧
            # TrustedKeyRing（03-P 架构），登记簿接入后撤销/过期即阻止。
            check("signing_identity_trusted",
                  manifest.artifact_signing_key_id is None
                  or identity_status in ("usable", "no_registry"),
                  f"制品签名身份状态: {identity_status}"
                  if identity_status == "usable"
                  else ("服务端未接入身份登记簿，端侧验签为强制关卡"
                        if identity_status == "no_registry"
                        else f"制品签名身份不可信: {identity_status}"))
            if manifest.artifact_url:
                # 声明了可下载制品就必须同时给出端侧可独立验签的完整
                # envelope；缺任一项则不得下发为可更新目标。
                complete = bool(
                    manifest.artifact_sha256
                    and manifest.artifact_size is not None
                    and manifest.artifact_signature
                    and manifest.artifact_signing_key_id)
                check("artifact_envelope", complete,
                      "制品 URL/摘要/大小/签名/密钥齐全" if complete
                      else "制品交付 envelope 不完整")

        gray_percent = self._gray_percent(manifest)
        eligible = manifest is not None and \
            gray_bucket(installation_id) < gray_percent

        if manifest is None or any(not c["ok"] for c in checks):
            decision = "blocked"
        elif current == manifest.release_id:
            decision = "up_to_date"
        elif not eligible:
            decision = "not_eligible"
        else:
            decision = "update_available"

        rollback_target = (self._rollback_target(manifest)
                           if manifest is not None else None)
        target = None
        if manifest is not None and decision in (
                "update_available", "up_to_date"):
            target = {
                "release_id": manifest.release_id,
                "version": manifest.version,
                "artifact_digest": manifest.artifact_digest,
                "signed_by": manifest.signed_by,
                "signature_ok": bool(
                    self.releases.verify(manifest.release_id).get("ok")),
                "signing_identity_status": identity_status,
                "rollback_target": rollback_target,
                "min_app_version": manifest.min_app_version or "0.0.0",
                "channel": manifest.channel or "stable",
                "bundle_kind": manifest.bundle_kind or "capability",
                "delivery_mode": ("capability_slot"
                                  if (manifest.bundle_kind or "capability")
                                  in SLOT_KINDS else "client_binary"),
                "gray_percent": gray_percent,
                "created_at": manifest.created_at,
                "eval_report_available": manifest.eval_report is not None,
                # 冻结资产引用只下发给端侧做字节/摘要验签；服务端不宣称
                # 端侧已完成安装，也不把服务端 HMAC 秘密交给客户端。
                "asset_refs": list(manifest.asset_refs or []),
                "asset_refs_require_client_verification": bool(
                    manifest.asset_refs),
                "required_permissions": list(
                    manifest.permission_requirements or []),
                "required_tool_contracts": list(
                    manifest.required_tool_contracts or []),
            }
            if manifest.artifact_url:
                target.update({
                    "artifact_url": manifest.artifact_url,
                    "artifact_size": manifest.artifact_size,
                    "artifact_sha256": manifest.artifact_sha256,
                    "artifact_signature": manifest.artifact_signature,
                    "signing_key_id": manifest.artifact_signing_key_id,
                    "signature_schema": (manifest.signature_schema
                                         or "evoagent.release.signing/1"),
                })

        data = dict(previous_data)
        data.update({
            "decision": decision,
            "checks": checks,
            "reasons": reasons,
            "gray_percent": gray_percent,
            "last_seen_app_version": app_version,
            "last_seen_platform": platform,
            "last_reconciled_at": time.time(),
        })
        if target is not None:
            data["target_release_id"] = target["release_id"]
        self.store.save_installation(
            installation_id, device_id or previous.get("device_id") or "",
            platform, app_version, current,
            target["release_id"] if target is not None else None,
            decision, data)

        installation = self.store.get_installation(installation_id) or {}
        return {
            "installation_id": installation_id,
            "device_id": installation.get("device_id") or device_id,
            "decision": decision,
            "update": {
                "available": decision == "update_available",
                "target_release_id": target["release_id"] if target else None,
                "in_gray_scope": eligible,
                "gray_percent": gray_percent,
            },
            "checks": checks,
            "reasons": reasons,
            "current_release_id": current,
            "target": target,
            "server_time": time.time(),
        }

    # ---- 候选状态同步（双端协同 §2/§8）----
    def candidate_states(self, *, installation_id: str,
                         candidates: Any, limit: int = 20) -> Dict[str, Any]:
        """返回该安装实例可见的候选阶段摘要，并记录已下发状态。

        只暴露评测之后的状态；假设、证据与评测细节留在服务端。候选状态
        不改变发布与激活路径——App 仍必须走 reconcile/manifest 门禁。
        """
        installation_id = (installation_id or "").strip()
        if not installation_id:
            raise ValueError("缺少 installation_id")
        installation = self.store.get_installation(installation_id)
        if installation is None:
            raise KeyError("安装实例不存在")
        limit = max(1, min(int(limit), 100))
        visible: List[Dict[str, Any]] = []
        for cand in candidates or []:
            status = (getattr(cand.status, "value", None)
                      or str(getattr(cand, "status", "")))
            if status not in CANDIDATE_SYNC_STATUSES:
                continue
            summary = _candidate_summary(cand, status)
            visible.append(summary)
        visible.sort(key=lambda item: (item["updated_at"], item["candidate_id"]),
                     reverse=True)
        visible = visible[:limit]
        for item in visible:
            self.store.record_device_candidate_state(
                installation_id, item["candidate_id"], item["status"],
                item["release_id"] or "", item)
        return {
            "installation_id": installation_id,
            "current_release_id": installation.get("current_release_id"),
            "target_release_id": installation.get("target_release_id"),
            "installation_status": installation.get("status"),
            "candidates": visible,
            "server_time": time.time(),
        }

    # ---- 应用结果回执（双端协同 §5.3 / §10.1）----
    def record_apply(self, *, installation_id: str, action: str,
                     release_id: Optional[str] = None,
                     outcome: str = "ok", reason: str = "",
                     to_release_id: Optional[str] = None,
                     apply_id: Optional[str] = None,
                     signing_key_id: Optional[str] = None,
                     ) -> Dict[str, Any]:
        installation = self.store.get_installation(
            (installation_id or "").strip())
        if installation is None:
            raise KeyError("安装实例不存在")
        if action not in ACTIONS:
            raise ValueError(f"未知动作: {action}")
        if apply_id is not None and not re.match(
                r"^[A-Za-z0-9_-]{8,64}$", apply_id or ""):
            raise ValueError("非法 apply_id")
        deployment_id = f"dep_{apply_id}" if apply_id else \
            f"dep_{uuid.uuid4().hex[:12]}"
        existing = self.store.get_device_deployment(deployment_id)
        if existing is not None:
            return {"ok": True, "duplicate": True,
                    "deployment": existing,
                    "installation": installation}

        current = installation.get("current_release_id")
        target = installation.get("target_release_id")
        status = installation.get("status") or "unknown"
        data = dict(installation.get("data") or {})
        resolved_release = release_id or target or current

        # A rollback uses the destination's trust, not the failed source's.
        # Normalize it before either validation or receipt fields are built.
        if action == "rolled_back":
            resolved_release = to_release_id or data.get("last_known_good_release_id")
            if not resolved_release:
                return {"ok": False, "error": "没有可用回滚目标",
                        "installation": installation}
            if resolved_release not in self.releases.manifests:
                return {"ok": False, "error": "回滚目标版本不存在",
                        "installation": installation}

        # 回执信任绑定（ALN-11-P）：身份与制品摘要一律以服务端清单为准，
        # 不采信客户端自报值；自报密钥与清单不一致即拒绝。
        manifest = (self.releases.manifests.get(resolved_release)
                    if resolved_release else None)
        server_key_id = (getattr(manifest, "artifact_signing_key_id", None)
                         if manifest is not None else None)
        artifact_digest = (getattr(manifest, "artifact_sha256", None)
                           or getattr(manifest, "artifact_digest", None)
                           if manifest is not None else None)
        identity_status: Optional[str] = None
        if action in TRUST_BOUND_ACTIONS and hasattr(
                self.releases, "is_retired") \
                and self.releases.is_retired(resolved_release):
            # 退役版本不得被安装/激活（G6：未通过门禁的候选不能激活）
            return {"ok": False, "error": "目标版本已退役，拒绝安装回执",
                    "installation": installation}
        if action in TRUST_BOUND_ACTIONS and server_key_id:
            if signing_key_id and signing_key_id != server_key_id:
                return {"ok": False,
                        "error": "回执签名密钥与发布清单不一致",
                        "installation": installation}
            identity_status = self.identity_status(server_key_id)
            # no_registry = 尚未接入信任根：保持既有行为，仅记录状态
            if identity_status not in ("usable", "no_registry"):
                return {"ok": False,
                        "error": f"制品签名身份不可信: {identity_status}",
                        "installation": installation}

        if action == "activated":
            manifest = self.releases.manifests.get(resolved_release)
            if manifest is None:
                raise ValueError("激活目标版本不存在")
            if current:
                data["last_known_good_release_id"] = current
            data["last_activated_at"] = time.time()
            current, target, status = resolved_release, None, "activated"
        elif action == "retired":
            # 安装实例退役回执：退役当前版本时清空指针并保留已知良好版本，
            # 断联/失败不影响本地状态，仅记录待确认退役。
            if not resolved_release:
                return {"ok": False, "error": "退役回执缺少目标版本",
                        "installation": installation}
            data["retired_release_id"] = resolved_release
            data["last_retired_at"] = time.time()
            data["last_retire_reason"] = reason[:500]
            if current == resolved_release:
                current, target, status = None, None, "retired"
            elif target == resolved_release:
                target = None
        elif action == "rolled_back":
            data["last_rollback_reason"] = reason
            data["last_rolled_back_at"] = time.time()
            current, target, status = resolved_release, None, "rolled_back"
        elif action == "activation_failed":
            data["last_failure_reason"] = reason
            status = "failed"
        elif action == "health_check":
            status = "healthy" if outcome in ("ok", "healthy") else "unhealthy"
            data["last_health_check"] = {"outcome": outcome,
                                         "at": time.time()}
        elif action == "verified":
            status = "verified"
        elif action == "activation_pending":
            status = "activating"
        else:
            status = "downloading"
        data["last_apply_action"] = action

        self.store.save_installation(
            installation["installation_id"], installation.get("device_id") or "",
            installation.get("platform") or "android",
            installation.get("app_version") or "", current, target, status,
            data)
        record = {
            "action": action, "outcome": outcome, "reason": reason[:500],
            "release_id": resolved_release,
            "previous_release_id": installation.get("current_release_id"),
            "current_release_id": current,
            "at": time.time(),
        }
        # 信任绑定字段：以服务端清单解析值为准（ALN-11-P）
        if server_key_id:
            record["signing_key_id"] = server_key_id
            record["signing_identity_status"] = identity_status or \
                self.identity_status(server_key_id)
        if artifact_digest:
            record["artifact_digest"] = artifact_digest
        self.store.save_device_deployment(
            deployment_id, installation["installation_id"],
            resolved_release or "", action, outcome, record)
        return {"ok": True, "duplicate": False, "deployment_id": deployment_id,
                "deployment": record,
                "installation": self.store.get_installation(
                    installation["installation_id"])}

    # ---- 内部 ----
    def _active_manifest(self):
        rid = self.releases.current_release_id
        return self.releases.manifests.get(rid) if rid else None

    def _gray_percent(self, manifest: Any) -> int:
        if manifest is None:
            return 100
        for rec in reversed(self.releases.deployments):
            if rec.get("release_id") == manifest.release_id:
                try:
                    return max(0, min(100, int(rec.get("gray_percent", 100))))
                except (TypeError, ValueError):
                    return 100
        return 100

    def _rollback_target(self, manifest: Any) -> Optional[str]:
        if manifest is None:
            return None
        if manifest.rollback_target:
            return manifest.rollback_target
        for rec in reversed(self.releases.deployments):
            if rec.get("release_id") == manifest.release_id:
                return rec.get("previous")
        return None
