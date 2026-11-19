"""Litebackup and AsyncLitebackup: the daemon of one session, over its unix socket."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from datetime import datetime
from types import TracebackType
from typing import Any, Literal, TypeVar

from . import _foreground, _managed, _wire
from ._async_transport import AsyncTransport
from ._binary import TOKEN_ENVIRONMENT, litebackup_binary
from ._failover import AsyncFailover, Failover
from ._generated import models
from ._launch import launch_daemon, launch_daemon_async, process_environment
from ._paths import session_socket
from ._transport import Answer, SyncTransport
from ._wire import Call, Durability, Duration, PathSource, WriteProtection
from .errors import ErrorCode, LitebackupError, invalid_argument

Result = TypeVar("Result")
Connection = TypeVar("Connection")


class _Settings:
    """What a client was created with, resolved once."""

    def __init__(
        self,
        *,
        token: str | None,
        session: str | None,
        socket_path: str | None,
        binary_path: str | None,
        launch: bool,
        environment: Mapping[str, str] | None,
    ) -> None:
        self.environment = process_environment(environment)
        self.token = token if token is not None else self.environment.get(TOKEN_ENVIRONMENT)
        stripped = (self.token or "").strip()
        if stripped.startswith("litebackup_") and not stripped.startswith("litebackup_join_"):
            raise invalid_argument("Get a join token from Machines → Add machine")
        if socket_path is not None and not socket_path:
            raise invalid_argument("socket_path needs a path")
        self.explicit_socket = socket_path is not None
        self.binary_path = binary_path
        self.launch = launch
        if socket_path is not None:
            self.session: str | None = session
            self.socket_path = socket_path
        else:
            self.session, self.socket_path = session_socket(session, self.environment)

    def foreground_command(self, source: PathSource, **options: Any) -> builtins.list[str]:
        if self.explicit_socket:
            raise invalid_argument("foreground backup needs a named session and cannot target socket_path")
        binary = litebackup_binary(self.environment, self.binary_path)
        return _foreground.backup_command(binary, source, session=self.session, **options)

    def foreground_environment(self) -> dict[str, str]:
        environment = dict(self.environment)
        if self.token:
            environment[TOKEN_ENVIRONMENT] = self.token
        return environment


# The synchronous client.


class Litebackup:
    """Talks to the Litebackup daemon of one session, starting it like the CLI does.

    Calls connect on first use. Use it as a context manager, or call close(), which
    never shuts down the daemon::

        with Litebackup() as litebackup:
            litebackup.backups.start("/var/lib/app/main.sqlite")
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        session: str | None = None,
        socket_path: str | None = None,
        binary_path: str | None = None,
        launch: bool = True,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """Arguments:

        token: the join token, TOKEN@HOST, the daemon joins with on its first start; LITEBACKUP_TOKEN by default.
        session: the session name; LITEBACKUP_SESSION, then "default".
        socket_path: connect to this socket and never start a daemon.
        binary_path: the litebackup binary that starts the daemon; see litebackup_binary() for the search.
        launch: start the session daemon when it is not running.
        environment: the environment for resolving paths and starting the daemon; os.environ by default.
        """
        self._settings = _Settings(
            token=token, session=session, socket_path=socket_path, binary_path=binary_path, launch=launch, environment=environment
        )
        self._transport = SyncTransport(self._settings.socket_path)
        self._connect_lock = threading.Lock()
        self._connected = False
        self._closed = False
        self._failovers: set[Failover] = set()
        self.client_version = ""
        """The daemon's version, from its last answer."""
        self.backups = Backups(self)
        self.rules = Rules(self)
        self.databases = Databases(self)
        self.replicas = Replicas(self)
        self.conflicts = Conflicts(self)
        self.events = Events(self)
        self.session = Session(self)
        self.failovers = Failovers(self)

    @property
    def socket_path(self) -> str:
        return self._settings.socket_path

    def connect(self) -> None:
        """Connects, starting the daemon when needed; fails with incompatible_version for a daemon older than 1.0.0."""
        if self._closed:
            raise LitebackupError(ErrorCode.CLOSED, "the client is closed")
        with self._connect_lock:
            if self._connected:
                return
            settings = self._settings
            if not settings.explicit_socket and settings.launch and settings.session:
                launch_daemon(settings.session, settings.socket_path, settings.environment, settings.token, settings.binary_path)
            self._send(_wire.connect_probe())
            _wire.check_daemon_version(self.client_version, settings.session)
            self._connected = True

    def _send(self, call: Call[Result]) -> Result:
        answer: Answer = self._transport.request(call.method, call.target, call.encoded_body(), call.deadline_seconds)
        self.client_version = answer.client_version
        return _wire.decode_answer(call, answer.status, answer.body)

    def _call(self, call: Call[Result]) -> Result:
        self.connect()
        return self._send(call)

    def _forget_failover(self, failover: Failover) -> None:
        self._failovers.discard(failover)

    def close(self) -> None:
        """Stops following failovers and closes connections. The daemon keeps running, and so do its failovers and acquired databases."""
        self._closed = True
        for failover in list(self._failovers):
            failover.end()
        self._transport.close()

    def __enter__(self) -> Litebackup:
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()


class _Group:
    def __init__(self, client: Litebackup) -> None:
        self._client = client


class Backups(_Group):
    """Continuous backups and one-shot backup jobs."""

    def start(
        self, source: PathSource, *, name: str | None = None, prefix: str | None = None, maximum_local_databases: int | None = None
    ) -> models.BackupRule:
        """Saves a continuous backup rule for a file, directory, or glob; returns once the daemon saved it.

        Starting the same path again with the same settings returns the saved rule, so
        call it on every start. Other settings for a saved path raise ``exists`` with the
        saved rule in ``details["rule"]``.
        """
        return self._client._call(_wire.start_backup(source, name=name, prefix=prefix, maximum_local_databases=maximum_local_databases))

    def once(
        self,
        source: PathSource,
        *,
        name: str | None = None,
        prefix: str | None = None,
        wait: Durability = "server",
        timeout: Duration | None = None,
    ) -> None:
        """Runs one foreground backup job and returns once it exits, after the chosen destination acknowledged it."""
        settings = self._client._settings
        command = settings.foreground_command(source, name=name, prefix=prefix, wait=wait, timeout=timeout)
        try:
            completed = subprocess.run(command, env=settings.foreground_environment(), capture_output=True, text=True, check=False)
        except OSError as error:
            raise LitebackupError(ErrorCode.LAUNCH_FAILED, str(error)) from error
        if completed.returncode != 0:
            raise _foreground.backup_failure(completed.stderr, completed.returncode)

    def stop(self, name: str) -> models.StopBackupResult:
        """Stops the single-file backup with this exact remote name on this machine."""
        return self._client._call(_wire.stop_backup(name))


class Rules(_Group):
    """Saved backup and failover rules."""

    def list(self) -> builtins.list[models.BackupRule]:
        return self._client._call(_wire.list_rules())

    def stop(self, rule_id: str) -> models.StopBackupResult:
        """Stops a whole rule, including future matches."""
        return self._client._call(_wire.stop_rule(rule_id))


class Databases(_Group):
    """Databases on this machine and on the server."""

    def get(self, name: str) -> models.DatabaseStatus:
        return self._client._call(_wire.database(name))

    def page(self, *, after: str = "", limit: int = 100) -> models.DatabaseStatusList:
        """One page; pass its ``next`` as ``after`` to continue."""
        return self._client._call(_wire.databases_page(after=after, limit=limit))

    def iter_all(self, *, limit: int = 100) -> Iterator[models.DatabaseStatus]:
        """Every database on this machine, a page at a time."""
        after = ""
        while True:
            page = self.page(after=after, limit=limit)
            yield from page.databases
            if not page.next:
                return
            after = page.next

    def list(self) -> builtins.list[models.DatabaseStatus]:
        return list(self.iter_all())

    def versions_page(self, name: str, *, after: int = 0, limit: int | None = None) -> models.VersionList:
        return self._client._call(_wire.versions_page(name, after=after, limit=limit))

    def iter_versions(self, name: str, *, after: int = 0, limit: int | None = None) -> Iterator[models.RestorableVersion]:
        """The versions the server can restore, oldest first; at most ``limit`` when set."""
        if limit == 0:
            return
        yielded = 0
        while True:
            page = self.versions_page(name, after=after, limit=None if limit is None else limit - yielded)
            for version in page.versions:
                yield version
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if not page.next or page.next <= after or not page.versions:
                return
            after = page.next

    def versions(self, name: str, *, after: int = 0, limit: int | None = None) -> builtins.list[models.RestorableVersion]:
        return list(self.iter_versions(name, after=after, limit=limit))

    def restore(self, name: str, *, to: PathSource, version: int | None = None, time: datetime | str | None = None) -> models.RestoreResult:
        """Writes a version to a new file and checks it. An existing file is never overwritten (exists)."""
        return self._client._call(_wire.restore(name, to=to, version=version, time=time))

    def sync(self, name: str) -> models.DatabaseStatus:
        return self._client._call(_wire.sync(name))

    def offload(self, name: str) -> models.DatabaseStatus:
        return self._client._call(_wire.offload(name))

    def writer(self, name: str) -> models.Writer:
        return self._client._call(_wire.writer(name))

    def acquire(
        self,
        name: str,
        *,
        to: PathSource | None = None,
        maintenance: bool = False,
        offload: Literal["never", "released"] = "never",
        machine_label: str | None = None,
        on_lost: Literal["conflict", "block"] = "conflict",
        timeout: Duration | None = None,
    ) -> models.AcquireResult:
        """Takes the write lock and makes the local file the server's head. ``managed()`` also opens, closes, and releases."""
        return self._client._call(
            _wire.acquire(
                name, to=to, maintenance=maintenance, offload=offload, machine_label=machine_label, on_lost=on_lost, timeout=timeout
            )
        )

    def release(
        self, name: str, *, wait: Durability | None = None, offload: bool = False, timeout: Duration | None = None
    ) -> models.ReleaseResult:
        """Syncs a final time and releases the write lock. Close every connection first, or it fails with still_open."""
        return self._client._call(_wire.release(name, wait=wait, offload=offload, timeout=timeout))

    def managed(
        self,
        name: str,
        *,
        durability: Durability,
        adapter: _managed.ScopeAdapter[Connection],
        to: PathSource | None = None,
        offload: bool = False,
        cleanup_timeout: Duration | None = None,
        machine_label: str | None = None,
        maintenance: bool = False,
        on_lost: Literal["conflict", "block"] = "conflict",
        acquire_timeout: Duration | None = None,
    ) -> AbstractContextManager[Connection]:
        """Acquires, opens, yields the connection, then closes and releases with ``durability``::

            with litebackup.databases.managed("agents/42.sqlite", to=path, durability="cloud", adapter=sqlite3_adapter()) as db:
                db.execute("INSERT INTO steps (body) VALUES (?)", (step,))
                db.commit()

        The adapter rolls back what the block did not commit. A failed block still
        closes and releases, without offload. A failed close does not release: the
        database stays acquired with its file untouched. Several failures raise an
        ExceptionGroup, the block's error first.
        """
        return _managed.managed(
            self._client,
            name,
            durability=durability,
            adapter=adapter,
            to=to,
            offload=offload,
            cleanup_timeout=cleanup_timeout,
            machine_label=machine_label,
            maintenance=maintenance,
            on_lost=on_lost,
            acquire_timeout=acquire_timeout,
        )


class Replicas(_Group):
    """Live readonly replicas on this machine."""

    def start(
        self, name: str, *, to: PathSource | None = None, write_protection: WriteProtection | None = None, timeout: Duration | None = None
    ) -> models.DatabaseStatus:
        """Keeps a live readonly replica and returns once its first version is local."""
        return self._client._call(_wire.start_replica(name, to=to, write_protection=write_protection, timeout=timeout))

    def start_prefix(self, prefix: str, *, to: PathSource, write_protection: WriteProtection | None = None) -> models.PrefixReplica:
        """Replicates current and future databases under a prefix into a directory; downloads run in the background."""
        return self._client._call(_wire.start_prefix_replica(prefix, to=to, write_protection=write_protection))

    def list(self) -> models.ReplicaList:
        return self._client._call(_wire.list_replicas())

    def stop(self, name: str) -> None:
        self._client._call(_wire.stop_replica(name))

    def stop_prefix(self, prefix: str, *, to: PathSource) -> None:
        self._client._call(_wire.stop_prefix_replica(prefix, to=to))


class Conflicts(_Group):
    """Conflict copies, saved because they diverged from the server."""

    def list(self) -> builtins.list[models.Conflict]:
        return self._client._call(_wire.list_conflicts())

    def delete(self, conflict_id: str) -> None:
        self._client._call(_wire.delete_conflict(conflict_id))


class Events(_Group):
    """The daemon's event log."""

    def page(self, *, after: int = 0, wait: Duration | None = None, limit: int | None = None) -> models.EventList:
        """One page after an event ID; -1 returns none and, as next, where the log ends."""
        return self._client._call(_wire.events_page(after=after, wait=wait, limit=limit))

    def watch(
        self, *, after: int = 0, wait: Duration | None = None, on_error: Callable[[LitebackupError], None] | None = None
    ) -> Iterator[models.Event]:
        """Yields events, oldest first, by long-polling until the caller stops.

        A failed long-poll is retried with backoff from the same cursor; sdk_unsupported
        and closed end the watch by raising.
        """
        wait_seconds = _wire.seconds(wait, "the event wait") or _wire.EVENT_WAIT_SECONDS
        delay = _wire.FIRST_RETRY_SECONDS
        while True:
            try:
                page = self.page(after=after, wait=wait_seconds)
            except LitebackupError as error:
                if _wire.is_terminal_watch_error(error):
                    raise
                if on_error is not None:
                    on_error(error)
                time.sleep(delay)
                delay = min(delay * 2, _wire.MAXIMUM_RETRY_SECONDS)
                continue
            delay = _wire.FIRST_RETRY_SECONDS
            for event in page.events:
                after = max(after, event.id)
                yield event
            after = max(after, page.next)


class Session(_Group):
    """The session and its daemon."""

    def status(self, *, after: str = "", limit: int = 100) -> models.Status:
        return self._client._call(_wire.status(after=after, limit=limit))

    def shutdown(self, *, wait: Literal["none", "server", "cloud"] | None = None, timeout: Duration | None = None) -> models.ShutdownResult:
        """Final-syncs every database, releases every write lock, and stops the daemon. close() never does this."""
        return self._client._call(_wire.shutdown(wait=wait, timeout=timeout))


class Failovers(_Group):
    """Databases run on several identical machines."""

    def start(
        self,
        path: PathSource,
        *,
        name: str | None = None,
        timeout: Duration | None = None,
        on_role_change: Callable[[str], None] | None = None,
        on_conflict_saved: Callable[[str], None] | None = None,
        on_error: Callable[[LitebackupError], None] | None = None,
    ) -> Failover:
        """Runs the database at ``path``, the same on every machine; returns once this machine is the origin or holds a replica."""
        client = self._client
        call = _wire.start_failover(path, name=name, timeout=timeout)
        end = client._call(_wire.events_page(after=-1))
        info = client._call(call)
        if client._closed:
            raise LitebackupError(ErrorCode.CLOSED, "the client is closed")
        failover = Failover(client, info, end.next, on_role_change=on_role_change, on_conflict_saved=on_conflict_saved, on_error=on_error)
        client._failovers.add(failover)
        return failover


# The asynchronous client.


class AsyncLitebackup:
    """The asyncio client: the same groups as Litebackup, awaited.

    Cancelling a task stops its local wait; the daemon may still finish the operation.
    asyncio.CancelledError propagates unchanged::

        async with AsyncLitebackup() as litebackup:
            await litebackup.backups.start("/var/lib/app/main.sqlite")
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        session: str | None = None,
        socket_path: str | None = None,
        binary_path: str | None = None,
        launch: bool = True,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """Arguments are those of Litebackup."""
        self._settings = _Settings(
            token=token, session=session, socket_path=socket_path, binary_path=binary_path, launch=launch, environment=environment
        )
        self._transport = AsyncTransport(self._settings.socket_path)
        self._connect_lock: asyncio.Lock | None = None
        self._connected = False
        self._closed = False
        self._failovers: set[AsyncFailover] = set()
        self.client_version = ""
        """The daemon's version, from its last answer."""
        self.backups = AsyncBackups(self)
        self.rules = AsyncRules(self)
        self.databases = AsyncDatabases(self)
        self.replicas = AsyncReplicas(self)
        self.conflicts = AsyncConflicts(self)
        self.events = AsyncEvents(self)
        self.session = AsyncSession(self)
        self.failovers = AsyncFailovers(self)

    @property
    def socket_path(self) -> str:
        return self._settings.socket_path

    async def connect(self) -> None:
        """Connects, starting the daemon when needed; fails with incompatible_version for a daemon older than 1.0.0."""
        if self._closed:
            raise LitebackupError(ErrorCode.CLOSED, "the client is closed")
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._connected:
                return
            settings = self._settings
            if not settings.explicit_socket and settings.launch and settings.session:
                await launch_daemon_async(
                    settings.session, settings.socket_path, settings.environment, settings.token, settings.binary_path
                )
            await self._send(_wire.connect_probe())
            _wire.check_daemon_version(self.client_version, settings.session)
            self._connected = True

    async def _send(self, call: Call[Result]) -> Result:
        answer = await self._transport.request(call.method, call.target, call.encoded_body(), call.deadline_seconds)
        self.client_version = answer.client_version
        return _wire.decode_answer(call, answer.status, answer.body)

    async def _call(self, call: Call[Result]) -> Result:
        await self.connect()
        return await self._send(call)

    def _forget_failover(self, failover: AsyncFailover) -> None:
        self._failovers.discard(failover)

    async def close(self) -> None:
        """Stops following failovers and closes connections. The daemon keeps running."""
        self._closed = True
        for failover in list(self._failovers):
            await failover.end()
        await self._transport.close()

    async def __aenter__(self) -> AsyncLitebackup:
        return self

    async def __aexit__(self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None) -> None:
        await self.close()


class _AsyncGroup:
    def __init__(self, client: AsyncLitebackup) -> None:
        self._client = client


class AsyncBackups(_AsyncGroup):
    """Continuous backups and one-shot backup jobs."""

    async def start(
        self, source: PathSource, *, name: str | None = None, prefix: str | None = None, maximum_local_databases: int | None = None
    ) -> models.BackupRule:
        """See Backups.start: idempotent for the same settings, ``exists`` with the saved rule for others."""
        return await self._client._call(
            _wire.start_backup(source, name=name, prefix=prefix, maximum_local_databases=maximum_local_databases)
        )

    async def once(
        self,
        source: PathSource,
        *,
        name: str | None = None,
        prefix: str | None = None,
        wait: Durability = "server",
        timeout: Duration | None = None,
    ) -> None:
        """Runs one foreground backup job; cancelling the task stops the job with SIGTERM."""
        settings = self._client._settings
        command = settings.foreground_command(source, name=name, prefix=prefix, wait=wait, timeout=timeout)
        try:
            process = await asyncio.create_subprocess_exec(
                *command, env=settings.foreground_environment(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        except OSError as error:
            raise LitebackupError(ErrorCode.LAUNCH_FAILED, str(error)) from error
        try:
            _, stderr = await process.communicate()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await asyncio.shield(process.wait())
            raise
        if process.returncode != 0:
            raise _foreground.backup_failure(stderr.decode("utf-8", "replace"), process.returncode or 1)

    async def stop(self, name: str) -> models.StopBackupResult:
        return await self._client._call(_wire.stop_backup(name))


class AsyncRules(_AsyncGroup):
    """Saved backup and failover rules."""

    async def list(self) -> builtins.list[models.BackupRule]:
        return await self._client._call(_wire.list_rules())

    async def stop(self, rule_id: str) -> models.StopBackupResult:
        return await self._client._call(_wire.stop_rule(rule_id))


class AsyncDatabases(_AsyncGroup):
    """Databases on this machine and on the server."""

    async def get(self, name: str) -> models.DatabaseStatus:
        return await self._client._call(_wire.database(name))

    async def page(self, *, after: str = "", limit: int = 100) -> models.DatabaseStatusList:
        return await self._client._call(_wire.databases_page(after=after, limit=limit))

    async def iter_all(self, *, limit: int = 100) -> AsyncIterator[models.DatabaseStatus]:
        """Every database on this machine, a page at a time: ``async for database in litebackup.databases.iter_all()``."""
        after = ""
        while True:
            page = await self.page(after=after, limit=limit)
            for database in page.databases:
                yield database
            if not page.next:
                return
            after = page.next

    async def list(self) -> builtins.list[models.DatabaseStatus]:
        return [database async for database in self.iter_all()]

    async def versions_page(self, name: str, *, after: int = 0, limit: int | None = None) -> models.VersionList:
        return await self._client._call(_wire.versions_page(name, after=after, limit=limit))

    async def iter_versions(self, name: str, *, after: int = 0, limit: int | None = None) -> AsyncIterator[models.RestorableVersion]:
        if limit == 0:
            return
        yielded = 0
        while True:
            page = await self.versions_page(name, after=after, limit=None if limit is None else limit - yielded)
            for version in page.versions:
                yield version
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if not page.next or page.next <= after or not page.versions:
                return
            after = page.next

    async def versions(self, name: str, *, after: int = 0, limit: int | None = None) -> builtins.list[models.RestorableVersion]:
        return [version async for version in self.iter_versions(name, after=after, limit=limit)]

    async def restore(
        self, name: str, *, to: PathSource, version: int | None = None, time: datetime | str | None = None
    ) -> models.RestoreResult:
        return await self._client._call(_wire.restore(name, to=to, version=version, time=time))

    async def sync(self, name: str) -> models.DatabaseStatus:
        return await self._client._call(_wire.sync(name))

    async def offload(self, name: str) -> models.DatabaseStatus:
        return await self._client._call(_wire.offload(name))

    async def writer(self, name: str) -> models.Writer:
        return await self._client._call(_wire.writer(name))

    async def acquire(
        self,
        name: str,
        *,
        to: PathSource | None = None,
        maintenance: bool = False,
        offload: Literal["never", "released"] = "never",
        machine_label: str | None = None,
        on_lost: Literal["conflict", "block"] = "conflict",
        timeout: Duration | None = None,
    ) -> models.AcquireResult:
        return await self._client._call(
            _wire.acquire(
                name, to=to, maintenance=maintenance, offload=offload, machine_label=machine_label, on_lost=on_lost, timeout=timeout
            )
        )

    async def release(
        self, name: str, *, wait: Durability | None = None, offload: bool = False, timeout: Duration | None = None
    ) -> models.ReleaseResult:
        return await self._client._call(_wire.release(name, wait=wait, offload=offload, timeout=timeout))

    def managed(
        self,
        name: str,
        *,
        durability: Durability,
        adapter: _managed.AsyncScopeAdapter[Connection],
        to: PathSource | None = None,
        offload: bool = False,
        cleanup_timeout: Duration | None = None,
        machine_label: str | None = None,
        maintenance: bool = False,
        on_lost: Literal["conflict", "block"] = "conflict",
        acquire_timeout: Duration | None = None,
    ) -> AbstractAsyncContextManager[Connection]:
        """See Databases.managed. Cleanup runs to completion under its own timeout even when the task is
        cancelled; the cancellation is re-raised afterwards, with any cleanup failure attached."""
        return _managed.managed_async(
            self._client,
            name,
            durability=durability,
            adapter=adapter,
            to=to,
            offload=offload,
            cleanup_timeout=cleanup_timeout,
            machine_label=machine_label,
            maintenance=maintenance,
            on_lost=on_lost,
            acquire_timeout=acquire_timeout,
        )


class AsyncReplicas(_AsyncGroup):
    """Live readonly replicas on this machine."""

    async def start(
        self, name: str, *, to: PathSource | None = None, write_protection: WriteProtection | None = None, timeout: Duration | None = None
    ) -> models.DatabaseStatus:
        return await self._client._call(_wire.start_replica(name, to=to, write_protection=write_protection, timeout=timeout))

    async def start_prefix(self, prefix: str, *, to: PathSource, write_protection: WriteProtection | None = None) -> models.PrefixReplica:
        return await self._client._call(_wire.start_prefix_replica(prefix, to=to, write_protection=write_protection))

    async def list(self) -> models.ReplicaList:
        return await self._client._call(_wire.list_replicas())

    async def stop(self, name: str) -> None:
        await self._client._call(_wire.stop_replica(name))

    async def stop_prefix(self, prefix: str, *, to: PathSource) -> None:
        await self._client._call(_wire.stop_prefix_replica(prefix, to=to))


class AsyncConflicts(_AsyncGroup):
    """Conflict copies, saved because they diverged from the server."""

    async def list(self) -> builtins.list[models.Conflict]:
        return await self._client._call(_wire.list_conflicts())

    async def delete(self, conflict_id: str) -> None:
        await self._client._call(_wire.delete_conflict(conflict_id))


class AsyncEvents(_AsyncGroup):
    """The daemon's event log."""

    async def page(self, *, after: int = 0, wait: Duration | None = None, limit: int | None = None) -> models.EventList:
        return await self._client._call(_wire.events_page(after=after, wait=wait, limit=limit))

    async def watch(
        self, *, after: int = 0, wait: Duration | None = None, on_error: Callable[[LitebackupError], None] | None = None
    ) -> AsyncIterator[models.Event]:
        """See Events.watch; cancel the task or stop iterating to end it."""
        wait_seconds = _wire.seconds(wait, "the event wait") or _wire.EVENT_WAIT_SECONDS
        delay = _wire.FIRST_RETRY_SECONDS
        while True:
            try:
                page = await self.page(after=after, wait=wait_seconds)
            except LitebackupError as error:
                if _wire.is_terminal_watch_error(error):
                    raise
                if on_error is not None:
                    on_error(error)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _wire.MAXIMUM_RETRY_SECONDS)
                continue
            delay = _wire.FIRST_RETRY_SECONDS
            for event in page.events:
                after = max(after, event.id)
                yield event
            after = max(after, page.next)


class AsyncSession(_AsyncGroup):
    """The session and its daemon."""

    async def status(self, *, after: str = "", limit: int = 100) -> models.Status:
        return await self._client._call(_wire.status(after=after, limit=limit))

    async def shutdown(
        self, *, wait: Literal["none", "server", "cloud"] | None = None, timeout: Duration | None = None
    ) -> models.ShutdownResult:
        return await self._client._call(_wire.shutdown(wait=wait, timeout=timeout))


class AsyncFailovers(_AsyncGroup):
    """Databases run on several identical machines."""

    async def start(
        self,
        path: PathSource,
        *,
        name: str | None = None,
        timeout: Duration | None = None,
        on_role_change: Callable[[str], None] | None = None,
        on_conflict_saved: Callable[[str], None] | None = None,
        on_error: Callable[[LitebackupError], None] | None = None,
    ) -> AsyncFailover:
        client = self._client
        call = _wire.start_failover(path, name=name, timeout=timeout)
        end = await client._call(_wire.events_page(after=-1))
        info = await client._call(call)
        if client._closed:
            raise LitebackupError(ErrorCode.CLOSED, "the client is closed")
        failover = AsyncFailover(
            client, info, end.next, on_role_change=on_role_change, on_conflict_saved=on_conflict_saved, on_error=on_error
        )
        client._failovers.add(failover)
        return failover
