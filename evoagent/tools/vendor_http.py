"""真实外部供应商适配器（ALN-02-P：真实供应商副作用与进程故障恢复）。

把「受控外部 HTTP 服务 / 真实供应商」包装成 `ToolRegistry` 可调用的工具，
让已有的副作用预记账与回执核验链路跑在真实外部 I/O 上，而不是测试夹具里
的假函数：

- **runner**：``POST {base}/effects``，携带由参数派生的客户端幂等键与载荷。
  供应商按幂等键去重（重复提交返回同一回执，不会重复产生外部副作用）。
  成功返回带有 ``evidence.external_receipt_ref`` 的执行结果；网络错误/超时
  交给 registry 按 ``side_effect_level=external`` 标记为 unknown（禁止盲目重试）。
- **reconcile**：按同一幂等键 ``GET {base}/receipts/{key}`` 读取**供应商权威
  回执**，与调用方提供的回执逐项比对：
  - 存在且一致 → ``independently_verified=True`` + ``executed=True``（确认已执行）；
  - 明确不存在 → ``independently_verified=True`` + ``executed=False``（证明未执行，
    允许一次全新尝试）；
  - 查询失败或回执不一致 → 抛异常，保持 unknown（不猜测外部状态）。
- **幂等键**：由规范化参数摘要派生（与 registry 默认幂等键同源），因此崩溃后
  用同一参数重放会命中供应商去重与本地账本两条路径。

边界：本模块对接的是**受控外部测试服务**（真实本地 HTTP 服务），不是生产供应商；
协议形态（幂等键 + 回执查询 + 逐字比对）按真实供应商对接方式设计，生产接入需
替换 base_url/鉴权与供应商错误码映射（见交付文档"保留缺口"）。
"""

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .contract import ToolContract, ToolResult

DEFAULT_TOOL_ID = "vendor.submit"
DEFAULT_TIMEOUT = 3.0


class VendorError(RuntimeError):
    """供应商交互失败（连接、超时、协议或回执不可解析）。"""


def canonical_args(args: Dict[str, Any]) -> str:
    return json.dumps(args, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False, default=str)


def client_key_for(args: Dict[str, Any]) -> str:
    """由参数派生的稳定客户端幂等键（跨进程/重启一致）。"""
    return "vendor-" + hashlib.sha256(canonical_args(args).encode()).hexdigest()[:32]


@dataclass
class VendorClient:
    """最小 HTTP 客户端：提交外部副作用 + 查询权威回执。"""

    base_url: str
    timeout: float = DEFAULT_TIMEOUT
    token: str = ""
    calls: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.base_url = str(self.base_url).rstrip("/")

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> tuple:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.base_url + path, data=data,
                                         headers=headers, method=method)
        self.calls.append((method, path))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise VendorError(f"供应商不可达: {exc}") from exc

    def submit(self, client_key: str, operation: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        status, body = self._request("POST", "/effects", {
            "client_key": client_key, "operation": operation, "payload": payload})
        if status >= 400:
            raise VendorError(f"供应商拒绝请求({status}): {body[:200]!r}")
        receipt = self._parse(body)
        if receipt.get("client_key") != client_key:
            raise VendorError("供应商回执与客户端幂等键不一致")
        if not receipt.get("receipt_id"):
            raise VendorError("供应商回执缺少 receipt_id")
        return receipt

    def lookup(self, client_key: str) -> Optional[Dict[str, Any]]:
        status, body = self._request("GET", "/receipts/" + client_key)
        if status == 404:
            return None
        if status >= 400:
            raise VendorError(f"回执查询失败({status})")
        receipt = self._parse(body)
        if receipt.get("client_key") != client_key:
            raise VendorError("回执查询结果与幂等键不一致")
        return receipt

    @staticmethod
    def _parse(body: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise VendorError(f"供应商回执不可解析: {exc}") from exc
        if not isinstance(data, dict):
            raise VendorError("供应商回执必须是 JSON 对象")
        return data


class VendorEffectAdapter:
    """把一个外部 HTTP 供应商注册成副作用工具（runner + reconcile）。"""

    def __init__(self, base_url: str, *, tool_id: str = DEFAULT_TOOL_ID,
                 timeout_policy: float = DEFAULT_TIMEOUT,
                 tool_version: str = "1.0.0",
                 client: Optional[VendorClient] = None):
        self.tool_id = tool_id
        self.tool_version = tool_version
        self.client = client or VendorClient(base_url, timeout=timeout_policy)
        self._contract = ToolContract(
            tool_id=tool_id,
            description="向外部供应商提交真实副作用（HTTP，按幂等键去重）",
            tool_version=tool_version,
            input_schema={"type": "object", "properties": {
                "operation": {"type": "string"},
                "payload": {"type": "object"},
                "delay": {"type": "number"},
                "reject": {"type": "boolean"},
                "corrupt": {"type": "boolean"},
            }, "required": ["operation"]},
            output_schema={"type": "object", "properties": {
                "receipt_id": {"type": "string"},
                "status": {"type": "string"},
                "result": {"type": "object"},
            }, "required": ["receipt_id", "status"]},
            side_effect_level="external",
            timeout_policy=float(timeout_policy),
            retry_policy=0,            # 外部副作用不自动重试；由对账决定
            idempotency_policy="key",
            requires_network=True,
            reconcile=self.reconcile,
        )

    @property
    def contract(self) -> ToolContract:
        return self._contract

    # ---- 执行 ----
    def runner(self, args: Dict[str, Any]) -> ToolResult:
        key = client_key_for(args)
        operation = str(args.get("operation") or "")
        payload = dict(args.get("payload") or {})
        receipt = self.client.submit(key, operation, payload)
        result = ToolResult(
            ok=True,
            data={"receipt_id": receipt["receipt_id"],
                  "status": receipt.get("status", "applied"),
                  "result": receipt.get("result") or {}},
            submitted=True, executed=True, contract_valid=True,
            goal_reached=True,
            evidence={"external_receipt_ref": "vendor:" + receipt["receipt_id"],
                      "client_key": key, "vendor_status": receipt.get("status", ""),
                      "operation": operation})
        result.idempotency_key = key
        return result

    # ---- 对账（回执核验）----
    def reconcile(self, operation: Dict[str, Any], receipt: Dict[str, Any]) -> ToolResult:
        args = operation.get("args")
        if not isinstance(args, dict):
            raise VendorError("缺少原动作参数，不能核验回执")
        key = client_key_for(args)
        remote = self.client.lookup(key)  # 供应商权威记录，不信任调用方传入
        if remote is None:
            result = ToolResult(ok=False, executed=False, submitted=False,
                                independently_verified=True)
            result.evidence["external_receipt_ref"] = "vendor:absent:" + key
            return result
        provided = receipt if isinstance(receipt, dict) else {}
        if provided.get("receipt_id") and provided["receipt_id"] != remote.get("receipt_id"):
            raise VendorError("调用方回执与供应商权威记录不一致，拒绝确认")
        if provided.get("client_key") and provided["client_key"] != key:
            raise VendorError("调用方回执的幂等键与原动作不一致")
        result = ToolResult(
            ok=True,
            data={"receipt_id": remote["receipt_id"],
                  "status": remote.get("status", "applied"),
                  "result": remote.get("result") or {}},
            submitted=True, executed=True, contract_valid=True,
            goal_reached=True, independently_verified=True)
        result.evidence["external_receipt_ref"] = "vendor:" + remote["receipt_id"]
        result.evidence["reconciled_via"] = "vendor_receipt_query"
        return result
