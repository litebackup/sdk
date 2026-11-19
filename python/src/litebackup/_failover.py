"""Failovers: one database on several identical machines, followed through the daemon's events."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from . import _wire
from ._generated import models
from .errors import ErrorCode, LitebackupError

if TYPE_CHECKING:
    from .client import AsyncLitebackup, Litebackup

Role = Literal["origin", "replica"]
EVENT_PAGE = 1000


class _FailoverState:
    """What a failover knows and calls back about, shared by the sync and async kinds."""

    def __init__(
        self,
        info: models.Failover,
        on_role_change: Callable[[str], None] | None,
        on_conflict_saved: Callable[[str], None] | None,
        on_error: Callable[[LitebackupError], None] | None,
    ) -> None:
        self.info = info
        self.name = info.name
        self.path = info.path
        self.role: str = info.role
        self._on_role_change = on_role_change
        self._on_conflict_saved = on_conflict_saved
        self._on_error = on_error
        self.stopped = False

    @property
    def is_origin(self) -> bool:
        """Whether this machine writes the database now."""
        return self.role == "origin"

    def handle(self, event: models.Event) -> None:
        if self.stopped or event.name != self.name:
            return
        if event.type == "role_changed":
            role = event.role or event.message
            if role and role != self.role:
                self.role = role
                if self._on_role_change is not None:
                    self._on_role_change(role)
        elif event.type == "conflict_saved" and self._on_conflict_saved is not None:
            self._on_conflict_saved(event.path or event.message)

    def report(self, error: LitebackupError) -> bool:
        """Reports a failed long-poll; returns False when following must end."""
        if self._on_error is not None and not self.stopped:
            self._on_error(error)
        return not _wire.is_terminal_watch_error(error)


class Failover(_FailoverState):
    """A failover followed by a background thread; stop() or the client's close() ends following.

    Each replica asks for the write lock about once a second, so one takes over once
    the origin's lease runs out. Callbacks run on the following thread, in event order.
    """

    def __init__(self, client: Litebackup, info: models.Failover, cursor: int, **callbacks: Callable[..., None] | None) -> None:
        super().__init__(info, **callbacks)
        self._client = client
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._follow, args=(cursor,), name=f"litebackup-failover-{self.name}", daemon=True)
        self._thread.start()

    def writer(self) -> models.Writer:
        """Which machine writes the database, when its lease runs out, and whether it ran out."""
        return self._client.databases.writer(self.name)

    def stop(self) -> None:
        """Stops the failover and releases its write lock. The file stays."""
        try:
            self._client.backups.stop(self.name)
        finally:
            self.end()

    def end(self) -> None:
        """Stops following events; callbacks are not called afterwards."""
        self.stopped = True
        self._wake.set()
        self._client._forget_failover(self)

    def _follow(self, cursor: int) -> None:
        delay = _wire.FIRST_RETRY_SECONDS
        while not self.stopped:
            try:
                page = self._client._call(_wire.events_page(after=cursor, wait=_wire.EVENT_WAIT_SECONDS, limit=EVENT_PAGE))
            except LitebackupError as error:
                if self.stopped or not self.report(error):
                    return
                self._wake.wait(delay)
                delay = min(delay * 2, _wire.MAXIMUM_RETRY_SECONDS)
                continue
            delay = _wire.FIRST_RETRY_SECONDS
            for event in page.events:
                cursor = max(cursor, event.id)
                self.handle(event)
            cursor = max(cursor, page.next)


class AsyncFailover(_FailoverState):
    """A failover followed by an asyncio task; stop() or the client's close() ends following."""

    def __init__(self, client: AsyncLitebackup, info: models.Failover, cursor: int, **callbacks: Callable[..., None] | None) -> None:
        super().__init__(info, **callbacks)
        self._client = client
        self._task = asyncio.get_running_loop().create_task(self._follow(cursor), name=f"litebackup-failover-{self.name}")

    async def writer(self) -> models.Writer:
        """Which machine writes the database, when its lease runs out, and whether it ran out."""
        return await self._client.databases.writer(self.name)

    async def stop(self) -> None:
        """Stops the failover and releases its write lock. The file stays."""
        try:
            await self._client.backups.stop(self.name)
        finally:
            await self.end()

    async def end(self) -> None:
        """Stops following events and waits for the following task to finish."""
        self.stopped = True
        self._client._forget_failover(self)
        if self._task is not asyncio.current_task():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise

    async def _follow(self, cursor: int) -> None:
        delay = _wire.FIRST_RETRY_SECONDS
        while not self.stopped:
            try:
                page = await self._client._call(_wire.events_page(after=cursor, wait=_wire.EVENT_WAIT_SECONDS, limit=EVENT_PAGE))
            except LitebackupError as error:
                if self.stopped or error.code == ErrorCode.CLOSED or not self.report(error):
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, _wire.MAXIMUM_RETRY_SECONDS)
                continue
            delay = _wire.FIRST_RETRY_SECONDS
            for event in page.events:
                cursor = max(cursor, event.id)
                self.handle(event)
            cursor = max(cursor, page.next)
