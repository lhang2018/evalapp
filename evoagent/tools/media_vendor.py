"""真实音视频供应商切片（ALN-19-A）。

按 ALN-19 关闭标准接入真实音视频成品与计划/评测/费用；本切片先交付
**供应商契约与受控调用入口**（真实 HTTP 传输、配置/预算校验、费用/版本/
授权追溯、失败边界恢复）。边界（如实标注）：

- 只有配置了真实供应商（provider/base_url/api_key/model_version/预算/单价）
  才允许调用；缺失即拒绝，绝不默认放行或用假成功代替；
- 费用按调用前预估 + 供应商回执对账；失败不扣预算（回滚），并记录失败审计；
- 测试用本地 HTTP **夹具（fixture）** 供应商，明确标注为夹具，**不冒充**
  真实供应商/生产实测；真实供应商凭据与预算未提供前不冒称 19-A 关闭。
"""

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

ALLOWED_EDITS = ("trim", "concat", "subtitle")


class MediaVendorError(RuntimeError):
    """供应商调用失败/回执非法（不返回假成功）。"""


class MediaVendorRefused(PermissionError):
    """未配置真实供应商/预算不足/操作未授权，拒绝调用。"""


@dataclass(frozen=True)
class MediaVendorConfig:
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    model_version: str = ""
    budget_usd: float = 0.0
    price_per_second_usd: float = 0.0
    edit_price_usd: float = 0.05
    timeout_s: float = 30.0

    @property
    def ready(self) -> bool:
        return bool(self.provider.strip() and self.base_url.strip()
                    and self.api_key.strip() and self.model_version.strip()
                    and self.budget_usd > 0 and self.price_per_second_usd > 0)

    def public(self) -> Dict[str, Any]:
        return {"provider": self.provider, "base_url": self.base_url,
                "model_version": self.model_version,
                "budget_usd": self.budget_usd,
                "price_per_second_usd": self.price_per_second_usd,
                "edit_price_usd": self.edit_price_usd,
                "configured": self.ready,
                "api_key_configured": bool(self.api_key)}

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "MediaVendorConfig":
        env = dict(os.environ if env is None else env)
        def num(key, default=0.0):
            try:
                return float(env.get(key) or default)
            except (TypeError, ValueError):
                return default
        return cls(provider=env.get("EVOAGENT_MEDIA_VENDOR", ""),
                   base_url=env.get("EVOAGENT_MEDIA_BASE_URL", ""),
                   api_key=env.get("EVOAGENT_MEDIA_API_KEY", ""),
                   model_version=env.get("EVOAGENT_MEDIA_MODEL_VERSION", ""),
                   budget_usd=num("EVOAGENT_MEDIA_BUDGET_USD"),
                   price_per_second_usd=num("EVOAGENT_MEDIA_PRICE_PER_SECOND"),
                   edit_price_usd=num("EVOAGENT_MEDIA_EDIT_PRICE", 0.05),
                   timeout_s=num("EVOAGENT_MEDIA_TIMEOUT", 30.0) or 30.0)


def _default_transport(url: str, payload: Dict[str, Any],
                       headers: Dict[str, str], timeout: float) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raise MediaVendorError(
            f"供应商 HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise MediaVendorError(f"供应商网络失败: {exc}") from exc
    if status != 200:
        raise MediaVendorError(f"供应商返回状态 {status}")
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise MediaVendorError("供应商回执不是合法 JSON") from exc


class MediaVendorClient:
    def __init__(self, config: MediaVendorConfig, *,
                 transport: Optional[Callable[..., Dict[str, Any]]] = None,
                 audit: Any = None, clock: Optional[Callable[[], float]] = None):
        self.config = config
        self.transport = transport or _default_transport
        self.audit = audit
        self.clock = clock or time.time
        self.spent_usd = 0.0
        self.calls: List[Dict[str, Any]] = []
        self.failures: List[Dict[str, Any]] = []

    # ---- 内部：拒绝/审计/预算 ----
    def _require_ready(self) -> None:
        if not self.config.ready:
            raise MediaVendorRefused(
                "未配置真实音视频供应商或预算；拒绝调用（不伪造产物）")

    def _require_actor(self, actor: str, reason: str) -> None:
        if not isinstance(actor, str) or not actor.strip():
            raise MediaVendorRefused("调用必须记录已鉴权 actor")
        if not isinstance(reason, str) or not reason.strip():
            raise MediaVendorRefused("调用必须记录 reason")

    def _remaining(self) -> float:
        return round(self.config.budget_usd - self.spent_usd, 6)

    def _audit(self, actor: str, action: str, detail: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(actor, action, target=self.config.provider,
                              detail=detail)
        except Exception:  # noqa: BLE001 —— 审计故障不改变主结果
            pass

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self.config.base_url.rstrip("/") + path
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + self.config.api_key}
        response = self.transport(url, payload, headers, self.config.timeout_s)
        if not isinstance(response, dict):
            raise MediaVendorError("供应商回执必须是 JSON 对象")
        return response

    @staticmethod
    def _content(response: Dict[str, Any]) -> bytes:
        raw = response.get("content_b64")
        if not isinstance(raw, str) or not raw:
            raise MediaVendorError("供应商回执缺少 content_b64（拒绝假产物）")
        try:
            return base64.b64decode(raw, validate=True)
        except (ValueError, TypeError) as exc:
            raise MediaVendorError("供应商回执 content_b64 非法") from exc

    def _record(self, actor: str, reason: str, action: str, asset: Dict[str, Any],
                cost: float, response: Dict[str, Any]) -> Dict[str, Any]:
        vendor_cost = response.get("vendor_cost_usd")
        if isinstance(vendor_cost, (int, float)) and not isinstance(vendor_cost, bool):
            cost = round(float(vendor_cost), 6)
        if cost < 0 or cost > self._remaining() + 1e-9:
            raise MediaVendorError("供应商回执费用超出剩余预算，拒绝记账")
        self.spent_usd = round(self.spent_usd + cost, 6)
        record = {"ok": True, "action": action, "provider": self.config.provider,
                  "model_version": self.config.model_version,
                  "asset": asset, "cost_usd": cost,
                  "remaining_usd": self._remaining(), "actor": actor,
                  "reason": reason, "at": self.clock()}
        self.calls.append(record)
        self._audit(actor, "media_" + action, {
            "provider": self.config.provider,
            "model_version": self.config.model_version,
            "asset_id": asset.get("asset_id"),
            "sha256": asset.get("sha256"), "cost_usd": cost})
        return record

    def _fail(self, actor: str, reason: str, action: str,
              error: Exception) -> None:
        entry = {"ok": False, "action": action, "actor": actor,
                 "reason": reason, "error": str(error), "at": self.clock(),
                 "refunded": True}
        self.failures.append(entry)
        self._audit(actor, "media_" + action + "_failed", entry)

    # ---- 真实供应商操作 ----
    def render_clip(self, prompt: str, *, duration_s: float, actor: str,
                    reason: str) -> Dict[str, Any]:
        self._require_ready()
        self._require_actor(actor, reason)
        if not isinstance(prompt, str) or not prompt.strip():
            raise MediaVendorError("prompt 不得为空")
        try:
            duration_s = float(duration_s)
        except (TypeError, ValueError):
            raise MediaVendorError("duration_s 必须是数字") from None
        if not 1.0 <= duration_s <= 60.0:
            raise MediaVendorError("duration_s 必须在 1..60 秒")
        cost = round(duration_s * self.config.price_per_second_usd, 6)
        if cost > self._remaining() + 1e-9:
            raise MediaVendorRefused("预算不足，拒绝生成视频")
        try:
            response = self._post("/v1/clips", {
                "prompt": prompt.strip(), "duration_s": duration_s,
                "model_version": self.config.model_version})
            content = self._content(response)
            asset_id = str(response.get("asset_id") or "").strip()
            if not asset_id:
                raise MediaVendorError("供应商回执缺少 asset_id")
            asset = {"asset_id": asset_id, "sha256": hashlib.sha256(content).hexdigest(),
                     "bytes": len(content),
                     "content_type": str(response.get("content_type") or "")}
            return self._record(actor, reason, "render_clip", asset, cost, response)
        except MediaVendorError as exc:
            self._fail(actor, reason, "render_clip", exc)
            raise

    def edit_clip(self, asset_ref: str, ops: List[str], *, actor: str,
                  reason: str) -> Dict[str, Any]:
        self._require_ready()
        self._require_actor(actor, reason)
        if not isinstance(asset_ref, str) or not asset_ref.strip():
            raise MediaVendorError("asset_ref 不得为空")
        if (not isinstance(ops, list) or not ops
                or any(op not in ALLOWED_EDITS for op in ops)):
            raise MediaVendorRefused(
                "未授权的剪辑操作；允许: " + ", ".join(ALLOWED_EDITS))
        cost = round(self.config.edit_price_usd * len(ops), 6)
        if cost > self._remaining() + 1e-9:
            raise MediaVendorRefused("预算不足，拒绝剪辑")
        try:
            response = self._post("/v1/edits", {
                "asset_ref": asset_ref.strip(), "ops": list(ops),
                "model_version": self.config.model_version})
            content = self._content(response)
            asset = {"asset_id": str(response.get("asset_id") or ""),
                     "parent_asset_id": asset_ref.strip(),
                     "sha256": hashlib.sha256(content).hexdigest(),
                     "bytes": len(content),
                     "ops": list(ops)}
            return self._record(actor, reason, "edit_clip", asset, cost, response)
        except MediaVendorError as exc:
            self._fail(actor, reason, "edit_clip", exc)
            raise

    def report(self) -> Dict[str, Any]:
        return {"config": self.config.public(),
                "spent_usd": self.spent_usd,
                "remaining_usd": self._remaining(),
                "calls": len(self.calls),
                "failures": len(self.failures),
                "last_call": self.calls[-1] if self.calls else None}
