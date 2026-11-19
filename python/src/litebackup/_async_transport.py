"""HTTP/1.1 over the daemon's unix socket with h11 and asyncio streams.

Cancelling the task that awaits a call closes its connection, so a cancelled
call never leaves a half-read answer in the pool.
"""

from __future__ import annotations

import asyncio
import contextlib

import h11

from ._transport import POOL_SIZE, READ_SIZE, Answer, AnswerReader, request_events
from .errors import ErrorCode, LitebackupError


class _AsyncConnection:
    __slots__ = ("protocol", "reader", "writer")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.protocol = h11.Connection(our_role=h11.CLIENT)

    def dropped(self) -> bool:
        return self.reader.at_eof() or self.writer.is_closing()

    def close(self) -> None:
        self.writer.close()


class AsyncTransport:
    """Sends requests over a unix socket from one event loop."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._idle: list[_AsyncConnection] = []
        self._active: set[_AsyncConnection] = set()
        self.closed = False

    async def _checkout(self) -> tuple[_AsyncConnection, bool]:
        if self.closed:
            raise LitebackupError(ErrorCode.CLOSED, "the client is closed")
        while self._idle:
            connection = self._idle.pop()
            if connection.dropped():
                connection.close()
                continue
            self._active.add(connection)
            return connection, True
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        connection = _AsyncConnection(reader, writer)
        self._active.add(connection)
        return connection, False

    def _checkin(self, connection: _AsyncConnection, reusable: bool) -> None:
        self._active.discard(connection)
        if reusable and not self.closed and len(self._idle) < POOL_SIZE:
            connection.protocol.start_next_cycle()
            self._idle.append(connection)
            return
        connection.close()

    async def request(self, method: str, target: str, body: bytes | None, deadline_seconds: float | None) -> Answer:
        """Sends one request and reads its whole answer; see SyncTransport.request."""
        loop = asyncio.get_running_loop()
        deadline = None if deadline_seconds is None else loop.time() + deadline_seconds
        while True:
            reader = AnswerReader()
            connection: _AsyncConnection | None = None
            reused = False
            try:
                async with asyncio.timeout_at(deadline):
                    connection, reused = await self._checkout()
                    answer = await self._exchange(connection, method, target, body, reader)
            except asyncio.CancelledError:
                if connection is not None:
                    self._checkin(connection, False)
                raise
            except LitebackupError:
                if connection is not None:
                    self._checkin(connection, False)
                raise
            except TimeoutError as error:
                if connection is not None:
                    self._checkin(connection, False)
                raise LitebackupError(
                    ErrorCode.TIMEOUT, "the daemon did not answer within the call's timeout; it may still finish the operation"
                ) from error
            except (OSError, h11.ProtocolError, asyncio.IncompleteReadError) as error:
                if connection is not None:
                    self._checkin(connection, False)
                if self.closed:
                    raise LitebackupError(ErrorCode.CLOSED, "the client is closed") from error
                if reused and method == "GET" and not reader.started:
                    continue
                raise LitebackupError(ErrorCode.CONNECTION_FAILED, str(error) or type(error).__name__) from error
            protocol = connection.protocol
            self._checkin(connection, protocol.our_state is h11.DONE and protocol.their_state is h11.DONE)
            return answer

    @staticmethod
    async def _exchange(connection: _AsyncConnection, method: str, target: str, body: bytes | None, reader: AnswerReader) -> Answer:
        data = b"".join(connection.protocol.send(event) or b"" for event in request_events(method, target, body))
        connection.writer.write(data)
        await connection.writer.drain()
        while True:
            event = connection.protocol.next_event()
            if event is h11.NEED_DATA:
                connection.protocol.receive_data(await connection.reader.read(READ_SIZE))
                continue
            if reader.accept(event):
                return reader.answer()

    async def close(self) -> None:
        """Closes every connection; calls in flight fail with closed."""
        self.closed = True
        connections = [*self._idle, *self._active]
        self._idle = []
        for connection in connections:
            connection.close()
        for connection in connections:
            with contextlib.suppress(Exception):
                await connection.writer.wait_closed()
