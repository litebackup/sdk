"""HTTP/1.1 over the daemon's unix socket with h11: blocking sockets and a small keep-alive pool."""

from __future__ import annotations

import contextlib
import select
import socket
import threading
import time

import h11

from ._wire import CLIENT_VERSION_HEADER, RESPONSE_LIMIT, SDK_VERSION_HEADER, SDK_VERSION_VALUE
from .errors import ErrorCode, LitebackupError

POOL_SIZE = 4
READ_SIZE = 65536


class Answer:
    """A whole answer read from the socket."""

    __slots__ = ("body", "client_version", "status")

    def __init__(self, status: int, client_version: str, body: bytes) -> None:
        self.status = status
        self.client_version = client_version
        self.body = body


def request_events(method: str, target: str, body: bytes | None) -> list[h11.Request | h11.Data | h11.EndOfMessage]:
    """The h11 events of one request."""
    headers = [("Host", "litebackup"), (SDK_VERSION_HEADER, SDK_VERSION_VALUE)]
    if body is not None:
        headers += [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
    events: list[h11.Request | h11.Data | h11.EndOfMessage] = [h11.Request(method=method, target=target, headers=headers)]
    if body:
        events.append(h11.Data(data=body))
    events.append(h11.EndOfMessage())
    return events


class AnswerReader:
    """Collects one answer from h11 events."""

    def __init__(self) -> None:
        self.status = 0
        self.client_version = ""
        self.chunks: list[bytes] = []
        self.size = 0
        self.started = False

    def accept(self, event: object) -> bool:
        """Takes one event; reports True once the answer is complete."""
        if isinstance(event, h11.Response):
            self.started = True
            self.status = event.status_code
            for name, value in event.headers:
                if name.decode("latin-1").lower() == CLIENT_VERSION_HEADER:
                    self.client_version = value.decode("latin-1")
        elif isinstance(event, h11.Data):
            self.size += len(event.data)
            if self.size > RESPONSE_LIMIT:
                raise LitebackupError(ErrorCode.INVALID_RESPONSE, "the daemon's answer is too large")
            self.chunks.append(bytes(event.data))
        elif isinstance(event, h11.EndOfMessage):
            return True
        elif isinstance(event, h11.ConnectionClosed):
            raise ConnectionResetError("the daemon closed the connection before answering")
        return False

    def answer(self) -> Answer:
        return Answer(self.status, self.client_version, b"".join(self.chunks))


class _Connection:
    __slots__ = ("protocol", "socket")

    def __init__(self, connection: socket.socket) -> None:
        self.socket = connection
        self.protocol = h11.Connection(our_role=h11.CLIENT)

    def dropped(self) -> bool:
        """An idle connection the daemon closed reads as ready."""
        readable, _, _ = select.select([self.socket], [], [], 0)
        return bool(readable)


class SyncTransport:
    """Sends requests over a unix socket. Close ends calls in flight."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._lock = threading.Lock()
        self._idle: list[_Connection] = []
        self._active: set[_Connection] = set()
        self.closed = False

    def _pooled_connection(self) -> tuple[_Connection, bool]:
        with self._lock:
            if self.closed:
                raise LitebackupError(ErrorCode.CLOSED, "the client is closed")
            while self._idle:
                connection = self._idle.pop()
                if connection.dropped():
                    connection.socket.close()
                    continue
                self._active.add(connection)
                return connection, True
            # Registered before connecting, so close() sees it; _exchange checks again once connected.
            connection = _Connection(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            self._active.add(connection)
        return connection, False

    def _return_connection(self, connection: _Connection, reusable: bool) -> None:
        with self._lock:
            self._active.discard(connection)
            if reusable and not self.closed and len(self._idle) < POOL_SIZE:
                connection.protocol.start_next_cycle()
                self._idle.append(connection)
                return
        connection.socket.close()

    def request(self, method: str, target: str, body: bytes | None, deadline_seconds: float | None) -> Answer:
        """Sends one request and reads its whole answer.

        A GET that fails on a reused connection before any answer arrives is sent
        once more on a new one; other requests are never sent twice.
        """
        deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
        while True:
            connection, reused = self._pooled_connection()
            reader = AnswerReader()
            try:
                answer = self._exchange(connection, reused, method, target, body, deadline, reader)
            except LitebackupError:
                self._return_connection(connection, False)
                raise
            except (OSError, h11.ProtocolError) as error:
                self._return_connection(connection, False)
                if self.closed:
                    raise LitebackupError(ErrorCode.CLOSED, "the client is closed") from error
                if isinstance(error, TimeoutError) and deadline is not None and time.monotonic() >= deadline:
                    raise LitebackupError(
                        ErrorCode.TIMEOUT, "the daemon did not answer within the call's timeout; it may still finish the operation"
                    ) from error
                if reused and method == "GET" and not reader.started:
                    continue
                raise LitebackupError(ErrorCode.CONNECTION_FAILED, str(error) or type(error).__name__) from error
            protocol = connection.protocol
            self._return_connection(connection, protocol.our_state is h11.DONE and protocol.their_state is h11.DONE)
            return answer

    def _exchange(
        self,
        connection: _Connection,
        reused: bool,
        method: str,
        target: str,
        body: bytes | None,
        deadline: float | None,
        reader: AnswerReader,
    ) -> Answer:
        raw = connection.socket
        if not reused:
            raw.settimeout(self._remaining(deadline))
            raw.connect(self.socket_path)
            if self.closed:
                raise LitebackupError(ErrorCode.CLOSED, "the client is closed")
        data = b"".join(connection.protocol.send(event) or b"" for event in request_events(method, target, body))
        raw.settimeout(self._remaining(deadline))
        raw.sendall(data)
        while True:
            event = connection.protocol.next_event()
            if event is h11.NEED_DATA:
                raw.settimeout(self._remaining(deadline))
                connection.protocol.receive_data(raw.recv(READ_SIZE))
                continue
            if reader.accept(event):
                return reader.answer()

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the call's deadline passed")
        return remaining

    def close(self) -> None:
        """Closes idle connections and ends calls in flight."""
        with self._lock:
            self.closed = True
            idle, self._idle = self._idle, []
            active = list(self._active)
        for connection in idle:
            connection.socket.close()
        for connection in active:
            with contextlib.suppress(OSError):
                connection.socket.shutdown(socket.SHUT_RDWR)
