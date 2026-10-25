"""发布管理（PRD 59-63 / Architecture 13.3-13.5）。

- 发布组合清单：代码提交 + 模型适配 + 提示词 + 工具/工作流/环境/评测版本 + 制品摘要 + 回滚目标。
- 签名制品；回滚恢复兼容组合；不可逆变化需要补偿策略（Arch 13.5）。
"""

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..assets.entity import AssetError, AssetRef, AssetValidationError
from ..assets.refs import verify_manifest_ref
from . import signing

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_APP_VERSION_RE = re.compile(
    r"^[vV]?\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.-]+)?$")
_SUPPORTED_PLATFORMS = ("android", "ios")
_CHANNELS = ("stable", "draft", "internal")
# 这些字段由签名流程本身产生，不参与被签名的清单元数据。
_SIGNING_FIELDS = frozenset({
    "artifact_digest", "signed_by", "signing_payload_digest",
    "signature_version", "signing_key_id",
    # 制品 envelope 由端侧按真实字节单独验签，不进入元数据签名，
    # 避免给历史清单新增字段导致旧签名无法复验。
    "artifact_url", "artifact_sha256", "artifact_size",
    "artifact_signature", "artifact_signing_key_id", "signature_schema",
})
# ALN-03 之前的 v1 签名只覆盖这些字段；迁移时按原字节重建以验证旧签名。
_LEGACY_SIGNED_FIELDS = (
    "release_id", "version", "code_commit", "model_adapter_version",
    "prompt_version", "tools_version", "workflow_versions",
    "memory_policy_version", "env_version", "eval_dataset_version",
    "eval_report", "compatible_requirements", "target_platforms",
    "min_app_version", "required_tool_contracts", "channel", "bundle_kind",
    "asset_refs", "rollback_target", "artifact_digest", "signed_by",
    "created_at", "signature_version", "permission_requirements",
    "data_schema", "model_capabilities", "offline_support",
)


def _normalize_asset_refs(refs: Any) -> List[Dict[str, str]]:
    """把发布清单里的资产引用规范成冻结形状（三字段、无别名）。"""
    if refs is None:
        return []
    if isinstance(refs, (str, bytes)) or not isinstance(refs, (list, tuple)):
        raise ValueError("asset_refs 必须是资产引用对象数组")
    out: List[Dict[str, str]] = []
    seen: Dict[Tuple[str, str], str] = {}
    for item in refs:
        if not isinstance(item, Mapping):
            raise ValueError("asset_refs 元素必须是对象")
        try:
            ref = AssetRef(str(item.get("asset_id") or ""),
                           str(item.get("version") or ""))
        except AssetValidationError as exc:
            raise ValueError(f"非法资产引用: {exc}") from exc
        digest = item.get("content_digest")
        if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
            raise ValueError(
                f"资产引用 {ref} 缺少合法 content_digest（64 位小写十六进制）")
        key = (ref.asset_id, ref.version)
        if key in seen:
            if seen[key] != digest:
                raise ValueError(
                    f"同一资产版本出现不同摘要: {ref}")
            continue
        seen[key] = digest
        out.append({"asset_id": ref.asset_id, "version": ref.version,
                    "content_digest": digest})
    return sorted(out, key=lambda r: (r["asset_id"], r["version"]))


def _sign(payload: Dict[str, Any], secret: str) -> str:
    return hmac.new(secret.encode(), _canonical(payload), hashlib.sha256).hexdigest()


def _canonical(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _artifact_payload(manifest: "ReleaseManifest") -> Dict[str, Any]:
    """制品载荷；空 asset_refs 不下发，保证旧清单摘要不因新字段漂移。"""
    payload = manifest.to_dict()
    if not payload.get("asset_refs"):
        payload.pop("asset_refs", None)
    return payload


def _digest(manifest: "ReleaseManifest") -> str:
    payload = _artifact_payload(manifest)
    payload.pop("artifact_digest", None)
    payload.pop("signed_by", None)
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _signed_metadata(manifest: "ReleaseManifest") -> Dict[str, Any]:
    """现代签名覆盖完整清单元数据（含冻结资产引用与逐文件摘要）。"""
    return {key: value for key, value in manifest.to_dict().items()
            if key not in _SIGNING_FIELDS}


def _signing_payload(manifest: "ReleaseManifest") -> Dict[str, Any]:
    return signing.build_payload(
        _signed_metadata(manifest),
        key_id=manifest.signing_key_id or "")


def _legacy_artifact_payload(manifest: "ReleaseManifest") -> Dict[str, Any]:
    data = manifest.to_dict()
    payload = {key: data[key] for key in _LEGACY_SIGNED_FIELDS if key in data}
    if not payload.get("asset_refs"):
        payload.pop("asset_refs", None)
    return payload


def _legacy_digest(manifest: "ReleaseManifest") -> str:
    payload = _legacy_artifact_payload(manifest)
    payload.pop("artifact_digest", None)
    payload.pop("signed_by", None)
    return hashlib.sha256(_canonical(payload)).hexdigest()


@dataclass
class ReleaseManifest:
    release_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    version: str = "v0"
    code_commit: str = "local"
    model_adapter_version: str = "mock-1"
    prompt_version: str = "v0"
    tools_version: str = "builtin-1.0.0"
    workflow_versions: List[str] = field(default_factory=list)
    memory_policy_version: str = "v0"
    env_version: str = "local"
    eval_dataset_version: str = ""
    eval_report: Optional[Dict[str, Any]] = None
    compatible_requirements: List[str] = field(default_factory=list)
    # Compatibility is signed content. Legacy manifests lacking declarations
    # remain readable for history but cannot enter a stable channel.
    target_platforms: List[str] = field(default_factory=list)
    min_app_version: str = "0.0.0"
    required_tool_contracts: List[str] = field(default_factory=list)
    channel: str = "stable"
    bundle_kind: str = "capability"
    # 冻结资产引用：asset_id + version + content_digest，永不写“最新版本”。
    asset_refs: List[Dict[str, str]] = field(default_factory=list)
    # 逐文件制品摘要（相对制品包根目录的 POSIX 路径 + 大小 + sha256）。
    artifact_files: List[Dict[str, Any]] = field(default_factory=list)
    rollback_target: Optional[str] = None
    rollback_supported: Optional[bool] = None
    artifact_digest: Optional[str] = None
    # 真实制品交付 envelope：客户端按 url 分块下载后，用受信公钥验证
    # 实际字节的 sha256/大小与签名；与元数据 HMAC 签名分开命名，互不覆盖。
    artifact_url: Optional[str] = None
    artifact_sha256: Optional[str] = None
    artifact_size: Optional[int] = None
    artifact_signature: Optional[str] = None
    artifact_signing_key_id: Optional[str] = None
    signature_schema: Optional[str] = None
    signed_by: Optional[str] = None
    signing_key_id: Optional[str] = None
    signing_payload_digest: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    signature_version: str = signing.SIGNATURE_VERSION
    permission_requirements: Optional[List[str]] = None
    data_schema: Optional[str] = None
    model_capabilities: Optional[List[str]] = None
    offline_support: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ReleaseManifest":
        d = dict(d)
        d.setdefault("signature_version", "legacy")
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


class ReleaseManager:
    def __init__(self, signing_secret: Optional[str] = None,
                 store: Optional[Any] = None,
                 artifact_dir: Optional[str] = None, *,
                 keyring: Optional[signing.KeyRing] = None,
                 signing_key_id: Optional[str] = None):
        if keyring is None:
            keyring = signing.KeyRing()
            keyring.add(signing_key_id or "server-hmac-1",
                        signing_secret or secrets.token_hex(16))
        self.keyring = keyring
        self.secret = keyring.active.secret
        self.manifests: Dict[str, ReleaseManifest] = {}
        self.deployments: List[Dict[str, Any]] = []
        self.rollback_records: List[Dict[str, Any]] = []
        # 退役记录（ALN-11-P）：发布后生命周期状态，与签名字段无关
        self.retired_records: List[Dict[str, Any]] = []
        self.current_release_id: Optional[str] = None
        # 不可逆副作用记录（Arch 13.5：不能假设简单回滚可消除全部影响）
        self.irreversible_actions: List[Dict[str, Any]] = []
        self.verification_evidence: List[Dict[str, Any]] = []
        self.store = store
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        if self.artifact_dir is not None:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if store is not None:
            self._load()

    def _load(self) -> None:
        """重启恢复发布状态（Arch 3.3 状态所有权 / 13.3 发布组合）。"""
        for m in reversed(self.store.list_release_manifests(limit=1000)):
            self.manifests[m.release_id] = m
        for ev in reversed(self.store.list_release_events(limit=1000)):
            if ev["kind"] == "deploy":
                self.deployments.append(ev["data"])
                self.current_release_id = ev["release_id"]
            elif ev["kind"] == "rollback":
                self.rollback_records.append(ev["data"])
                self.current_release_id = ev["data"].get("to")
            elif ev["kind"] == "retire":
                # 退役是发布后生命周期状态；恢复后不影响当前版本指针
                self.retired_records.append(ev["data"])

    # ---- 发布组合 ----
    def build_manifest(self, **kwargs: Any) -> ReleaseManifest:
        # These explicit defaults describe the metadata-only built-in bundle.
        # Real capability builders must supply their additional requirements.
        artifact_paths = kwargs.pop("artifact_files", None)
        defaults = dict(target_platforms=["android", "ios"],
                        permission_requirements=[], data_schema="evoagent/1",
                        model_capabilities=[], offline_support=False,
                        rollback_supported=True)
        defaults.update(kwargs)
        m = ReleaseManifest(**defaults)
        m.asset_refs = _normalize_asset_refs(m.asset_refs)
        if m.release_id in self.manifests:
            raise ValueError("发布 ID 已存在，禁止覆盖不可变制品")
        m.artifact_files = self._collect_artifacts(m.release_id,
                                                   artifact_paths)
        self._sign(m)
        self._store_manifest(m)
        return m

    def _collect_artifacts(self, release_id: str,
                           paths: Any) -> List[Dict[str, Any]]:
        if paths is None:
            return []
        if (isinstance(paths, (str, bytes))
                or not isinstance(paths, (list, tuple))):
            raise ValueError("artifact_files 必须是相对制品包根目录的路径数组")
        if self.artifact_dir is None:
            raise ValueError("声明制品文件必须先配置 artifact_dir")
        bundle_root = self.artifact_dir / release_id
        return signing.collect_artifact_files(bundle_root, paths)

    def _sign(self, m: ReleaseManifest,
              key: Optional[signing.SigningKey] = None) -> None:
        key = key or self.keyring.active
        m.signature_version = signing.SIGNATURE_VERSION
        m.signing_key_id = key.key_id
        payload = signing.build_payload(_signed_metadata(m),
                                        key_id=key.key_id)
        m.signing_payload_digest = signing.payload_digest(payload)
        m.artifact_digest = m.signing_payload_digest
        m.signed_by = signing.sign_payload(payload, key.secret)

    def _write_artifact(self, m: ReleaseManifest) -> None:
        if self.artifact_dir is None:
            return
        artifact = self.artifact_dir / f"{m.release_id}.json"
        temporary = artifact.with_suffix(".tmp")
        temporary.write_bytes(_canonical(_artifact_payload(m)))
        temporary.replace(artifact)

    def _store_manifest(self, m: ReleaseManifest) -> None:
        self.manifests[m.release_id] = m
        if self.store is not None:
            self.store.save_release_manifest(m)
        self._write_artifact(m)

    # ---- 门禁之后的部署（PRD 62）----
    def deploy(self, release_id: str, *, gate_passed: bool,
               scope: str = "normal",
               gray_percent: int = 100,
               asset_repo: Optional[Any] = None) -> Dict[str, Any]:
        m = self.manifests.get(release_id)
        if not m:
            return {"ok": False, "error": "manifest 不存在"}
        if self.is_retired(release_id):
            return {"ok": False, "error": "已退役版本不允许部署"}
        # 发布权限（PRD 81 / Arch 13.4）：核心安全边界不允许普通流程发布
        if scope == "critical":
            return {"ok": False, "error": "核心安全边界的改动不允许普通发布流程"}
        if not gate_passed:
            return {"ok": False, "error": "门禁未通过，拒绝部署"}
        missing = self.missing_compatibility(m)
        if missing:
            evidence = self._record_verification(
                m, None, context="deploy",
                extra={"ok": False, "errors": list(missing),
                       "missing_compatibility": list(missing)})
            prefix = ("发布通道声明非法: " if m.channel not in _CHANNELS
                      else "稳定通道缺少完整兼容声明: ")
            return {"ok": False, "error": prefix + ", ".join(missing),
                    "missing": missing, "verification": evidence}
        check = self.verify(m.release_id)
        evidence = self._record_verification(
            m, check, context="deploy",
            extra={"compatibility_relaxed": m.channel != "stable"})
        if not check.get("ok"):
            return {"ok": False, "error": "制品签名/完整性校验失败",
                    **check, "verification": evidence}
        if m.asset_refs:
            # 含资产引用的发布必须先按精确版本与摘要复核；不给仓储就不放行。
            if asset_repo is None:
                return {"ok": False,
                        "error": "发布含资产引用，但未提供资产仓储，无法完成精确版本校验"}
            assets = self.verify_assets(release_id, asset_repo)
            if not assets.get("ok"):
                return {"ok": False, "error": "资产引用校验失败", **assets}
        self.current_release_id = release_id
        rec = {"release_id": release_id, "at": time.time(),
               "gray_percent": max(0, min(100, int(gray_percent))),
               "previous": self.deployments[-1]["release_id"] if self.deployments else None}
        self.deployments.append(rec)
        if self.store is not None:
            self.store.append_release_event("deploy", release_id, rec)
        return {"ok": True, **rec, "version": m.version,
                "verification": evidence}

    # ---- 回滚（PRD 63）----
    def rollback(self, reason: str) -> Dict[str, Any]:
        if not self.deployments:
            return {"ok": False, "error": "无可回滚记录"}
        last = self.deployments[-1]
        target = last["previous"]
        m = self.manifests.get(target) if target else None
        if m is None:
            return {"ok": False, "error": "回滚目标缺失"}
        if self.is_retired(target):
            return {"ok": False, "error": "回滚目标已退役，拒绝回滚到退役版本"}
        check = self.verify(target)
        evidence = self._record_verification(m, check, context="rollback")
        if not check.get("ok"):
            return {"ok": False, "error": "回滚目标完整性校验失败",
                    **check, "verification": evidence}
        self.current_release_id = target
        rec = {"from": last["release_id"], "to": target,
               "reason": reason, "at": time.time()}
        self.rollback_records.append(rec)
        # 提示不可逆动作
        if self.irreversible_actions:
            rec["irreversible_warning"] = (
                f"存在 {len(self.irreversible_actions)} 项不可逆变化，需单独补偿策略")
        if self.store is not None:
            self.store.append_release_event("rollback", target, rec)
        return {"ok": True, **rec}

    # ---- 退役（ALN-11-P：发布后生命周期）----
    def retire(self, release_id: str, *, reason: str, actor: str) -> Dict[str, Any]:
        """把已发布版本标记为退役；记录不删，可追溯。

        退役版本不得再通过验证、部署、回滚或对账下发；已安装实例的
        退役回执由 ReleaseDelivery.record_apply(action="retired") 记录。
        """
        release_id = (release_id or "").strip()
        if not release_id:
            return {"ok": False, "error": "缺少 release_id"}
        if not isinstance(reason, str) or not reason.strip():
            return {"ok": False, "error": "退役必须写明原因"}
        if not isinstance(actor, str) or not actor.strip():
            return {"ok": False, "error": "退役必须记录操作者"}
        if release_id not in self.manifests:
            return {"ok": False, "error": "manifest 不存在"}
        if self.is_retired(release_id):
            return {"ok": False, "error": "版本已退役"}
        rec = {"release_id": release_id, "reason": reason.strip(),
               "actor": actor.strip(), "at": time.time()}
        self.retired_records.append(rec)
        if self.store is not None:
            self.store.append_release_event("retire", release_id, rec)
        return {"ok": True, **rec}

    def is_retired(self, release_id: Optional[str]) -> bool:
        if not release_id:
            return False
        return any(rec.get("release_id") == release_id
                   for rec in self.retired_records)

    def retired_release_ids(self) -> List[str]:
        return sorted({rec["release_id"] for rec in self.retired_records
                       if rec.get("release_id")})

    # ---- 制品验证（Architecture 18.4）----
    def verify(self, release_id: str) -> Dict[str, Any]:
        m = self.manifests.get(release_id)
        if not m:
            return {"ok": False, "error": "manifest 不存在"}
        if self.is_retired(release_id):
            # 退役版本不得通过验证，也不得作为发布/回滚/对账目标
            return {"ok": False, "retired": True,
                    "artifact_digest": m.artifact_digest,
                    "key_id": m.signing_key_id,
                    "error": "版本已退役，不得通过验证或作为发布目标"}
        if m.signature_version != signing.SIGNATURE_VERSION:
            # 旧格式可读，但不得静默放行；必须显式迁移（重新签名）。
            return {"ok": False, "legacy": True,
                    "artifact_digest": m.artifact_digest,
                    "key_id": m.signing_key_id,
                    "error": "旧签名格式需显式迁移（重新签名）后才能发布"}
        errors: List[str] = []
        payload_digest: Optional[str] = None
        try:
            payload = _signing_payload(m)
            payload_digest = signing.payload_digest(payload)
            digest_ok = (
                isinstance(m.signing_payload_digest, str)
                and isinstance(m.artifact_digest, str)
                and hmac.compare_digest(payload_digest,
                                        m.signing_payload_digest)
                and hmac.compare_digest(payload_digest, m.artifact_digest))
            signature = signing.verify_signature(
                payload, m.signed_by, self.keyring)
            signature_ok = bool(signature.get("ok"))
            if not signature_ok:
                errors.append(str(signature.get("error") or "签名校验失败"))
        except (TypeError, ValueError, signing.SigningError) as exc:
            digest_ok = signature_ok = False
            errors.append(f"签名载荷无法重建: {exc}")
        artifacts = self._verify_artifacts(m)
        errors.extend(f"{item['path']}: {item['error']}"
                      for item in artifacts["errors"])
        artifact_ok = True
        if self.artifact_dir is not None:
            artifact = self.artifact_dir / f"{release_id}.json"
            try:
                data = json.loads(artifact.read_text(encoding="utf-8"))
                artifact_ok = (isinstance(data, dict)
                               and hmac.compare_digest(
                                   _canonical(data),
                                   _canonical(_artifact_payload(m))))
            except (OSError, ValueError, TypeError):
                artifact_ok = False
            if not artifact_ok:
                errors.append("发布正文文件缺失或被篡改")
        # 签名只证明来源与完整性，制品本身安全仍需测试与门禁（Arch 18.4）
        return {"ok": bool(digest_ok and signature_ok
                           and artifacts["ok"] and artifact_ok),
                "artifact_digest": m.artifact_digest,
                "payload_digest": payload_digest,
                "key_id": m.signing_key_id,
                "digest_ok": digest_ok,
                "signature_ok": signature_ok,
                "artifact_files_ok": artifacts["ok"],
                "artifact_files_errors": artifacts["errors"],
                "artifact_ok": artifact_ok,
                "errors": errors,
                "note": "签名仅证明来源与完整性，安全结论依赖于评测与门禁"}

    def _verify_artifacts(self, m: ReleaseManifest) -> Dict[str, Any]:
        if not m.artifact_files:
            return {"ok": True, "verified": [], "errors": []}
        if self.artifact_dir is None:
            return {"ok": False, "verified": [],
                    "errors": [{"path": "-",
                                "error": "清单声明了制品文件但未配置制品目录"}]}
        return signing.verify_artifact_files(
            self.artifact_dir / m.release_id, m.artifact_files,
            check_extra=True)

    def _record_verification(self, m: ReleaseManifest, result: Any, *,
                             context: str,
                             extra: Optional[Dict[str, Any]] = None
                             ) -> Dict[str, Any]:
        result = result or {}
        evidence: Dict[str, Any] = {
            "release_id": m.release_id,
            "version": m.version,
            "channel": m.channel,
            "context": context,
            "at": time.time(),
            "signature_version": m.signature_version,
            "key_id": result.get("key_id") or m.signing_key_id,
            "payload_digest": result.get("payload_digest"),
            "artifact_digest": m.artifact_digest,
            "ok": bool(result.get("ok", False)),
            "errors": list(result.get("errors") or []),
        }
        if extra:
            evidence.update(extra)
        self.verification_evidence.append(evidence)
        if self.store is not None and hasattr(self.store,
                                              "append_release_event"):
            self.store.append_release_event(
                "verification", m.release_id, evidence)
        return evidence

    def verify_assets(self, release_id: str, repo: Any, *,
                      required_states: Tuple[str, ...] = ("approved", "active")
                      ) -> Dict[str, Any]:
        """按发布清单的冻结引用逐项精确读取资产。

        这是服务端的发布门禁，不替代端侧公钥验签；也不会把服务端签名
        秘密下发给客户端。清单无资产引用时视为元数据包，直接通过。
        """
        m = self.manifests.get(release_id)
        if not m:
            return {"ok": False, "error": "manifest 不存在"}
        if not m.asset_refs:
            return {"ok": True, "refs": [],
                    "note": "清单无资产引用（元数据包）"}
        verified: List[Dict[str, str]] = []
        errors: List[Dict[str, str]] = []
        for ref in m.asset_refs:
            if not isinstance(ref, Mapping):
                errors.append({"ref": repr(ref), "error": "资产引用必须是对象",
                               "error_class": "AssetValidationError"})
                continue
            try:
                asset = verify_manifest_ref(ref, repo)
            except AssetError as exc:
                errors.append({"ref": f"{ref.get('asset_id')}@{ref.get('version')}",
                               "error": str(exc),
                               "error_class": type(exc).__name__})
                continue
            if required_states and asset.lifecycle_state not in required_states:
                errors.append({
                    "ref": str(asset.ref),
                    "error": f"生命周期 {asset.lifecycle_state} 不允许进入发布"
                             f"（要求 {list(required_states)}）",
                    "error_class": "LifecycleError"})
                continue
            verified.append({"asset_id": asset.asset_id,
                             "version": asset.version,
                             "content_digest": asset.content_digest})
        return {"ok": not errors, "refs": verified, "errors": errors}

    # ---- 旧格式迁移：显式重新签名，绝不静默接受 ----
    def migrate_signature(self, release_id: str, *,
                          allow_unverified: bool = False,
                          actor: str = "",
                          key_id: Optional[str] = None
                          ) -> Dict[str, Any]:
        m = self.manifests.get(release_id)
        if m is None:
            return {"ok": False, "error": "manifest 不存在"}
        if m.signature_version == signing.SIGNATURE_VERSION:
            check = self.verify(release_id)
            evidence = self._record_verification(
                m, check, context="migration",
                extra={"migrated": False, "legacy_verified": True})
            return {"ok": bool(check.get("ok")), "migrated": False,
                    "verification": evidence}
        legacy_ok, legacy_reason = self._legacy_signature_status(m)
        if not legacy_ok and not allow_unverified:
            message = (f"旧签名无法验证（{legacy_reason}）；"
                       f"需显式 allow_unverified 重新签名")
            evidence = self._record_verification(
                m, None, context="migration",
                extra={"ok": False, "errors": [message],
                       "migrated": False, "legacy_verified": False,
                       "legacy_reason": legacy_reason,
                       "actor": str(actor or "").strip()})
            return {"ok": False, "migrated": False, "error": message,
                    "verification": evidence}
        if not legacy_ok and not str(actor or "").strip():
            return {"ok": False, "migrated": False,
                    "error": "无法验证的旧格式重新签名必须记录操作者"}
        key = self.keyring.get(key_id) if key_id else self.keyring.active
        if key is None:
            return {"ok": False, "migrated": False,
                    "error": f"未知签名密钥标识: {key_id!r}"}
        self._sign(m, key)
        self._store_manifest(m)
        check = self.verify(release_id)
        evidence = self._record_verification(
            m, check, context="migration",
            extra={"migrated": True, "legacy_verified": legacy_ok,
                   "legacy_reason": legacy_reason,
                   "actor": str(actor or "").strip()})
        return {"ok": bool(check.get("ok")), "migrated": True,
                "legacy_verified": legacy_ok,
                "legacy_reason": legacy_reason,
                "verification": evidence}

    def _legacy_signature_status(
            self, m: ReleaseManifest) -> Tuple[bool, str]:
        if m.signature_version != "hmac-sha256-v1":
            return False, f"不支持的旧签名版本: {m.signature_version!r}"
        try:
            if not (isinstance(m.artifact_digest, str)
                    and hmac.compare_digest(_legacy_digest(m),
                                            m.artifact_digest)):
                return False, "旧制品摘要不匹配"
            if not isinstance(m.signed_by, str) or not m.signed_by:
                return False, "旧签名缺失"
            for key_id in self.keyring.key_ids():
                expected = _sign({"digest": m.artifact_digest},
                                 self.keyring.get(key_id).secret)
                if hmac.compare_digest(expected, m.signed_by):
                    return True, key_id
            return False, "旧签名与当前密钥环不匹配"
        except (TypeError, ValueError):
            return False, "旧签名格式无法解析"

    # ---- 稳定通道兼容声明 ----
    @staticmethod
    def missing_compatibility(m: ReleaseManifest) -> List[str]:
        if m.channel not in _CHANNELS:
            return [f"channel(未知发布通道: {m.channel!r})"]
        if m.channel != "stable":
            # 草稿/内部通道可放宽，但验证证据会显式标记 relaxed。
            return []
        missing: List[str] = []

        def declared_list(name: str, value: Any, *, require_item: bool
                          ) -> None:
            if (not isinstance(value, list)
                    or not all(isinstance(v, str) and v.strip()
                               for v in value)
                    or (require_item and not value)):
                missing.append(name)

        declared_list("target_platforms", m.target_platforms,
                      require_item=True)
        if (isinstance(m.target_platforms, list)
                and any(p not in _SUPPORTED_PLATFORMS
                        for p in m.target_platforms
                        if isinstance(p, str))):
            missing.append("target_platforms(不支持的平台)")
        if (not isinstance(m.min_app_version, str)
                or not _APP_VERSION_RE.match(m.min_app_version)):
            missing.append("min_app_version")
        declared_list("permission_requirements", m.permission_requirements,
                      require_item=False)
        declared_list("required_tool_contracts", m.required_tool_contracts,
                      require_item=False)
        declared_list("model_capabilities", m.model_capabilities,
                      require_item=False)
        if not isinstance(m.data_schema, str) or not m.data_schema.strip():
            missing.append("data_schema")
        if not isinstance(m.offline_support, bool):
            missing.append("offline_support")
        if not isinstance(m.rollback_supported, bool):
            missing.append("rollback_supported")
        try:
            _normalize_asset_refs(m.asset_refs)
        except ValueError:
            missing.append("asset_refs")
        return missing

    @staticmethod
    def compatibility_complete(m: ReleaseManifest) -> bool:
        return not ReleaseManager.missing_compatibility(m)
