"""ALN-20-P：固定外部 Runtime 真实 PoC 测试。

外部 Runtime 协议用本地真实 HTTP 服务（模拟 Ollama `/api/version`、`/api/tags`、
`/api/generate`）驱动；真实 Ollama 0.40.11 + 本地模型的实跑证据另存
`.planning/2026-10-25-aln20p/poc-evidence.json`（见交付文档）。
五类能力全部委派 App 既有权威组件（ToolRegistry/EventBus/controller/Sandbox）。
"""

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from evoagent.app import App
from evoagent.runtime import RuntimeCapability, assert_matches_declarations
from evoagent.runtime.external_poc import (IMPLEMENTATION, ExternalRuntimeBridge,
                                           ExternalRuntimeUnavailable,
                                           RuntimePinMismatch, fetch_identity,
                                           verify_pin)

VERSION = "0.0.0-fake"
MODEL = "fake:1b"
DIGEST = "b" * 64


def _load_contract_suite():
    path = Path(__file__).with_name("test_runtime_protocol.py")
    spec = importlib.util.spec_from_file_location("runtime_contract_suite", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run_contract_suite


run_contract_suite = _load_contract_suite()


class FakeOllama:
    def __init__(self):
        self.delay = 0.0
        self.text = "pong"
        self.version = VERSION
        self.digest = DIGEST
        self.requests = []


@pytest.fixture
def runtime_server():
    state = FakeOllama()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state.requests.append(("GET", self.path))
            if self.path == "/api/version":
                return self._json(200, {"version": state.version})
            if self.path == "/api/tags":
                return self._json(200, {"models": [
                    {"name": MODEL, "digest": state.digest}]})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            state.requests.append(("POST", self.path, payload))
            if self.path != "/api/generate":
                return self._json(404, {"error": "not found"})
            if state.delay:
                time.sleep(state.delay)
            return self._json(200, {"model": payload.get("model"),
                                    "response": state.text,
                                    "eval_count": 3,
                                    "total_duration": 1234})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = "http://127.0.0.1:%d" % server.server_port
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def app(tmp_path):
    application = App(data_dir=str(tmp_path))
    yield application
    application.db.close()


def bridge(app, runtime_server, **overrides):
    identity = fetch_identity(runtime_server.url, model=MODEL)
    values = dict(registry=app.tools, bus=app.bus, controller=app.controller,
                  sandbox=app.sandbox, identity=identity,
                  base_url=runtime_server.url, timeout=5.0)
    values.update(overrides)
    return ExternalRuntimeBridge(**values)


def test_shared_contract_suite_passes_on_external_bridge(app, runtime_server):
    runtime = bridge(app, runtime_server)
    results = run_contract_suite(runtime)
    assert results == {"events": True, "cancel": True, "recovery": True,
                       "sandbox": "executed"}
    assert_matches_declarations(runtime, IMPLEMENTATION)
    capabilities = {item.capability for item in runtime.declarations()}
    assert capabilities == set(RuntimeCapability)


def test_identity_pins_version_and_model_digest(runtime_server):
    identity = fetch_identity(runtime_server.url, model=MODEL)
    assert identity.version == VERSION and identity.model_digest == DIGEST
    assert verify_pin(identity, expected_version=VERSION,
                      expected_model_digest=DIGEST) == identity
    with pytest.raises(RuntimePinMismatch):
        verify_pin(identity, expected_version="9.9.9",
                   expected_model_digest=DIGEST)
    with pytest.raises(RuntimePinMismatch):
        verify_pin(identity, expected_version=VERSION,
                   expected_model_digest="c" * 64)


def test_missing_authority_is_rejected_not_degraded(app, runtime_server):
    identity = fetch_identity(runtime_server.url, model=MODEL)
    with pytest.raises(ValueError):
        ExternalRuntimeBridge(registry=None, bus=app.bus,
                              controller=app.controller, sandbox=app.sandbox,
                              identity=identity, base_url=runtime_server.url)


def test_real_generate_calls_http_and_classifies_timeout(app, runtime_server):
    runtime = bridge(app, runtime_server)
    result = runtime.generate("请只回复 pong")
    assert result["text"] == "pong"
    assert result["eval_count"] == 3
    assert result["latency_s"] >= 0
    assert any(item[0] == "POST" and item[1] == "/api/generate"
               for item in runtime_server.requests)
    runtime_server.delay = 1.0
    with pytest.raises(ExternalRuntimeUnavailable):
        runtime.generate("慢请求", timeout=0.1)


def test_unreachable_runtime_is_isolated(app, runtime_server):
    identity = fetch_identity(runtime_server.url, model=MODEL)
    runtime = ExternalRuntimeBridge(
        registry=app.tools, bus=app.bus, controller=app.controller,
        sandbox=app.sandbox, identity=identity,
        base_url="http://127.0.0.1:9", timeout=0.5)
    with pytest.raises(ExternalRuntimeUnavailable):
        runtime.generate("不可达")
    # 失败被隔离：事件/取消等既有权威仍可用
    event = runtime.emit_event(event_type="isolation-check",
                               task_id="t1", run_id="r1", payload={})
    assert event is not None
    assert runtime.cancel_run("missing-task").ok is False


def test_poc_report_evaluates_adoption_criteria(app, runtime_server):
    runtime = bridge(app, runtime_server)
    report = runtime.poc(prompt="ping", expected_version=VERSION,
                         expected_model_digest=DIGEST)
    assert report["ok"] is True
    assert report["identity"]["version"] == VERSION
    assert report["identity"]["model_digest"] == DIGEST
    assert report["matrix"]["ok"] is True
    assert report["generate"]["text"] == "pong"
    assert report["delegation"]["event_stream"]["subscriber_received"] is True
    assert report["delegation"]["sandbox"]["ok"] is True
    assert report["failure_isolation"] == {
        "unreachable": "ExternalRuntimeUnavailable",
        "pin_mismatch": "RuntimePinMismatch"}
    adoption = report["adoption"]
    assert adoption["verdict"] == "adopt"
    assert {item["id"] for item in adoption["criteria"]} == {
        "AC-1", "AC-2", "AC-3", "AC-4"}
    assert all(item["passed"] for item in adoption["criteria"])
    assert adoption["limitations"]


def test_poc_refuses_pin_drift_before_executing(app, runtime_server):
    runtime = bridge(app, runtime_server)
    with pytest.raises(RuntimePinMismatch):
        runtime.poc(prompt="ping", expected_version=VERSION,
                    expected_model_digest="d" * 64)
    # 拒绝发生在真实生成之前：没有发出 /api/generate
    assert not any(item[0] == "POST" for item in runtime_server.requests)
