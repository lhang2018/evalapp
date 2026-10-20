"""版本对账与应用结果回执（双端协同 §5/§7/§10）。

服务端只判断“该安装实例是否兼容、是否在灰度范围内、应拿到哪个已发布版本”，
并记录客户端下载/校验/激活/回滚结果。候选生成、评测与发布权仍属于各自控制器，
本模块不能改变发布状态（Arch 13.4）。
"""

import hashlib
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Set

PLATFORMS = ("android", "ios")
ACTIONS = ("download_started", "verified", "activation_pending", "activated",
           "activation_failed", "health_check", "rolled_back")
SLOT_KINDS = {"capability", "workflow", "model_adapter"}
APP_VERSION_RE = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


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


class ReleaseDelivery:
    def __init__(self, releases: Any, store: Any,
                 tool_ids: Optional[Set[str]] = None):
        self.releases = releases
        self.store = store
        self.tool_ids = set(tool_ids or ())

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
            }

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

    # ---- 应用结果回执（双端协同 §5.3 / §10.1）----
    def record_apply(self, *, installation_id: str, action: str,
                     release_id: Optional[str] = None,
                     outcome: str = "ok", reason: str = "",
                     to_release_id: Optional[str] = None,
                     apply_id: Optional[str] = None,
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

        if action == "activated":
            manifest = self.releases.manifests.get(resolved_release)
            if manifest is None:
                raise ValueError("激活目标版本不存在")
            if current:
                data["last_known_good_release_id"] = current
            data["last_activated_at"] = time.time()
            current, target, status = resolved_release, None, "activated"
        elif action == "rolled_back":
            rollback = to_release_id or data.get("last_known_good_release_id")
            if not rollback:
                return {"ok": False, "error": "没有可用回滚目标",
                        "installation": installation}
            if rollback not in self.releases.manifests:
                return {"ok": False, "error": "回滚目标版本不存在",
                        "installation": installation}
            data["last_rollback_reason"] = reason
            data["last_rolled_back_at"] = time.time()
            current, target, status = rollback, None, "rolled_back"
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
