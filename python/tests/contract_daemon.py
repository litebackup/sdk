"""A fake litebackup daemon on a unix socket, for the contract fixtures and unit tests."""

from __future__ import annotations

import contextlib
import json
import os
import socket
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
STATUS_BODY = b'{"session":"contract","server":"http://example","client_version":"1.1.0","session_id":"s","watch":"inotify","databases":[]}'


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class ContractDaemon:
    """Answers the connect probe itself and hands every other request to reply."""

    def __init__(self, reply: Callable[[Received], Reply], client_version: str | None = "1.1.0") -> None:
        self.directory = tempfile.mkdtemp(prefix="lbp-")
        self.socket_path = os.path.join(self.directory, "client.sock")
        self.received: list[Received] = []
        self.sdk_versions: set[str] = set()
        # Set by stop: connections still open close without an answer, as a daemon that exits leaves them.
        self.stopped = False
        # Accepted connections, which stop shuts down.
        self._connections: set[socket.socket] = set()
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def setup(self) -> None:
                super().setup()
                daemon._connections.add(self.connection)

            def finish(self) -> None:
                daemon._connections.discard(self.connection)
                super().finish()

            def _answer(self) -> None:
                if daemon.stopped:
                    self.close_connection = True
                    return
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                received = Received(
                    self.command, url.path, dict(parse_qsl(url.query, keep_blank_values=True)), self.rfile.read(length), dict(self.headers)
                )
                daemon.sdk_versions.add(self.headers.get("Litebackup-SDK-Version", ""))
                if received.method == "GET" and received.path == "/v1/status/show" and received.query == {"limit": "1"}:
                    status, content_type, body = 200, "application/json", STATUS_BODY
                else:
                    daemon.received.append(received)
                    status, content_type, body = reply(received)
                if daemon.stopped:
                    # The daemon stopped while this request waited: it gets no answer.
                    self.close_connection = True
                    return
                self.send_response(status)
                if client_version is not None:
                    self.send_header("Litebackup-Client-Version", client_version)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _answer
            do_POST = _answer

        self._handler = Handler
        self.serve()

    def serve(self) -> None:
        """Listens on the socket and answers there until stop."""
        self.stopped = False
        self._server = _Server(self.socket_path, self._handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Closes the socket and every connection, as a daemon that exits does; calls then fail with connection_failed until serve."""
        if self.stopped:
            return
        self.stopped = True
        self._server.shutdown()
        self._server.server_close()
        for connection in list(self._connections):
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            os.unlink(self.socket_path)

    def close(self) -> None:
        self.stop()
        with contextlib.suppress(OSError):
            os.rmdir(self.directory)

    def check_sdk_version(self) -> None:
        assert self.sdk_versions <= {f"python/{SDK_VERSION}"}, self.sdk_versions
