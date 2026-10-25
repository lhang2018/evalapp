"""ALN-02-P：真实外部供应商副作用与进程故障恢复测试。

用真实本地 HTTP 服务充当「受控外部测试服务」，验证：
- 真实外部副作用只执行一次（供应商按幂等键去重，本地账本缓存重放）；
- 超时/响应丢失 → 副作用未知、禁止盲目重试，必须先按供应商权威回执对账；
- 进程在「已提交、未确认」窗口被强杀 → 重启后凭供应商回执恢复，不重复执行；
- 供应商明确无回执 → 证明未执行，允许一次全新尝试；
- 调用方回执与供应商权威记录不一致 → 拒绝确认，保持 unknown。
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from evoagent.core.permissions import PermissionController
from evoagent.core.persistence import Database
from evoagent.tools.registry import ToolRegistry
from evoagent.tools.vendor_http import (VendorClient, VendorEffectAdapter,
                                        VendorError, client_key_for)


class VendorState:
    def __init__(self):
        self.lock = threading.Lock()
        self.receipts = {}
        self.executions = 0
        self.requests = 0
        self.arrived = threading.Event()


@pytest.fixture
def vendor():
    state = VendorState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102 —— 测试保持安静
            pass

        def _json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/effects":
                return self._json(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            key = str(body.get("client_key") or "")
            payload = body.get("payload") or {}
            if payload.get("corrupt"):
                # 供应商返回不可解析正文，且不记录任何副作用
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"not-json")
                return
            if payload.get("reject"):
                return self._json(409, {"error": "rejected"})
            with state.lock:
                state.requests += 1
                receipt = state.receipts.get(key)
                if receipt is None:
                    receipt = {
                        "receipt_id": "rcpt-%d" % (len(state.receipts) + 1),
                        "client_key": key,
                        "status": "applied",
                        "result": {"external_id": "ext-" + key[-8:],
                                   "operation": body.get("operation"),
                                   "payload": payload},
                    }
                    state.receipts[key] = receipt
                    state.executions += 1
                state.arrived.set()
            delay = float(payload.get("delay") or 0)
            if delay:
                time.sleep(delay)
            self._json(200, receipt)

        def do_GET(self):
            if not self.path.startswith("/receipts/"):
                return self._json(404, {"error": "not found"})
            key = self.path.rsplit("/", 1)[-1]
            with state.lock:
                receipt = state.receipts.get(key)
            if receipt is None:
                return self._json(404, {"error": "no receipt"})
            self._json(200, receipt)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port, state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def gateway(tmp_path, vendor):
    base_url, state = vendor
    db = Database(str(tmp_path / "effects.db"))
    tools = ToolRegistry(PermissionController(high_risk_mode="allow"), db)
    adapter = VendorEffectAdapter(base_url, timeout_policy=2.0)
    tools.register(adapter.contract, adapter.runner)
    try:
        yield tools, adapter, db, base_url, state
    finally:
        db.close()


ARGS = {"operation": "charge", "payload": {"amount": 7}}


def test_real_http_effect_executes_once_and_replays_from_ledger(gateway):
    tools, adapter, db, _url, state = gateway
    result = tools.execute(adapter.tool_id, ARGS, task_id="task",
                           idempotency_key="op-1")
    assert result.ok and result.executed
    assert result.data["receipt_id"].startswith("rcpt-")
    assert result.evidence["external_receipt_ref"].startswith("vendor:rcpt-")
    assert state.executions == 1
    replay = tools.execute(adapter.tool_id, ARGS, task_id="task",
                           idempotency_key="op-1")
    assert replay.evidence.get("replayed") is True
    assert replay.data["receipt_id"] == result.data["receipt_id"]
    assert state.executions == 1


def test_lost_response_blocks_retry_until_vendor_receipt_verified(gateway):
    tools, adapter, db, base_url, state = gateway
    # 客户端侧超时先发生（供应商已记录副作用，响应尚未返回）
    impatient = VendorEffectAdapter(base_url, timeout_policy=2.0,
                                    client=VendorClient(base_url, timeout=0.2))
    tools.register(impatient.contract, impatient.runner)
    args = {"operation": "charge", "payload": {"amount": 9, "delay": 0.35}}
    result = tools.execute(impatient.tool_id, args, task_id="task",
                           idempotency_key="slow-1")
    assert result.ok is False and result.side_effect_uncertain is True
    assert result.required_action == "reconcile"
    assert result.evidence["effect_id"]
    effect_id = result.evidence["effect_id"]
    assert tools.effects.get(effect_id)["status"] == "unknown"
    assert state.executions == 1
    # 未对账前禁止重试
    blocked = tools.execute(impatient.tool_id, args, task_id="task",
                            idempotency_key="slow-1")
    assert blocked.error_class == "side_effect_unknown"
    assert state.executions == 1
    # 按供应商权威回执对账 → confirmed；之后同一幂等键只重放缓存
    reconciled = tools.reconcile_effect(effect_id, {})
    assert reconciled.independently_verified and reconciled.executed
    assert reconciled.evidence["external_receipt_ref"].startswith("vendor:rcpt-")
    assert tools.effects.get(effect_id)["status"] == "confirmed"
    replay = tools.execute(impatient.tool_id, args, task_id="task",
                           idempotency_key="slow-1")
    assert replay.evidence.get("replayed") is True
    assert state.executions == 1


def test_forged_receipt_is_rejected_and_effect_stays_unknown(gateway):
    tools, adapter, db, base_url, state = gateway
    impatient = VendorEffectAdapter(base_url, timeout_policy=2.0,
                                    client=VendorClient(base_url, timeout=0.2))
    tools.register(impatient.contract, impatient.runner)
    args = {"operation": "charge", "payload": {"amount": 5, "delay": 0.35}}
    result = tools.execute(impatient.tool_id, args, task_id="task",
                           idempotency_key="forged-1")
    effect_id = result.evidence["effect_id"]
    with pytest.raises(VendorError):
        tools.reconcile_effect(effect_id, {"receipt_id": "rcpt-forged"})
    assert tools.effects.get(effect_id)["status"] == "unknown"
    # 真实回执才放行
    tools.reconcile_effect(effect_id, {})
    assert tools.effects.get(effect_id)["status"] == "confirmed"


def test_absent_receipt_proves_not_executed_and_allows_one_new_attempt(gateway):
    tools, adapter, db, base_url, state = gateway
    result = tools.execute(adapter.tool_id,
                           {"operation": "charge", "payload": {"corrupt": True}},
                           task_id="task", idempotency_key="corrupt-1")
    assert result.side_effect_uncertain is True
    effect_id = result.evidence["effect_id"]
    assert tools.effects.get(effect_id)["status"] == "unknown"
    assert state.executions == 0
    reconciled = tools.reconcile_effect(effect_id, {})
    assert reconciled.independently_verified is True
    assert reconciled.executed is False
    assert tools.effects.get(effect_id)["status"] == "failed"
    fresh = tools.execute(adapter.tool_id, ARGS, task_id="task",
                          idempotency_key="corrupt-2")
    assert fresh.ok and state.executions == 1


CHILD_SCRIPT = r"""
import json
import sys
from evoagent.core.permissions import PermissionController
from evoagent.core.persistence import Database
from evoagent.tools.registry import ToolRegistry
from evoagent.tools.vendor_http import VendorEffectAdapter

db = Database(sys.argv[1])
tools = ToolRegistry(PermissionController(high_risk_mode="allow"), db)
adapter = VendorEffectAdapter(sys.argv[2], timeout_policy=30.0)
tools.register(adapter.contract, adapter.runner)
result = tools.execute(adapter.tool_id,
                       {"operation": "charge",
                        "payload": {"amount": 11, "delay": 5}},
                       task_id="task-crash", idempotency_key="crash-1")
print(json.dumps({"ok": result.ok}))
"""


def test_process_killed_after_submit_is_recovered_by_vendor_receipt(gateway):
    tools, adapter, db, base_url, state = gateway
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    root = os.getcwd()
    proc = subprocess.Popen([sys.executable, "-c", CHILD_SCRIPT, db.path, base_url],
                            cwd=root, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # 供应商已收到请求（副作用已真实发生），子进程还没拿到响应就被强杀
        assert state.arrived.wait(15), "供应商未收到请求"
        proc.kill()
        proc.wait(15)
        assert proc.returncode != 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    assert state.executions == 1
    history = tools.effects.history("task-crash")
    assert len(history) == 1, history
    effect_id, status = history[0]["effect_id"], history[0]["status"]
    # 崩溃窗口内：已提交、未确认，绝不当成“未执行”
    assert status in ("submitted", "unknown"), status
    # 重启后对新连接按供应商权威回执对账
    rebuilt = ToolRegistry(PermissionController(high_risk_mode="allow"), db)
    rebuilt_adapter = VendorEffectAdapter(base_url, timeout_policy=2.0)
    rebuilt.register(rebuilt_adapter.contract, rebuilt_adapter.runner)
    reconciled = rebuilt.reconcile_effect(effect_id, {})
    assert reconciled.independently_verified and reconciled.executed
    assert rebuilt.effects.get(effect_id)["status"] == "confirmed"
    replay = rebuilt.execute(rebuilt_adapter.tool_id,
                             {"operation": "charge",
                              "payload": {"amount": 11, "delay": 5}},
                             task_id="task-crash", idempotency_key="crash-1")
    assert replay.evidence.get("replayed") is True
    assert state.executions == 1  # 崩溃恢复全程没有第二次外部执行


def test_vendor_deduplicates_repeated_submission_by_client_key(vendor):
    base_url, state = vendor
    client = VendorClient(base_url, timeout=2.0)
    key = client_key_for(ARGS)
    first = client.submit(key, "charge", {"amount": 7})
    second = client.submit(key, "charge", {"amount": 7})
    assert first["receipt_id"] == second["receipt_id"]
    assert state.executions == 1
