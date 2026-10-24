"""Authentication errors remain readable when the request body arrives later."""
import http.client
import json
import threading
import time
from types import SimpleNamespace

import pytest

from evoagent.api.server import ApiServer, make_handler
from evoagent.app import App


@pytest.mark.parametrize("path,token", [
    ("/tasks", "wrong"),
    ("/goal-policies", "operator"),
    ("/goals/nonexistent/advance", "operator"),
])
def test_rejected_headers_then_delayed_body_returns_auth_json(tmp_path, path, token):
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "operator"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    payload = b'{"goal":"must not execute"}'
    try:
        connection.putrequest("POST", path)
        connection.putheader("Content-Length", str(len(payload)))
        connection.putheader("Authorization", "Bearer " + token)
        connection.endheaders()
        time.sleep(0.03)
        connection.send(payload)
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read())["error_code"]
        assert app.db.list_tasks() == []
    finally:
        connection.close()
        server.shutdown()
        thread.join(5)
        app.db.close()


@pytest.mark.parametrize("path,token,status", [
    ("/tasks", None, 401),
    ("/goal-policies", "operator", 403),
])
def test_missing_unauthorized_body_does_not_block_rejection(tmp_path, path, token, status):
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "operator"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    try:
        connection.putrequest("POST", path)
        connection.putheader("Content-Length", "100")
        if token:
            connection.putheader("Authorization", "Bearer " + token)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == status
        assert json.loads(response.read())["error_code"]
        assert app.db.list_tasks() == []
    finally:
        connection.close()
        server.shutdown()
        thread.join(5)
        app.db.close()


@pytest.mark.parametrize("consumed", [False, True])
def test_domain_rejection_drains_only_unread_body(consumed):
    handler = object.__new__(make_handler(None))
    calls = []
    handler._query = lambda: {}
    handler._body = lambda: calls.append("body") or {}
    handler._drain_rejected_body = lambda: calls.append("drain")
    handler._json = lambda code, payload: calls.append((code, payload))
    ctx = handler._ctx("POST", "/goal-policies", SimpleNamespace(role="operator"))
    assert calls == []  # Preserve lazy dispatch, including raw fallback routes.
    if consumed:
        assert ctx.body == {}
    ctx.json(403, {"error": "admin required"})
    assert calls == (["body"] if consumed else ["drain"]) + [
        (403, {"error": "admin required"})]
