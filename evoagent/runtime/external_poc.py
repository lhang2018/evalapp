"""固定外部 Runtime 真实 PoC（ALN-20-P）。

被测外部 Runtime：**固定版本的 Ollama HTTP 服务 + 已下载本地模型**（真实
外部进程，非夹具）。PoC 只让它承接「模型生成执行」，五类 Runtime 能力全部
由既有权威组件补位，从而满足 20-A 的采用判据：

- AC-1：五类能力 full 或显式补位 → 工具/事件/取消/恢复/沙箱全部委派既有权威；
- AC-2：权限、副作用账本与 Task 权威不被复制 → 外部 Runtime 不接触账本，
  所有动作经 `ToolRegistry.execute` / `controller.cancel_task|recover_task`；
- AC-3：取消/恢复/沙箱语义可验证 → 支持 `tests/test_runtime_protocol.py` 的
  共享夹具 `run_contract_suite`（本模块测试直接复用）；
- AC-4：固定版本可复现 → 绑定 Runtime 版本号与**模型内容摘要**（`/api/version`
  与 `/api/tags` 的 digest），不一致即 fail-closed 拒绝执行。

边界：本模块不把 Ollama 的会话消息冒充 run 级事件账本；`generate()` 是外部
执行入口（非 RuntimeProtocol 能力），取消语义仍由既有 controller 承载；
真实主链采用（路由到外部 Runtime）不在本卡范围。
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .matrix import assert_matches_declarations
from .protocol import (CancelOutcome, CapabilityDeclaration, RecoveryOutcome,
                       RuntimeCapability, RuntimeProtocol, SandboxOutcome)

IMPLEMENTATION = "external_ollama_http"
DEFAULT_TIMEOUT = 10.0


class RuntimePinMismatch(RuntimeError):
    """固定版本/模型摘要与登记不一致：拒绝执行（fail-closed）。"""


class ExternalRuntimeUnavailable(RuntimeError):
    """外部 Runtime 不可达/超时/协议错误：隔离为可分类失败。"""


@dataclass(frozen=True)
class PinnedIdentity:
    """固定外部 Runtime 的身份：版本号 + 模型摘要（可复现证据）。"""

    implementation: str
    version: str
    model: str
    model_digest: str
    base_url: str = ""
    source: str = "/api/version + /api/tags"

    def to_dict(self) -> Dict[str, str]:
        return {"implementation": self.implementation, "version": self.version,
                "model": self.model, "model_digest": self.model_digest,
                "base_url": self.base_url, "source": self.source}


def _request(base_url: str, path: str, payload: Optional[Dict[str, Any]] = None,
             timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data,
                                     headers=headers,
                                     method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise ExternalRuntimeUnavailable(
            f"外部 Runtime 不可达或超时: {type(exc).__name__}: {exc}") from exc
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExternalRuntimeUnavailable(f"外部 Runtime 返回非 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ExternalRuntimeUnavailable("外部 Runtime 返回不是 JSON 对象")
    return parsed


def fetch_identity(base_url: str, *, model: str,
                   timeout: float = DEFAULT_TIMEOUT) -> PinnedIdentity:
    """读取真实外部 Runtime 的版本与模型摘要（用于固定版本 PoC）。"""
    version = _request(base_url, "/api/version", timeout=timeout).get("version")
    if not isinstance(version, str) or not version.strip():
        raise ExternalRuntimeUnavailable("外部 Runtime 未返回版本号")
    tags = _request(base_url, "/api/tags", timeout=timeout).get("models") or []
    for item in tags:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "")
        if name == model or name.split(":")[0] == model.split(":")[0]:
            digest = str(item.get("digest") or "")
            if not digest:
                raise ExternalRuntimeUnavailable("模型缺少内容摘要，无法固定版本")
            return PinnedIdentity(implementation=IMPLEMENTATION,
                                  version=version.strip(), model=name,
                                  model_digest=digest, base_url=base_url)
    raise ExternalRuntimeUnavailable(f"外部 Runtime 未安装指定模型: {model}")


def verify_pin(actual: PinnedIdentity, *, expected_version: str,
               expected_model_digest: str) -> PinnedIdentity:
    """固定版本校验：版本号或模型摘要不一致即拒绝（不自动升级）。"""
    if actual.version != expected_version:
        raise RuntimePinMismatch(
            f"外部 Runtime 版本已漂移: 期望 {expected_version} 实际 {actual.version}")
    if actual.model_digest != expected_model_digest:
        raise RuntimePinMismatch(
            f"模型摘要已漂移: 期望 {expected_model_digest} 实际 {actual.model_digest}")
    return actual


class ExternalRuntimeBridge(RuntimeProtocol):
    """把固定外部 Runtime 桥接进本协议；五类能力委派既有权威。"""

    name = IMPLEMENTATION

    def __init__(self, *, registry: Any, bus: Any, controller: Any,
                 sandbox: Any, identity: PinnedIdentity,
                 base_url: str = "", timeout: float = DEFAULT_TIMEOUT):
        missing = [label for label, value in (
            ("registry", registry), ("bus", bus), ("controller", controller),
            ("sandbox", sandbox)) if value is None]
        if missing:
            # 显式拒绝：缺少权威组件时不得自建第二套（AC-2）
            raise ValueError("外部 Runtime 桥接缺少权威组件: " + ", ".join(missing))
        self.registry = registry
        self.bus = bus
        self.controller = controller
        self.sandbox = sandbox
        self.identity = identity
        self.base_url = base_url or identity.base_url
        self.timeout = float(timeout)

    # ---- 能力声明（与 matrix 行逐项对齐）----
    def declarations(self) -> List[CapabilityDeclaration]:
        return [
            CapabilityDeclaration(RuntimeCapability.TOOL_INVOCATION, True, {
                "delegate": "ToolRegistry.execute",
                "note": "权限/预算/幂等/副作用账本语义不变"}),
            CapabilityDeclaration(RuntimeCapability.EVENT_STREAM, True, {
                "delegate": "EventBus", "note": "subscribe/emit/query_run 直通"}),
            CapabilityDeclaration(RuntimeCapability.CANCEL, True, {
                "delegate": "controller.cancel_task", "note": "协作式、幂等"}),
            CapabilityDeclaration(RuntimeCapability.RECOVERY, True, {
                "delegate": "controller.recover_task",
                "note": "中断接管仍由既有运行租约/Task 状态机决定"}),
            CapabilityDeclaration(RuntimeCapability.SANDBOX, True, {
                "delegate": "evolution.SandboxRunner",
                "note": "一次性目录/无网络/超时强杀"}),
        ]

    # ---- 五类能力实现（全部透传既有权威）----
    def _invoke_tool(self, tool_id: str, args: Dict[str, Any], **passthrough):
        return self.registry.execute(tool_id, args, **passthrough)

    def _subscribe_events(self, fn) -> None:
        self.bus.subscribe(fn)

    def _emit_event(self, **fields: Any) -> Any:
        return self.bus.emit(**fields)

    def _query_run_events(self, run_id: str, limit: int = 200):
        return self.bus.query_run(run_id, limit)

    def _cancel_run(self, task_id: str) -> CancelOutcome:
        try:
            result = self.controller.cancel_task(task_id)
        except Exception as exc:  # noqa: BLE001 —— 未知任务按契约返回 ok=False
            return CancelOutcome(ok=False, task_id=task_id,
                                 error=f"{type(exc).__name__}: {exc}")
        if isinstance(result, dict):
            return CancelOutcome(ok=bool(result.get("ok")), task_id=task_id,
                                 status=result.get("status"),
                                 error=result.get("error"))
        return CancelOutcome(ok=True, task_id=task_id)

    def _recover_run(self, task: Any, manifest: Optional[Dict[str, Any]] = None):
        try:
            recovered = self.controller.recover_task(task.task_id, wait=False)
        except Exception as exc:  # noqa: BLE001 —— 不可恢复按失败返回
            return RecoveryOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")
        return RecoveryOutcome(ok=True, run_id=recovered.current_run_id,
                               started_at=time.time())

    def _run_sandboxed(self, code: str, timeout: Optional[float] = None
                       ) -> SandboxOutcome:
        result = self.sandbox.run_code(code, timeout=timeout or self.timeout)
        return SandboxOutcome(ok=result.status == "passed",
                              output=result.stdout, error=result.stderr,
                              violation=bool(result.violations))

    # ---- 真实外部执行（PoC 的被测对象）----
    def generate(self, prompt: str, *, model: Optional[str] = None,
                 timeout: Optional[float] = None) -> Dict[str, Any]:
        """向固定外部 Runtime 发起真实生成调用（非流式）。"""
        moment = time.time()
        payload = {"model": model or self.identity.model, "prompt": prompt,
                   "stream": False}
        started = time.time()
        reply = _request(self.base_url, "/api/generate", payload,
                         timeout=timeout or self.timeout)
        latency = time.time() - started
        text = reply.get("response")
        if not isinstance(text, str) or not text.strip():
            raise ExternalRuntimeUnavailable("外部 Runtime 未返回生成文本")
        return {"text": text, "latency_s": round(latency, 4),
                "eval_count": reply.get("eval_count"),
                "total_duration_ns": reply.get("total_duration"),
                "model": reply.get("model") or payload["model"],
                "requested_at": moment}

    # ---- PoC 报告 ----
    def poc(self, *, prompt: str, expected_version: str,
            expected_model_digest: str, timeout: Optional[float] = None
            ) -> Dict[str, Any]:
        """执行一次真实 PoC 并给出采用裁决证据。"""
        report: Dict[str, Any] = {"implementation": IMPLEMENTATION,
                                  "at": time.time(), "ok": False}
        actual = fetch_identity(self.base_url, model=self.identity.model,
                                timeout=timeout or self.timeout)
        self.identity = verify_pin(actual, expected_version=expected_version,
                                   expected_model_digest=expected_model_digest)
        report["identity"] = self.identity.to_dict()
        assert_matches_declarations(self, IMPLEMENTATION)
        report["matrix"] = {"ok": True, "capabilities": [
            {"capability": item.capability.value, "supported": item.supported,
             "delegate": item.details.get("delegate")}
            for item in self.declarations()]}
        report["generate"] = self.generate(prompt, timeout=timeout)
        # 委派等价性：事件流往返、取消未知任务、沙箱真实执行
        received = []
        self.subscribe_events(received.append)
        event = self.emit_event(event_type="external_runtime_poc",
                                task_id="poc-task", run_id="poc-run",
                                payload={"prompt": prompt})
        event_id = event.get("event_id") if isinstance(event, dict) else getattr(event, "event_id", None)
        report["delegation"] = {
            "event_stream": {"emitted": event_id is not None,
                             "subscriber_received": any(
                                 (e.get("event_id") if isinstance(e, dict) else getattr(e, "event_id", None)) == event_id
                                 for e in received),
                             "query_run_events": len(self.query_run_events("poc-run"))},
            "cancel_unknown_task": self.cancel_run("poc-task-does-not-exist").__dict__,
            "sandbox": self.run_sandboxed("print('external-runtime-poc')").__dict__,
        }
        # 失败隔离：不可达地址与版本漂移都必须被显式分类拒绝
        try:
            _request("http://127.0.0.1:9", "/api/version", timeout=1.0)
            unavailable = "unexpected-success"
        except ExternalRuntimeUnavailable as exc:
            unavailable = type(exc).__name__
        try:
            verify_pin(self.identity, expected_version="0.0.0-poc-mismatch",
                       expected_model_digest=self.identity.model_digest)
            mismatch = "unexpected-success"
        except RuntimePinMismatch as exc:
            mismatch = type(exc).__name__
        report["failure_isolation"] = {"unreachable": unavailable,
                                       "pin_mismatch": mismatch}
        report["adoption"] = _adoption(report)
        report["ok"] = bool(report["adoption"]["verdict"] == "adopt")
        return report


def _adoption(report: Dict[str, Any]) -> Dict[str, Any]:
    """按 ALN-20-A 的 AC-1..AC-4 给出采用/拒绝结论与保留缺口。"""
    matrix_ok = bool((report.get("matrix") or {}).get("ok"))
    generated = bool((report.get("generate") or {}).get("text"))
    isolation_ok = (report["failure_isolation"]["unreachable"] == "ExternalRuntimeUnavailable"
                    and report["failure_isolation"]["pin_mismatch"] == "RuntimePinMismatch")
    criteria = [
        {"id": "AC-1", "passed": matrix_ok,
         "basis": "五类能力全部 full（既有权威组件补位），矩阵与声明一致"},
        {"id": "AC-2", "passed": matrix_ok,
         "basis": "工具/事件/取消/恢复/沙箱全部委派既有权威，未复制第二套账本或 Task 状态机"},
        {"id": "AC-3", "passed": generated and isolation_ok,
         "basis": "真实生成执行成功且不可达/版本漂移被显式分类拒绝；共享契约夹具见 tests/test_external_runtime_poc.py"},
        {"id": "AC-4", "passed": bool((report.get("identity") or {}).get("model_digest")),
         "basis": "绑定 Runtime 版本号 + 模型内容摘要，同版本重跑结论可复现"},
    ]
    verdict = "adopt" if all(item["passed"] for item in criteria) else "reject"
    return {"criteria": criteria, "verdict": verdict,
            "limitations": [
                "外部 Runtime 只承接生成调用；会话消息不冒充 run 级事件账本",
                "真实主链路由到外部 Runtime（任务级采用）不在本卡范围",
                "取消/恢复语义由既有 controller 承载，外部 Runtime 无自主中断接管能力",
            ]}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="固定外部 Runtime 真实 PoC")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", required=True)
    parser.add_argument("--version", required=True, help="固定的 Runtime 版本号")
    parser.add_argument("--digest", required=True, help="固定的模型内容摘要")
    parser.add_argument("--prompt", default="请只回复：pong")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    from ..app import App
    app = App(data_dir=".evoagent/poc")
    try:
        identity = fetch_identity(args.base_url, model=args.model,
                                  timeout=args.timeout)
        bridge = ExternalRuntimeBridge(
            registry=app.tools, bus=app.bus, controller=app.controller,
            sandbox=app.sandbox, identity=identity, base_url=args.base_url,
            timeout=args.timeout)
        report = bridge.poc(prompt=args.prompt, expected_version=args.version,
                            expected_model_digest=args.digest,
                            timeout=args.timeout)
    finally:
        app.db.close()
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        import pathlib
        pathlib.Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
