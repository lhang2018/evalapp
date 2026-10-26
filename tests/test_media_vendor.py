"""ALN-19-A：真实音视频供应商切片测试（本地 HTTP 夹具供应商）。

明确标注：本测试使用**本地 HTTP 夹具供应商**验证供应商契约、真实 HTTP
传输、配置/预算校验、费用/版本/授权追溯与失败恢复；**不是**真实供应商
或生产实测。真实凭据与预算未提供前不冒称 19-A 关闭。
"""

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from evoagent.tools.media_vendor import (MediaVendorClient, MediaVendorConfig,
                                         MediaVendorError, MediaVendorRefused)


class _Recorder:
    def __init__(self):
        self.rows = []

    def record(self, actor, action, *, target="", detail=None):
        self.rows.append({"actor": actor, "action": action,
                          "target": target, "detail": detail or {}})
        return "aud_" + str(len(self.rows))


class VendorFixture:
    """本地 HTTP 夹具供应商（仅测试用，非真实供应商）。"""

    def __init__(self):
        self.requests = []
        self.healthy = True
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                fixture.requests.append({"path": self.path,
                                         "payload": payload,
                                         "auth": self.headers.get("Authorization")})
                if not fixture.healthy:
                    return self._send(500, {"error": "vendor down"})
                if self.path == "/v1/clips":
                    content = ("fixture-video:" + payload["prompt"]).encode()
                    return self._send(200, {
                        "asset_id": "clip_fixture_1",
                        "content_b64": base64.b64encode(content).decode(),
                        "content_type": "video/mp4",
                        "vendor_cost_usd": round(float(payload["duration_s"]) * 0.02, 6)})
                if self.path == "/v1/edits":
                    if payload.get("asset_ref") == "missing":
                        return self._send(404, {"error": "asset not found"})
                    content = "fixture-edited".encode()
                    return self._send(200, {
                        "asset_id": "edit_fixture_1",
                        "content_b64": base64.b64encode(content).decode(),
                        "vendor_cost_usd": round(0.05 * len(payload["ops"]), 6)})
                return self._send(404, {"error": "unknown path"})

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def vendor():
    fixture = VendorFixture()
    yield fixture
    fixture.close()


def config(vendor, **overrides):
    values = dict(provider="fixture-vendor", base_url=vendor.base_url,
                  api_key="fixture-key", model_version="vendor-v1",
                  budget_usd=1.0, price_per_second_usd=0.02,
                  edit_price_usd=0.05, timeout_s=10.0)
    values.update(overrides)
    return MediaVendorConfig(**values)


def test_missing_config_refuses(monkeypatch):
    client = MediaVendorClient(MediaVendorConfig())
    with pytest.raises(MediaVendorRefused):
        client.render_clip("cat", duration_s=5, actor="user", reason="生成")
    with pytest.raises(MediaVendorRefused):
        client.edit_clip("clip_1", ["trim"], actor="user", reason="剪辑")
    assert client.report()["config"]["configured"] is False


def test_render_real_http_with_cost_version_authorization_trace(vendor):
    audit = _Recorder()
    client = MediaVendorClient(config(vendor), audit=audit)
    result = client.render_clip("海边日落", duration_s=5, actor="user-a",
                                reason="生成短片")
    assert result["ok"] is True
    assert result["asset"]["asset_id"] == "clip_fixture_1"
    raw = ("fixture-video:海边日落").encode()
    assert result["asset"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["cost_usd"] == 0.1
    assert result["remaining_usd"] == 0.9
    assert vendor.requests[0]["auth"] == "Bearer fixture-key"
    assert vendor.requests[0]["payload"]["model_version"] == "vendor-v1"
    assert audit.rows[0]["action"] == "media_render_clip"
    assert audit.rows[0]["detail"]["provider"] == "fixture-vendor"
    assert audit.rows[0]["detail"]["model_version"] == "vendor-v1"
    assert audit.rows[0]["actor"] == "user-a"


def test_budget_refusal_does_not_call_vendor(vendor):
    client = MediaVendorClient(config(vendor, budget_usd=0.1))
    with pytest.raises(MediaVendorRefused):
        client.render_clip("cat", duration_s=10, actor="user", reason="超额")
    assert vendor.requests == []
    assert client.spent_usd == 0.0


def test_edit_allowed_and_refused_paths(vendor):
    audit = _Recorder()
    client = MediaVendorClient(config(vendor), audit=audit)
    result = client.edit_clip("clip_fixture_1", ["trim", "subtitle"],
                              actor="user-a", reason="剪辑")
    assert result["ok"] is True
    assert result["asset"]["asset_id"] == "edit_fixture_1"
    assert result["cost_usd"] == 0.1
    with pytest.raises(MediaVendorRefused):
        client.edit_clip("clip_1", ["unknown-op"], actor="user-a", reason="x")
    with pytest.raises(MediaVendorError):
        client.edit_clip("missing", ["trim"], actor="user-a", reason="不存在")
    assert client.spent_usd == 0.1  # 失败未扣费
    assert client.failures[-1]["refunded"] is True
    actions = [row["action"] for row in audit.rows]
    assert "media_edit_clip_failed" in actions


def test_vendor_failure_then_recovery(vendor):
    client = MediaVendorClient(config(vendor))
    vendor.healthy = False
    with pytest.raises(MediaVendorError):
        client.render_clip("cat", duration_s=5, actor="user", reason="失败")
    assert client.spent_usd == 0.0
    assert client.failures
    vendor.healthy = True
    result = client.render_clip("cat", duration_s=5, actor="user", reason="恢复")
    assert result["ok"] is True
    assert client.spent_usd == 0.1
    assert client.report()["calls"] == 1
    assert client.report()["failures"] == 1


def test_env_config_parsing(monkeypatch):
    cfg = MediaVendorConfig.from_env({
        "EVOAGENT_MEDIA_VENDOR": "v", "EVOAGENT_MEDIA_BASE_URL": "http://x",
        "EVOAGENT_MEDIA_API_KEY": "k", "EVOAGENT_MEDIA_MODEL_VERSION": "m",
        "EVOAGENT_MEDIA_BUDGET_USD": "2.5",
        "EVOAGENT_MEDIA_PRICE_PER_SECOND": "0.03"})
    assert cfg.ready is True and cfg.budget_usd == 2.5
    assert cfg.public()["api_key_configured"] is True
