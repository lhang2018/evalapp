"""Early diagnostic rejection must not close over an unread small upload."""
import io
from types import SimpleNamespace

import pytest

from evoagent.api.server import make_handler


class Connection:
    def __init__(self):
        self.timeout = 4

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value


@pytest.mark.parametrize("actor,content_type,expected", [
    ("admin", "text/plain", 415), ("operator", "application/zip", 403),
])
def test_rejected_small_upload_is_discarded_without_parsing(actor, content_type, expected):
    handler = object.__new__(make_handler(SimpleNamespace()))
    body = b"not parsed as a diagnostic package"
    handler.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
    handler.connection = Connection()
    handler.rfile = io.BytesIO(body)
    responses = []
    handler._json = lambda status, payload: responses.append((status, payload))
    handler._diagnostics_import(actor)
    assert responses[0][0] == expected
    assert handler.rfile.tell() == len(body)
    assert handler.connection.timeout == 4
    assert handler.close_connection is True
