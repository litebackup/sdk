"""A fake litebackup daemon on a unix socket, for the contract fixtures and unit tests."""

from __future__ import annotations

import json
import os
import socketserver
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qsl, urlsplit

from litebackup import SDK_VERSION


@dataclass
class Received:
    method: str
    path: str
    query: dict[str, str]
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> object:
        return json.loads(self.body) if self.body else None


Reply = tuple[int, str, bytes]
STATUS_BODY = b'{"session":"contract","server":"http://example","client_version":"1.0.0","session_id":"s","watch":"inotify","databases":[]}'


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class ContractDaemon:
    """Answers the connect probe itself and hands every other request to reply."""

    def __init__(self, reply: Callable[[Received], Reply], client_version: str | None = "1.0.0") -> None:
        self.directory = tempfile.mkdtemp(prefix="lbp-")
        self.socket_path = os.path.join(self.directory, "client.sock")
        self.received: list[Received] = []
        self.sdk_versions: set[str] = set()
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _answer(self) -> None:
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                received = Received(
                    self.command, url.path, dict(parse_qsl(url.query, keep_blank_values=True)), self.rfile.read(length), dict(self.headers)
                )
                daemon.sdk_versions.add(self.headers.get("X-Litebackup-SDK-Version", ""))
                if received.method == "GET" and received.path == "/v1/status/show" and received.query == {"limit": "1"}:
                    status, content_type, body = 200, "application/json", STATUS_BODY
                else:
                    daemon.received.append(received)
                    status, content_type, body = reply(received)
                self.send_response(status)
                if client_version is not None:
                    self.send_header("X-Litebackup-Client-Version", client_version)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _answer
            do_POST = _answer

        self._server = _Server(self.socket_path, Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        try:
            os.unlink(self.socket_path)
            os.rmdir(self.directory)
        except OSError:
            pass

    def check_sdk_version(self) -> None:
        assert self.sdk_versions <= {f"python/{SDK_VERSION}"}, self.sdk_versions
