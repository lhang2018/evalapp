"""Authentication errors remain readable when the request body arrives later."""
import http.client
import json
import threading
import time

from evoagent.api.server import ApiServer
from evoagent.app import App


def test_rejected_headers_then_delayed_body_returns_auth_json(tmp_path):
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "operator"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    payload = b'{"goal":"must not execute"}'
    try:
        connection.putrequest("POST", "/tasks")
        connection.putheader("Content-Length", str(len(payload)))
        connection.putheader("Authorization", "Bearer wrong")
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


def test_missing_unauthorized_body_does_not_block_rejection(tmp_path):
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "operator"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    try:
        connection.putrequest("POST", "/tasks")
        connection.putheader("Content-Length", "100")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 401
        assert json.loads(response.read())["error_code"]
        assert app.db.list_tasks() == []
    finally:
        connection.close()
        server.shutdown()
        thread.join(5)
        app.db.close()
