"""发布管理（PRD 59-63 / Architecture 13.3-13.5）。

- 发布组合清单：代码提交 + 模型适配 + 提示词 + 工具/工作流/环境/评测版本 + 制品摘要 + 回滚目标。
- 签名制品；回滚恢复兼容组合；不可逆变化需要补偿策略（Arch 13.5）。
"""

import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def _sign(payload: Dict[str, Any], secret: str) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((secret + blob).encode()).hexdigest()


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
    # 下发范围与兼容条件（双端协同 Arch 5.1）：缺失时按“不限制”处理
    target_platforms: List[str] = field(default_factory=list)
    min_app_version: str = "0.0.0"
    required_tool_contracts: List[str] = field(default_factory=list)
    channel: str = "stable"
    bundle_kind: str = "capability"
    rollback_target: Optional[str] = None
    artifact_digest: Optional[str] = None
    signed_by: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ReleaseManifest":
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
        m = ReleaseManifest(**kwargs) if kwargs else ReleaseManifest()
        blob = json.dumps(m.to_dict(), sort_keys=True, default=str)
        m.artifact_digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
        m.signed_by = _sign({"digest": m.artifact_digest},
                            self.secret)
        self.manifests[m.release_id] = m
        if self.store is not None:
            self.store.save_release_manifest(m)
        if self.artifact_dir is not None:
            artifact = self.artifact_dir / f"{m.release_id}.json"
            artifact.write_text(json.dumps(m.to_dict(), ensure_ascii=False,
                                           indent=1, default=str),
                                encoding="utf-8")
        return m

    # ---- 门禁之后的部署（PRD 62）----
    def deploy(self, release_id: str, *, gate_passed: bool,
               scope: str = "normal",
               gray_percent: int = 100) -> Dict[str, Any]:
        m = self.manifests.get(release_id)
        if not m:
            return {"ok": False, "error": "manifest 不存在"}
        # 发布权限（PRD 81 / Arch 13.4）：核心安全边界不允许普通流程发布
        if scope == "critical":
            return {"ok": False, "error": "核心安全边界的改动不允许普通发布流程"}
        if not gate_passed:
            return {"ok": False, "error": "门禁未通过，拒绝部署"}
        check = self.verify(m.release_id)
        if not check.get("ok"):
            return {"ok": False, "error": "制品签名/完整性校验失败", **check}
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
        self.verify(target)
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
        expected_sig = _sign({"digest": m.artifact_digest}, self.secret)
        sig_ok = expected_sig == m.signed_by
        artifact_ok = True
        if self.artifact_dir is not None:
            artifact = self.artifact_dir / f"{release_id}.json"
            if artifact.exists():
                try:
                    data = json.loads(artifact.read_text(encoding="utf-8"))
                    artifact_ok = data.get("artifact_digest") == m.artifact_digest
                except Exception:  # noqa: BLE001
                    artifact_ok = False
        # 签名只证明来源与完整性，制品本身安全仍需测试与门禁（Arch 18.4）
        return {"ok": sig_ok and artifact_ok,
                "artifact_digest": m.artifact_digest,
                "artifact_ok": artifact_ok,
                "note": "签名仅证明来源与完整性，安全结论依赖于评测与门禁"}
