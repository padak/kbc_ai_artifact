"""Tests for src.kbc: how a credential reaches the Storage Files API.

Only the authentication seam is covered here, twice over: the header map a
``kbcstorage`` client ends up with (building one touches no network), and what
a request actually puts on the wire, observed by pointing the backend at a
loopback server this module runs itself. No Keboola stack is contacted.
Everything else in :mod:`src.kbc` is exercised through ``InMemoryFilesBackend``
in the store and API tests.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.kbc import BackendError, KbcFilesBackend

STACK = "https://connection.keboola.com"


class TestAuthentication:
    def test_a_storage_token_keeps_the_libraries_own_header(self):
        backend = KbcFilesBackend(STACK, "1234-abcdef")
        headers = backend._files()._auth_header
        assert headers["X-StorageApi-Token"] == "1234-abcdef"
        assert "Authorization" not in headers

    def test_a_session_bearer_replaces_it_with_a_project_scoped_one(self):
        backend = KbcFilesBackend(STACK, "kbc_at_abc_secret", 123)
        headers = backend._files()._auth_header
        assert headers["Authorization"] == "Bearer kbc_at_abc_secret"
        assert headers["X-KBC-ProjectId"] == "123"
        # requests drops a header whose value is None, which is how the
        # library's own token header is taken out of the request.
        assert headers["X-StorageApi-Token"] is None

    def test_a_personal_access_token_authenticates_the_same_way(self):
        backend = KbcFilesBackend(STACK, "kbc_pat_abc_secret", 7)
        headers = backend._files()._auth_header
        assert headers["Authorization"] == "Bearer kbc_pat_abc_secret"
        assert headers["X-KBC-ProjectId"] == "7"

    def test_a_bearer_without_a_project_fails_as_a_backend_error(self):
        """The caller sees a Storage failure, not an auth exception it cannot catch."""
        backend = KbcFilesBackend(STACK, "kbc_at_abc_secret")
        with pytest.raises(BackendError) as excinfo:
            backend._files()
        assert "X-Storage-Project" in str(excinfo.value)


class _RecordingStack:
    """A localhost stand-in for a stack that records the headers it receives.

    Only the Storage Files *listing* is answered, which is enough to observe
    what a request actually carries on the wire. Nothing here talks to
    Keboola; the backend is pointed at this server's own address.
    """

    def __init__(self) -> None:
        self.headers: list[dict[str, str]] = []
        received = self.headers

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                received.append({k.lower(): v for k, v in self.headers.items()})
                body = json.dumps([]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                """Silence the default stderr access log."""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "_RecordingStack":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class TestHeadersOnTheWire:
    """What the request actually carries, not just what the header map holds.

    ``kbcstorage`` hard-codes ``X-StorageApi-Token``; a bearer credential
    removes it by setting it to ``None``, which only works because ``requests``
    drops a header with that value. Asserting the map alone would not catch
    that behaviour changing.
    """

    def test_a_storage_token_is_sent_as_the_storage_header(self):
        with _RecordingStack() as stack:
            KbcFilesBackend(stack.url, "1234-abcdef").search_by_tag("some-tag")
        sent = stack.headers[0]
        assert sent["x-storageapi-token"] == "1234-abcdef"
        assert "authorization" not in sent

    def test_a_bearer_is_sent_without_the_storage_header(self):
        with _RecordingStack() as stack:
            KbcFilesBackend(stack.url, "kbc_at_abc_secret", 123).search_by_tag("t")
        sent = stack.headers[0]
        assert sent["authorization"] == "Bearer kbc_at_abc_secret"
        assert sent["x-kbc-projectid"] == "123"
        assert "x-storageapi-token" not in sent
