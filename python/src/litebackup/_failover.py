"""Failovers: one database on several identical machines, followed through the daemon's events."""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, TypeVar

from . import _wire
from ._generated import models
from .errors import ErrorCode, LitebackupError

if TYPE_CHECKING:
    from .client import AsyncLitebackup, Litebackup

Role = Literal["origin", "replica", "unknown"]
"""A failover's role on this machine; unknown while the daemon does not answer, once the failover stopped,
and from a draining event until the next role change or a role read past the drain.

The SDK sets unknown; the daemon never reports it.
"""
ROLE_UNKNOWN: Role = "unknown"
EVENT_PAGE = 1000
FAILOVER_EVENT_WAIT_SECONDS = 10.0
"""How long one failover long-poll waits.

It is shorter than ROLE_CONFIRMATION_LIMIT_SECONDS, so a daemon that answers confirms the role in time.
"""
ROLE_CONFIRMATION_LIMIT_SECONDS = 15.0
"""The longest a failover keeps reporting a role the daemon has not confirmed.

15 s is the default client heartbeat interval and a quarter of the default 60 s session lease another
machine waits out before it takes over. Tests shorten it.
"""


def _changes_role(event_type: str) -> bool:
    """Whether an event type sets a failover's role: role_changed, and draining and refused, which make it unknown."""
    return event_type in ("role_changed", "draining", "denied")


def _status_role(status: models.DatabaseStatus) -> str:
    """The role a status read reports: unknown while the daemon drains the origin, as after a draining event,
    and while the failover's file is refused (conflicted), as after a denied event."""
    return ROLE_UNKNOWN if status.draining or status.state == "conflicted" else status.role


# Error codes the event reading never retries: they end following. An expired cursor is not one: a failover resumes at the log's end.
FATAL_ERROR_CODES = (ErrorCode.SDK_UNSUPPORTED, ErrorCode.CLOSED, ErrorCode.CANCELLED, ErrorCode.INVALID_ARGUMENT)

Answer = TypeVar("Answer")


@dataclasses.dataclass(slots=True)
class _FollowProgress:
    """What a failover's follower carries from one read to the next."""

    cursor: int
    """The newest event ID handed to the failover."""
    delay: float = _wire.FIRST_RETRY_SECONDS
    """The backoff after the next failed read."""
    confirmed: float = dataclasses.field(default_factory=time.monotonic)
    """When the daemon last confirmed the role: the start, a long-poll, or a role read answered."""
    role_read_pending: bool = False
    """Set when the role is read before the next long-poll: after an expired cursor, and after the role became unknown."""
    log_end_pending: bool = False
    """Set when the log's end is read before that role read."""
    role_read_log_end: int = 0
    """The log's end read before the last role read: role_changed events up to it are older than that read and are skipped."""


class _FailoverState:
    """What a failover knows and calls back about, shared by the sync and async kinds."""

    def __init__(
        self,
        status: models.Failover,
        on_role_change: Callable[[str], None] | None,
        on_denied: Callable[[str], None] | None,
        on_error: Callable[[LitebackupError], None] | None,
    ) -> None:
        # What the daemon answered when the failover started.
        self.status = status
        self.name = status.name
        self.path = status.path
        self.role: str = status.role
        self._event_wait = FAILOVER_EVENT_WAIT_SECONDS
        self._confirmation_limit = ROLE_CONFIRMATION_LIMIT_SECONDS
        self._on_role_change = on_role_change
        self._on_denied = on_denied
        self._on_error = on_error
        self.stopped = False
        # Held across every callback, so once _mark_stopped returns no callback runs; reentrant, so a callback may stop the failover.
        self._callback_lock = threading.RLock()

    @property
    def is_origin(self) -> bool:
        """Whether this machine writes the database now; False while the role is unknown."""
        return self.role == "origin"

    def _mark_stopped(self) -> None:
        """Stops callbacks, waiting for one in progress on another thread to return.

        Nothing follows the role any more, so it is unknown.
        """
        with self._callback_lock:
            self.stopped = True
            self.role = ROLE_UNKNOWN

    def _report_start_role(self) -> None:
        """Calls on_role_change with the role the start answered, so an app can route writes from its callback alone."""
        with self._callback_lock:
            if not self.stopped and self._on_role_change is not None:
                self._on_role_change(self.role)

    def _apply_event(self, event: models.Event, progress: _FollowProgress) -> None:
        with self._callback_lock:
            if self.stopped or event.name != self.name:
                return
            # An event older than the last role read changes no role, because that read holds it;
            # a refusal among them is still reported.
            older_than_role_read = _changes_role(event.type) and event.id <= progress.role_read_log_end
            if older_than_role_read and event.type != "denied":
                return
            if event.type == "role_changed":
                self._change_role(event.role or event.message)
            elif event.type == "draining":
                # The daemon drains: it stops being the origin before it releases
                # the lock, so the app stops writing while no machine may take over yet.
                self._change_role(ROLE_UNKNOWN)
            elif event.type == "denied":
                # The daemon refused this machine's file and the failover stays conflicted there:
                # this machine neither writes nor follows the database.
                if not older_than_role_read:
                    self._change_role(ROLE_UNKNOWN)
                # The role callback may have stopped the failover.
                if not self.stopped and self._on_denied is not None:
                    self._on_denied(event.message)

    def _read_unknown_role_again(self, page: models.EventList, progress: _FollowProgress) -> None:
        """Asks for a role read after a long-poll that brought no role change while the role is unknown.

        A drain that failed says nothing more after its draining event; its status shows the role once it stopped.
        """
        if self.role != ROLE_UNKNOWN:
            return
        if any(event.name == self.name and _changes_role(event.type) and event.id > progress.role_read_log_end for event in page.events):
            return
        progress.role_read_pending = True
        progress.log_end_pending = True

    def _change_role(self, role: str) -> bool:
        """Saves a role and calls on_role_change when it differs; returns whether it changed."""
        with self._callback_lock:
            if self.stopped or not role or role == self.role:
                return False
            self.role = role
            if self._on_role_change is not None:
                self._on_role_change(role)
            return True

    def _report_error(self, error: LitebackupError) -> None:
        """Reports a failed read."""
        with self._callback_lock:
            if self._on_error is not None and not self.stopped:
                self._on_error(error)

    def _scan_role_change_page(self, page: models.EventList, end: int, progress: _FollowProgress) -> bool:
        """Moves role_read_log_end through a page read after it, up to end, and returns whether the page changed this database's role."""
        superseded = False
        for event in page.events:
            if event.id > end:
                break
            if event.name == self.name and _changes_role(event.type):
                superseded = True
        if page.events:
            # Never past end: a role change after it came after the role read, and the long-poll applies it.
            progress.role_read_log_end = max(progress.role_read_log_end, min(page.events[-1].id, end))
        return superseded

    def _clear_role(self) -> None:
        """Makes the role unknown without calling on_role_change."""
        with self._callback_lock:
            self.role = ROLE_UNKNOWN

    def _lose_role(self, progress: _FollowProgress) -> None:
        """Makes the role unknown and, when that changed it, has the follower read the log's end and the role once the daemon answers."""
        if self._change_role(ROLE_UNKNOWN):
            progress.role_read_pending = True
            progress.log_end_pending = True

    def _lose_unconfirmed_role(self, progress: _FollowProgress) -> None:
        """Makes the role unknown once the daemon has not confirmed it for the confirmation limit."""
        if self.role != ROLE_UNKNOWN and time.monotonic() - progress.confirmed >= self._confirmation_limit:
            self._lose_role(progress)

    def _with_answer_deadline(self, call: _wire.Call[Answer], progress: _FollowProgress) -> _wire.Call[Answer]:
        """The call with a deadline of the confirmation limit after the last confirmation, or after now once the role is unknown."""
        now = time.monotonic()
        start = now if self.role == ROLE_UNKNOWN else progress.confirmed
        return dataclasses.replace(call, answer_deadline_seconds=max(0.0, start + self._confirmation_limit - now))

    def _retry_delay(self, error: LitebackupError, progress: _FollowProgress) -> float | None:
        """After a failed read: makes the role unknown when the daemon did not answer or following ends, reports the error,
        and returns how long to wait before the next read, or None when following ends.

        While the role is known, the wait ends when the confirmation limit passes.
        """
        if self.stopped:
            return None
        ending = error.code in FATAL_ERROR_CODES
        if error.code == ErrorCode.CLOSED:
            # The client is closing and stops following next: no role callback.
            self._clear_role()
        elif ending or error.status == 0:
            self._lose_role(progress)
        self._report_error(error)
        if ending or self.stopped:
            return None
        wait = progress.delay
        if self.role != ROLE_UNKNOWN:
            wait = max(0.0, min(wait, progress.confirmed + self._confirmation_limit - time.monotonic()))
        progress.delay = min(progress.delay * 2, _wire.MAXIMUM_RETRY_SECONDS)
        return wait


class Failover(_FailoverState):
    """A failover followed by a background thread; stop() or the client's close() ends following.

    Each replica asks for the write lock about once a second, so one takes over once
    the origin's lease runs out. Callbacks run on the following thread, in event order;
    the first is on_role_change with the role the start answered, and it may run before
    start() returns, so use its role argument rather than the Failover.
    """

    def __init__(self, client: Litebackup, status: models.Failover, cursor: int, **callbacks: Callable[..., None] | None) -> None:
        super().__init__(status, **callbacks)
        self._client = client
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._follow, args=(cursor,), name=f"litebackup-failover-{self.name}", daemon=True)
        self._thread.start()

    def writer(self) -> models.Writer:
        """Which machine writes the database, when its lease runs out, and whether it ran out."""
        return self._client.databases.writer(self.name)

    def stop(self) -> None:
        """Stops the failover and releases its write lock. The file stays.

        Following ends first, so no callback runs once the failover is stopped.
        """
        self._stop_following()
        self._client.backups.stop(self.name)

    def _stop_following(self) -> None:
        """Stops following events; once it returns, no callback runs.

        A callback in progress on the following thread is waited for. The thread
        itself exits when its current long-poll returns; the client's close() waits for it.
        """
        self._mark_stopped()
        self._wake.set()
        self._client._remove_failover(self)

    def _join(self) -> None:
        if self._thread is not threading.current_thread():
            self._thread.join()

    def _follow(self, cursor: int) -> None:
        """Long-polls the daemon's events after cursor until stopped, handing this failover's events to it.

        It first reports the role the start answered.

        An expired cursor does not end following: it moves to the log's end, then the role
        is read again before the next long-poll, so a role change in events the daemon
        pruned is still reported. The cursor is taken first and kept while the role read
        is retried, so no event after it is lost.

        A read the daemon did not answer (connection_failed, timeout, daemon_unsupported:
        any error with status 0) makes the role unknown at once, and so does no answer
        within the confirmation limit of the last confirmation, or failed reads the daemon
        answered for that long. Once the daemon answers again, the follower reads the log's
        end, then the role, then the log's end again and the events up to it: when one changed
        the role, the role read may predate it, so it reads the role again. It
        keeps its cursor: refusals meanwhile are still reported, and role changes
        up to the end it looked at are skipped because the role read holds them. A closed
        client makes the role unknown without a callback.
        """
        self._report_start_role()
        progress = _FollowProgress(cursor)
        while not self.stopped:
            try:
                self._read_daemon(progress)
            except LitebackupError as error:
                wait = self._retry_delay(error, progress)
                if wait is None:
                    return
                self._wake.wait(wait)
                continue
            progress.delay = _wire.FIRST_RETRY_SECONDS

    def _read_daemon(self, progress: _FollowProgress) -> None:
        """Makes the follower's next read: the log's end or the role when a role read is pending, otherwise one long-poll."""
        self._lose_unconfirmed_role(progress)
        call = self._client._call
        if progress.role_read_pending:
            if progress.log_end_pending:
                progress.role_read_log_end = call(self._with_answer_deadline(_wire.events_page(after=-1), progress)).next
                progress.log_end_pending = False
            role = _status_role(call(self._with_answer_deadline(_wire.database(self.name), progress)))
            if self._role_superseded(progress):
                # The role read may predate a role change the log now holds: read the role again, then look past the new log end.
                return
            progress.confirmed = time.monotonic()
            progress.role_read_pending = False
            self._change_role(role)
            return
        long_poll = _wire.events_page(after=progress.cursor, wait=self._event_wait, limit=EVENT_PAGE)
        try:
            page = call(self._with_answer_deadline(long_poll, progress))
        except LitebackupError as error:
            if self.stopped or error.code != ErrorCode.CURSOR_EXPIRED:
                raise
            self._report_error(error)
            if self.stopped:
                return
            end = call(self._with_answer_deadline(_wire.events_page(after=-1), progress)).next
            progress.cursor = end
            progress.role_read_log_end = end
            progress.role_read_pending = True
            progress.log_end_pending = False
            return
        progress.confirmed = time.monotonic()
        for event in page.events:
            progress.cursor = max(progress.cursor, event.id)
            self._apply_event(event, progress)
        progress.cursor = max(progress.cursor, page.next)
        self._read_unknown_role_again(page, progress)

    def _role_superseded(self, progress: _FollowProgress) -> bool:
        """Reads the log's end as of now, then the events from role_read_log_end through it, without waiting.

        Returns whether one changed this database's role, so a role read that raced it is not trusted.
        role_read_log_end moves to that end; events after it came after the role read. When pruning
        passed role_read_log_end, it returns True with log_end_pending set, so the follower starts
        again from a fresh log end.
        """
        call = self._client._call
        end = call(self._with_answer_deadline(_wire.events_page(after=-1), progress)).next
        superseded = False
        while progress.role_read_log_end < end:
            scan = _wire.events_page(after=progress.role_read_log_end, limit=EVENT_PAGE)
            try:
                page = call(self._with_answer_deadline(scan, progress))
            except LitebackupError as error:
                if error.code != ErrorCode.CURSOR_EXPIRED:
                    raise
                progress.log_end_pending = True
                return True
            superseded = self._scan_role_change_page(page, end, progress) or superseded
            if not page.events:
                break
        progress.role_read_log_end = max(progress.role_read_log_end, end)
        return superseded


class AsyncFailover(_FailoverState):
    """A failover followed by an asyncio task; stop() or the client's close() ends following."""

    def __init__(self, client: AsyncLitebackup, status: models.Failover, cursor: int, **callbacks: Callable[..., None] | None) -> None:
        super().__init__(status, **callbacks)
        self._client = client
        self._task = asyncio.get_running_loop().create_task(self._follow(cursor), name=f"litebackup-failover-{self.name}")

    async def writer(self) -> models.Writer:
        """Which machine writes the database, when its lease runs out, and whether it ran out."""
        return await self._client.databases.writer(self.name)

    async def stop(self) -> None:
        """Stops the failover and releases its write lock. The file stays.

        Following ends first, so no callback runs once the failover is stopped.
        """
        await self._stop_following()
        await self._client.backups.stop(self.name)

    async def _stop_following(self) -> None:
        """Stops following events and waits for the following task to finish; once it returns, no callback runs."""
        self._mark_stopped()
        self._client._remove_failover(self)
        if self._task is not asyncio.current_task():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise

    async def _follow(self, cursor: int) -> None:
        """See Failover._follow."""
        self._report_start_role()
        progress = _FollowProgress(cursor)
        while not self.stopped:
            try:
                await self._read_daemon(progress)
            except LitebackupError as error:
                if error.code == ErrorCode.CLOSED:
                    self._clear_role()
                    return
                wait = self._retry_delay(error, progress)
                if wait is None:
                    return
                await asyncio.sleep(wait)
                continue
            progress.delay = _wire.FIRST_RETRY_SECONDS

    async def _read_daemon(self, progress: _FollowProgress) -> None:
        """See Failover._read_daemon."""
        self._lose_unconfirmed_role(progress)
        call = self._client._call
        if progress.role_read_pending:
            if progress.log_end_pending:
                progress.role_read_log_end = (await call(self._with_answer_deadline(_wire.events_page(after=-1), progress))).next
                progress.log_end_pending = False
            role = _status_role(await call(self._with_answer_deadline(_wire.database(self.name), progress)))
            if await self._role_superseded(progress):
                return
            progress.confirmed = time.monotonic()
            progress.role_read_pending = False
            self._change_role(role)
            return
        long_poll = _wire.events_page(after=progress.cursor, wait=self._event_wait, limit=EVENT_PAGE)
        try:
            page = await call(self._with_answer_deadline(long_poll, progress))
        except LitebackupError as error:
            if self.stopped or error.code != ErrorCode.CURSOR_EXPIRED:
                raise
            self._report_error(error)
            if self.stopped:
                return
            end = (await call(self._with_answer_deadline(_wire.events_page(after=-1), progress))).next
            progress.cursor = end
            progress.role_read_log_end = end
            progress.role_read_pending = True
            progress.log_end_pending = False
            return
        progress.confirmed = time.monotonic()
        for event in page.events:
            progress.cursor = max(progress.cursor, event.id)
            self._apply_event(event, progress)
        progress.cursor = max(progress.cursor, page.next)
        self._read_unknown_role_again(page, progress)

    async def _role_superseded(self, progress: _FollowProgress) -> bool:
        """See Failover._role_superseded."""
        call = self._client._call
        end = (await call(self._with_answer_deadline(_wire.events_page(after=-1), progress))).next
        superseded = False
        while progress.role_read_log_end < end:
            scan = _wire.events_page(after=progress.role_read_log_end, limit=EVENT_PAGE)
            try:
                page = await call(self._with_answer_deadline(scan, progress))
            except LitebackupError as error:
                if error.code != ErrorCode.CURSOR_EXPIRED:
                    raise
                progress.log_end_pending = True
                return True
            superseded = self._scan_role_change_page(page, end, progress) or superseded
            if not page.events:
                break
        progress.role_read_log_end = max(progress.role_read_log_end, end)
        return superseded
