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

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


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
    rollback_target: Optional[str] = None
    artifact_digest: Optional[str] = None
    signed_by: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    signature_version: str = "hmac-sha256-v1"
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
                 artifact_dir: Optional[str] = None):
        self.secret = signing_secret or secrets.token_hex(16)
        self.manifests: Dict[str, ReleaseManifest] = {}
        self.deployments: List[Dict[str, Any]] = []
        self.rollback_records: List[Dict[str, Any]] = []
        self.current_release_id: Optional[str] = None
        # 不可逆副作用记录（Arch 13.5：不能假设简单回滚可消除全部影响）
        self.irreversible_actions: List[Dict[str, Any]] = []
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

    # ---- 发布组合 ----
    def build_manifest(self, **kwargs: Any) -> ReleaseManifest:
        # These explicit defaults describe the metadata-only built-in bundle.
        # Real capability builders must supply their additional requirements.
        defaults = dict(target_platforms=["android", "ios"],
                        permission_requirements=[], data_schema="evoagent/1",
                        model_capabilities=[], offline_support=False)
        defaults.update(kwargs)
        m = ReleaseManifest(**defaults)
        m.asset_refs = _normalize_asset_refs(m.asset_refs)
        if m.release_id in self.manifests:
            raise ValueError("发布 ID 已存在，禁止覆盖不可变制品")
        m.artifact_digest = _digest(m)
        m.signed_by = _sign({"digest": m.artifact_digest},
                            self.secret)
        self.manifests[m.release_id] = m
        if self.store is not None:
            self.store.save_release_manifest(m)
        if self.artifact_dir is not None:
            artifact = self.artifact_dir / f"{m.release_id}.json"
            temporary = artifact.with_suffix(".tmp")
            temporary.write_bytes(_canonical(_artifact_payload(m)))
            temporary.replace(artifact)
        return m

    # ---- 门禁之后的部署（PRD 62）----
    def deploy(self, release_id: str, *, gate_passed: bool,
               scope: str = "normal",
               gray_percent: int = 100,
               asset_repo: Optional[Any] = None) -> Dict[str, Any]:
        m = self.manifests.get(release_id)
        if not m:
            return {"ok": False, "error": "manifest 不存在"}
        # 发布权限（PRD 81 / Arch 13.4）：核心安全边界不允许普通流程发布
        if scope == "critical":
            return {"ok": False, "error": "核心安全边界的改动不允许普通发布流程"}
        if not gate_passed:
            return {"ok": False, "error": "门禁未通过，拒绝部署"}
        if m.channel == "stable" and not self.compatibility_complete(m):
            return {"ok": False, "error": "稳定通道缺少完整兼容声明"}
        check = self.verify(m.release_id)
        if not check.get("ok"):
            return {"ok": False, "error": "制品签名/完整性校验失败", **check}
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
        return {"ok": True, **rec, "version": m.version}

    # ---- 回滚（PRD 63）----
    def rollback(self, reason: str) -> Dict[str, Any]:
        if not self.deployments:
            return {"ok": False, "error": "无可回滚记录"}
        last = self.deployments[-1]
        target = last["previous"]
        m = self.manifests.get(target) if target else None
        if m is None:
            return {"ok": False, "error": "回滚目标缺失"}
        if not self.verify(target).get("ok"):
            return {"ok": False, "error": "回滚目标完整性校验失败"}
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

    # ---- 制品验证（Architecture 18.4）----
    def verify(self, release_id: str) -> Dict[str, Any]:
        m = self.manifests.get(release_id)
        if not m:
            return {"ok": False, "error": "manifest 不存在"}
        try:
            digest_ok = (isinstance(m.artifact_digest, str)
                         and hmac.compare_digest(_digest(m), m.artifact_digest))
            expected_sig = _sign({"digest": m.artifact_digest}, self.secret)
            sig_ok = (m.signature_version == "hmac-sha256-v1"
                      and isinstance(m.signed_by, str)
                      and hmac.compare_digest(expected_sig, m.signed_by))
        except (TypeError, ValueError):
            digest_ok = sig_ok = False
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
        # 签名只证明来源与完整性，制品本身安全仍需测试与门禁（Arch 18.4）
        return {"ok": digest_ok and sig_ok and artifact_ok,
                "artifact_digest": m.artifact_digest,
                "digest_ok": digest_ok,
                "artifact_ok": artifact_ok,
                "note": "签名仅证明来源与完整性，安全结论依赖于评测与门禁"}

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

    @staticmethod
    def compatibility_complete(m: ReleaseManifest) -> bool:
        arrays = (m.target_platforms, m.required_tool_contracts,
                  m.permission_requirements, m.model_capabilities)
        try:
            _normalize_asset_refs(m.asset_refs)
        except ValueError:
            return False
        return (all(isinstance(a, list) and all(isinstance(v, str) and v
                                               for v in a) for a in arrays)
                and bool(m.target_platforms)
                and isinstance(m.min_app_version, str) and bool(m.min_app_version)
                and isinstance(m.data_schema, str) and bool(m.data_schema)
                and isinstance(m.offline_support, bool))
