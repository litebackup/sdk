"""Litebackup and AsyncLitebackup: the daemon of one session, over its unix socket."""

from __future__ import annotations

import asyncio
import builtins
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from datetime import datetime
from types import TracebackType
from typing import Any, Literal, TypeVar

from . import _foreground, _pool, _wire
from ._async_transport import AsyncTransport
from ._binary import litebackup_binary
from ._credential import API_KEY_ENVIRONMENT, credential_type
from ._failover import AsyncFailover, Failover
from ._generated import models
from ._launch import launch_daemon, launch_daemon_async, process_environment
from ._paths import session_socket
from ._transport import Answer, SyncTransport
from ._wire import Call, Durability, Duration, PathSource, WriteProtection
from .errors import ErrorCode, LeaseCleanupError, LitebackupError, invalid_argument

Result = TypeVar("Result")
DB = TypeVar("DB")


def _closed_error() -> LitebackupError:
    return LitebackupError(ErrorCode.CLOSED, "the client is closed")


class _Settings:
    """What a client was created with, resolved once."""

    def __init__(
        self,
        *,
        api_key: str | None,
        session: str | None,
        socket_path: str | None,
        binary_path: str | None,
        launch: bool,
        environment: Mapping[str, str] | None,
    ) -> None:
        self.environment = process_environment(environment)
        self.api_key = api_key if api_key is not None else self.environment.get(API_KEY_ENVIRONMENT)
        credential_type(self.api_key)
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
        if self.api_key is not None:
            environment[API_KEY_ENVIRONMENT] = self.api_key
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
        api_key: str | None = None,
        session: str | None = None,
        socket_path: str | None = None,
        binary_path: str | None = None,
        launch: bool = True,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """Arguments:

        api_key: the credential the daemon uses when it starts: an API key, or a join token it exchanges once for a
            key of this machine, then @ and the server's address; LITEBACKUP_API_KEY by default. The daemon keeps the
            key it saved unless this holds a different credential, which the launch refuses with
            ErrorCode.DRAIN_REQUIRED while the client holds write locks; a daemon already running keeps its key
            until it restarts. Foreground backups use it too.
        session: the session name; LITEBACKUP_SESSION, then "default".
        socket_path: connect to this socket and never start a daemon.
        binary_path: the litebackup binary that starts the daemon; see litebackup_binary() for the search.
        launch: start the session daemon when it is not running.
        environment: the environment for resolving paths and starting the daemon; os.environ by default.
        """
        self._settings = _Settings(
            api_key=api_key, session=session, socket_path=socket_path, binary_path=binary_path, launch=launch, environment=environment
        )
        self._transport = SyncTransport(self._settings.socket_path)
        self._connect_lock = threading.Lock()
        self._connected = False
        self._closed = False
        self._failovers: set[Failover] = set()
        self._jobs_lock = threading.Lock()
        self._jobs: set[subprocess.Popen[str]] = set()
        self.client_version = ""
        """The daemon's version, from its last answer."""
        self.backups = Backups(self)
        self.rules = Rules(self)
        self.databases = Databases(self)
        self.replicas = Replicas(self)
        self.events = Events(self)
        self.session = Session(self)
        self.failovers = Failovers(self)

    @property
    def socket_path(self) -> str:
        return self._settings.socket_path

    def _ensure_open(self) -> None:
        if self._closed:
            raise _closed_error()

    def connect(self) -> None:
        """Connects, starting the daemon when needed; fails with daemon_unsupported for a daemon older than 1.1.0."""
        self._ensure_open()
        with self._connect_lock:
            if self._connected:
                return
            settings = self._settings
            if not settings.explicit_socket and settings.launch and settings.session:
                launch_daemon(settings.session, settings.socket_path, settings.environment, settings.api_key, settings.binary_path)
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

    def _remove_failover(self, failover: Failover) -> None:
        self._failovers.discard(failover)

    def close(self) -> None:
        """Stops following failovers, ends running foreground backups, and closes connections.

        The daemon keeps running, and so do its failovers and acquired databases.
        """
        with self._jobs_lock:
            self._closed = True
            jobs = list(self._jobs)
        failovers = list(self._failovers)
        # Closing the transport first ends calls in flight, including the following
        # threads' long-polls and any call a failover callback waits on, so
        # _stop_following() never waits on a callback that waits on this client.
        self._transport.close()
        for failover in failovers:
            failover._stop_following()
        for job in jobs:
            _foreground.stop_job(job)
        for failover in failovers:
            failover._join()

    def __enter__(self) -> Litebackup:
        return self

    def __exit__(self, error_type: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()


class _Group:
    def __init__(self, client: Litebackup) -> None:
        self._client = client


class Backups(_Group):
    """Continuous backups and one-shot backup jobs."""

    def start(
        self,
        source: PathSource,
        *,
        name: str | None = None,
        prefix: str | None = None,
        maximum_local_databases: int | None = None,
        offload_durability: Durability = "server",
        offload_rollback_journal: bool = False,
    ) -> models.BackupRule:
        """Saves a continuous backup rule for a file, directory, or glob; returns once the daemon saved it.

        Starting the same path again with the same settings returns the saved rule, so
        call it on every start. Other settings for a saved path raise ``rule_exists`` with the
        saved rule in ``details["rule"]``.
        """
        return self._client._call(
            _wire.start_backup(
                source,
                name=name,
                prefix=prefix,
                maximum_local_databases=maximum_local_databases,
                offload_durability=offload_durability,
                offload_rollback_journal=offload_rollback_journal,
            )
        )

    def once(
        self,
        source: PathSource,
        *,
        name: str | None = None,
        prefix: str | None = None,
        durability: Durability = "server",
        timeout: Duration | None = None,
    ) -> None:
        """Runs one foreground backup job and returns once it exits, after the chosen destination acknowledged it.

        With a ``timeout``, a job still running 30 seconds after it ran out is killed
        and raises timeout. close() ends a running job, which then raises closed.
        """
        client = self._client
        client._ensure_open()
        settings = client._settings
        command = settings.foreground_command(source, name=name, prefix=prefix, durability=durability, timeout=timeout)
        try:
            process = subprocess.Popen(
                command, env=settings.foreground_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
            )
        except OSError as error:
            raise _foreground.start_failure(command[0], error) from error
        with client._jobs_lock:
            client._jobs.add(process)
            closed = client._closed
        try:
            if closed:
                _foreground.stop_job(process)
            try:
                stderr, timed_out = _foreground.await_job(process, _foreground.job_deadline(timeout))
            except BaseException:
                process.kill()
                process.wait()
                raise
        finally:
            with client._jobs_lock:
                client._jobs.discard(process)
        if timed_out:
            raise _foreground.timed_out_backup()
        if process.returncode != 0:
            if client._closed:
                raise _closed_error()
            raise _foreground.backup_failure(stderr, process.returncode)

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

    def all(self, *, limit: int = 100) -> Iterator[models.DatabaseStatus]:
        """Every database on this machine, a page at a time."""
        after = ""
        while True:
            page = self.page(after=after, limit=limit)
            yield from page.databases
            if not page.next:
                return
            after = page.next

    def list(self) -> builtins.list[models.DatabaseStatus]:
        return list(self.all())

    def versions_page(self, name: str, *, after: int = 0, limit: int | None = None) -> models.VersionList:
        return self._client._call(_wire.versions_page(name, after=after, limit=limit))

    def all_versions(self, name: str, *, after: int = 0, limit: int | None = None) -> Iterator[models.RestorableVersion]:
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
        return list(self.all_versions(name, after=after, limit=limit))

    def restore(
        self,
        name: str,
        *,
        path: PathSource,
        version: int | None = None,
        time: datetime | str | None = None,
        umask: int | None = None,
    ) -> models.RestoreResult:
        """Writes a version to a new file and checks it. An existing file is never overwritten (file_exists).

        The file gets 0666 less ``umask``, this process's umask when None.
        """
        return self._client._call(_wire.restore(name, path=path, version=version, time=time, umask=umask))

    def revert(self, name: str, *, version: int | None = None, time: datetime | str | None = None) -> models.RevertResult:
        """Rolls a database back under its own name: a past version's content becomes a new head, one past the current head.

        Pass ``version`` or ``time``, one of them; every later version stays restorable. The database must be one
        this machine acquired or backs up, and its file closed (still_open otherwise); a replica or a failover
        standby is refused (invalid_argument), and a file holding writes the server lacks is refused with conflict
        and left untouched. The daemon takes the write lock when it does not hold it and gives it back once the new
        head is synced. When that sync fails for any reason but a lost lock or a refused file, which are final, it
        keeps the lock and syncs again on its own, so release an acquired database afterwards.
        """
        return self._client._call(_wire.revert(name, version=version, time=time))

    def sync(self, name: str) -> models.DatabaseStatus:
        return self._client._call(_wire.sync(name))

    def offload(self, name: str, *, durability: Durability | None = None) -> models.DatabaseStatus:
        """Removes the local file of a released database once the server holds its last version, or the bucket with ``"cloud"``."""
        return self._client._call(_wire.offload(name, durability=durability))

    def writer(self, name: str) -> models.Writer:
        return self._client._call(_wire.writer(name))

    def acquire(
        self,
        name: str,
        *,
        path: PathSource | None = None,
        maintenance: bool = False,
        offload_on_release: bool = False,
        offload_durability: Durability = "server",
        machine_label: str | None = None,
        timeout: Duration | None = None,
        lock_timeout: Duration | None = None,
        umask: int | None = None,
        discard_local: bool = False,
    ) -> models.AcquireResult:
        """Takes the write lock and makes the local file the server's head. A ``pool()`` lease also opens, keeps, and releases it.

        A local file that diverged from the head is refused with ``conflict`` and left untouched; ``discard_local``
        replaces it with the head instead, discarding its writes the head lacks. While another machine holds
        the write lock, the daemon asks again for up to ``lock_timeout``; without it the acquire fails at once
        with ``lock_unavailable``.
        """
        return self._client._call(
            _wire.acquire(
                name,
                path=path,
                maintenance=maintenance,
                offload_on_release=offload_on_release,
                offload_durability=offload_durability,
                machine_label=machine_label,
                timeout=timeout,
                lock_timeout=lock_timeout,
                umask=umask,
                discard_local=discard_local,
            )
        )

    def release(
        self, name: str, *, durability: Durability | None = None, offload: bool = False, timeout: Duration | None = None
    ) -> models.ReleaseResult:
        """Syncs a final time and releases the write lock. Close every connection first, or it fails with still_open."""
        return self._client._call(_wire.release(name, durability=durability, offload=offload, timeout=timeout))

    def pool(
        self,
        adapter: _pool.Adapter[DB],
        *,
        idle_close: Duration | None = None,
        idle_release: Duration | None = None,
        cleanup_timeout: Duration | None = None,
        machine_label: str | None = None,
        on_release_error: Callable[[LeaseCleanupError], None] | None = None,
    ) -> _pool.Pool[DB]:
        """A pool that keeps databases open for calls, opened and closed with ``adapter``::

            with litebackup.databases.pool(adapter=sqlite3_adapter()) as pool:
                with pool.lease("agents/42.sqlite", path=path) as db:
                    db.execute("INSERT INTO steps (body) VALUES (?)", (step,))
                    db.commit()

        ``pool.use(name)`` is for a database a backup rule on this machine covers;
        ``pool.lease(name, ...)`` for one that moves between machines, whose write lock
        the pool keeps between leases until another machine asks for it.

        idle_close: closes a rule's database after this long unused, to cap open files; by default
            it stays open, except under a rule with maximum_local_databases, where it closes after
            two minutes so the rule can offload it.
        idle_release: releases a leased database after this long unused; by default the lease stays
            until another machine asks for it, ``release``, or ``close``.
        cleanup_timeout: bounds closing and releasing a leased database together; 10 minutes by default.
        machine_label: what other machines see as the writer of leased databases.
        on_release_error: called with each LeaseCleanupError of a release the pool started on its
            own; the database stays leased with its file intact, and the pool tries again next time.
            Logged by default.
        """
        return _pool.new_pool(self._client, adapter, idle_close, idle_release, cleanup_timeout, machine_label, on_release_error)


class Replicas(_Group):
    """Live readonly replicas on this machine."""

    def start(
        self, name: str, *, path: PathSource | None = None, write_protection: WriteProtection | None = None, timeout: Duration | None = None
    ) -> models.DatabaseStatus:
        """Keeps a live readonly replica and returns once its first version is local."""
        return self._client._call(_wire.start_replica(name, path=path, write_protection=write_protection, timeout=timeout))

    def start_prefix(self, prefix: str, *, directory: PathSource, write_protection: WriteProtection | None = None) -> models.PrefixReplica:
        """Replicates current and future databases under a prefix into a directory; downloads run in the background."""
        return self._client._call(_wire.start_prefix_replica(prefix, directory=directory, write_protection=write_protection))

    def list(self) -> models.ReplicaList:
        return self._client._call(_wire.list_replicas())

    def stop(self, name: str) -> None:
        self._client._call(_wire.stop_replica(name))

    def stop_prefix(self, prefix: str, *, directory: PathSource) -> None:
        self._client._call(_wire.stop_prefix_replica(prefix, directory=directory))


class Events(_Group):
    """The daemon's event log."""

    def page(self, *, after: int = 0, wait: Duration | None = None, limit: int | None = None) -> models.EventList:
        """One page after an event ID; -1 returns none and, as next, where the log ends."""
        return self._client._call(_wire.events_page(after=after, wait=wait, limit=limit))

    def watch(
        self, *, after: int = 0, wait: Duration | None = None, on_error: Callable[[LitebackupError], None] | None = None
    ) -> Iterator[models.Event]:
        """Yields events, oldest first, by long-polling until the caller stops.

        A failed long-poll is retried with backoff from the same cursor; sdk_unsupported,
        closed, cancelled, invalid_argument, and cursor_expired (the cursor is older than
        the oldest event kept; choose a new one) end the watch by raising.
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

    def shutdown(
        self, *, durability: Literal["none", "server", "cloud"] | None = None, timeout: Duration | None = None
    ) -> models.ShutdownResult:
        """Final-syncs every database, releases every write lock, and stops the daemon.

        A backup another machine writes holds no lock and stays waiting. Each database
        it could not sync or release is in failed, with its code, and keeps any write lock it holds;
        the session and the daemon then stay. An empty failed is the only complete
        shutdown. close() never does this.
        """
        return self._client._call(_wire.shutdown(durability=durability, timeout=timeout))


class Failovers(_Group):
    """Databases run on several identical machines."""

    def start(
        self,
        path: PathSource,
        *,
        name: str | None = None,
        timeout: Duration | None = None,
        discard_local: bool = False,
        on_role_change: Callable[[str], None] | None = None,
        on_refused: Callable[[str], None] | None = None,
        on_error: Callable[[LitebackupError], None] | None = None,
    ) -> Failover:
        """Runs the database at ``path``, the same on every machine; returns once this machine is the origin or holds a replica.

        ``on_role_change`` is called once with the role the start answered, then with every change:
        ``"origin"``, ``"replica"``, or ``"unknown"`` when the daemon stops answering; once it answers
        again, the role is read and reported. ``is_origin`` is False while the role is unknown.
        ``on_refused`` is called with the daemon's message when it refused this machine's file and the
        failover stays conflicted there, leaving the file as it is: a file that differs from the server head when
        this machine starts as the origin, or one holding writes the new origin never saw when this
        machine lost the origin role. The database's state is conflicted, and the role is ``"unknown"``, except for a
        refusal older than the last role read after the daemon answered again, which leaves the role that read gave.
        A file at the path that differs from the server head, and is not a clean copy of the version this machine
        last synced, is refused with ``conflict`` and left untouched; ``discard_local`` replaces it with the head.
        """
        client = self._client
        call = _wire.start_failover(path, name=name, timeout=timeout, discard_local=discard_local)
        end = client._call(_wire.events_page(after=-1))
        status = client._call(call)
        client._ensure_open()
        failover = Failover(client, status, end.next, on_role_change=on_role_change, on_refused=on_refused, on_error=on_error)
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
        api_key: str | None = None,
        session: str | None = None,
        socket_path: str | None = None,
        binary_path: str | None = None,
        launch: bool = True,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """Arguments are those of Litebackup."""
        self._settings = _Settings(
            api_key=api_key, session=session, socket_path=socket_path, binary_path=binary_path, launch=launch, environment=environment
        )
        self._transport = AsyncTransport(self._settings.socket_path)
        self._connect_lock: asyncio.Lock | None = None
        self._connected = False
        self._closed = False
        self._failovers: set[AsyncFailover] = set()
        self._jobs: set[asyncio.subprocess.Process] = set()
        self.client_version = ""
        """The daemon's version, from its last answer."""
        self.backups = AsyncBackups(self)
        self.rules = AsyncRules(self)
        self.databases = AsyncDatabases(self)
        self.replicas = AsyncReplicas(self)
        self.events = AsyncEvents(self)
        self.session = AsyncSession(self)
        self.failovers = AsyncFailovers(self)

    @property
    def socket_path(self) -> str:
        return self._settings.socket_path

    def _ensure_open(self) -> None:
        if self._closed:
            raise _closed_error()

    async def connect(self) -> None:
        """Connects, starting the daemon when needed; fails with daemon_unsupported for a daemon older than 1.1.0."""
        self._ensure_open()
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._connected:
                return
            settings = self._settings
            if not settings.explicit_socket and settings.launch and settings.session:
                await launch_daemon_async(
                    settings.session, settings.socket_path, settings.environment, settings.api_key, settings.binary_path
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

    def _remove_failover(self, failover: AsyncFailover) -> None:
        self._failovers.discard(failover)

    async def close(self) -> None:
        """Stops following failovers, ends running foreground backups, and closes connections. The daemon keeps running."""
        self._closed = True
        # Every step runs even when the caller is cancelled meanwhile, so no
        # failover keeps following; the cancellation is raised at the end.
        cancellation: asyncio.CancelledError | None = None
        for failover in list(self._failovers):
            try:
                await failover._stop_following()
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        try:
            await asyncio.gather(*(_foreground.stop_job_async(job) for job in list(self._jobs)))
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        try:
            await self._transport.close()
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        if cancellation is not None:
            raise cancellation

    async def __aenter__(self) -> AsyncLitebackup:
        return self

    async def __aexit__(self, error_type: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None) -> None:
        await self.close()


class _AsyncGroup:
    def __init__(self, client: AsyncLitebackup) -> None:
        self._client = client


class AsyncBackups(_AsyncGroup):
    """Continuous backups and one-shot backup jobs."""

    async def start(
        self,
        source: PathSource,
        *,
        name: str | None = None,
        prefix: str | None = None,
        maximum_local_databases: int | None = None,
        offload_durability: Durability = "server",
        offload_rollback_journal: bool = False,
    ) -> models.BackupRule:
        """See Backups.start: idempotent for the same settings, ``rule_exists`` with the saved rule for others."""
        return await self._client._call(
            _wire.start_backup(
                source,
                name=name,
                prefix=prefix,
                maximum_local_databases=maximum_local_databases,
                offload_durability=offload_durability,
                offload_rollback_journal=offload_rollback_journal,
            )
        )

    async def once(
        self,
        source: PathSource,
        *,
        name: str | None = None,
        prefix: str | None = None,
        durability: Durability = "server",
        timeout: Duration | None = None,
    ) -> None:
        """Runs one foreground backup job; cancelling the task stops the job with SIGTERM, then SIGKILL after 15 seconds.

        With a ``timeout``, a job still running 30 seconds after it ran out is stopped
        the same way and raises timeout. close() ends a running job, which then raises closed.
        """
        client = self._client
        client._ensure_open()
        settings = client._settings
        command = settings.foreground_command(source, name=name, prefix=prefix, durability=durability, timeout=timeout)
        try:
            process = await asyncio.create_subprocess_exec(
                *command, env=settings.foreground_environment(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        except OSError as error:
            raise _foreground.start_failure(command[0], error) from error
        client._jobs.add(process)
        try:
            if client._closed:
                # close() ran while the job started and may have missed it: stop it at once.
                await asyncio.shield(_foreground.await_job_async(process, 0))
                raise _closed_error()
            stderr, timed_out = await _foreground.await_job_async(process, _foreground.job_deadline(timeout))
        finally:
            client._jobs.discard(process)
        if timed_out:
            raise _foreground.timed_out_backup()
        if process.returncode != 0:
            if client._closed:
                raise _closed_error()
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

    async def all(self, *, limit: int = 100) -> AsyncIterator[models.DatabaseStatus]:
        """Every database on this machine, a page at a time: ``async for database in litebackup.databases.all()``."""
        after = ""
        while True:
            page = await self.page(after=after, limit=limit)
            for database in page.databases:
                yield database
            if not page.next:
                return
            after = page.next

    async def list(self) -> builtins.list[models.DatabaseStatus]:
        return [database async for database in self.all()]

    async def versions_page(self, name: str, *, after: int = 0, limit: int | None = None) -> models.VersionList:
        return await self._client._call(_wire.versions_page(name, after=after, limit=limit))

    async def all_versions(self, name: str, *, after: int = 0, limit: int | None = None) -> AsyncIterator[models.RestorableVersion]:
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
        return [version async for version in self.all_versions(name, after=after, limit=limit)]

    async def restore(
        self,
        name: str,
        *,
        path: PathSource,
        version: int | None = None,
        time: datetime | str | None = None,
        umask: int | None = None,
    ) -> models.RestoreResult:
        return await self._client._call(_wire.restore(name, path=path, version=version, time=time, umask=umask))

    async def revert(self, name: str, *, version: int | None = None, time: datetime | str | None = None) -> models.RevertResult:
        """Rolls a database back under its own name; see Databases.revert."""
        return await self._client._call(_wire.revert(name, version=version, time=time))

    async def sync(self, name: str) -> models.DatabaseStatus:
        return await self._client._call(_wire.sync(name))

    async def offload(self, name: str, *, durability: Durability | None = None) -> models.DatabaseStatus:
        """Removes the local file of a released database once the server holds its last version, or the bucket with ``"cloud"``."""
        return await self._client._call(_wire.offload(name, durability=durability))

    async def writer(self, name: str) -> models.Writer:
        return await self._client._call(_wire.writer(name))

    async def acquire(
        self,
        name: str,
        *,
        path: PathSource | None = None,
        maintenance: bool = False,
        offload_on_release: bool = False,
        offload_durability: Durability = "server",
        machine_label: str | None = None,
        timeout: Duration | None = None,
        lock_timeout: Duration | None = None,
        umask: int | None = None,
        discard_local: bool = False,
    ) -> models.AcquireResult:
        return await self._client._call(
            _wire.acquire(
                name,
                path=path,
                maintenance=maintenance,
                offload_on_release=offload_on_release,
                offload_durability=offload_durability,
                machine_label=machine_label,
                timeout=timeout,
                lock_timeout=lock_timeout,
                umask=umask,
                discard_local=discard_local,
            )
        )

    async def release(
        self, name: str, *, durability: Durability | None = None, offload: bool = False, timeout: Duration | None = None
    ) -> models.ReleaseResult:
        return await self._client._call(_wire.release(name, durability=durability, offload=offload, timeout=timeout))

    def pool(
        self,
        adapter: _pool.AsyncAdapter[DB],
        *,
        idle_close: Duration | None = None,
        idle_release: Duration | None = None,
        cleanup_timeout: Duration | None = None,
        machine_label: str | None = None,
        on_release_error: Callable[[LeaseCleanupError], None] | None = None,
    ) -> _pool.AsyncPool[DB]:
        """See Databases.pool; the AsyncPool's leases and uses are async context managers."""
        return _pool.new_async_pool(self._client, adapter, idle_close, idle_release, cleanup_timeout, machine_label, on_release_error)


class AsyncReplicas(_AsyncGroup):
    """Live readonly replicas on this machine."""

    async def start(
        self, name: str, *, path: PathSource | None = None, write_protection: WriteProtection | None = None, timeout: Duration | None = None
    ) -> models.DatabaseStatus:
        return await self._client._call(_wire.start_replica(name, path=path, write_protection=write_protection, timeout=timeout))

    async def start_prefix(
        self, prefix: str, *, directory: PathSource, write_protection: WriteProtection | None = None
    ) -> models.PrefixReplica:
        return await self._client._call(_wire.start_prefix_replica(prefix, directory=directory, write_protection=write_protection))

    async def list(self) -> models.ReplicaList:
        return await self._client._call(_wire.list_replicas())

    async def stop(self, name: str) -> None:
        await self._client._call(_wire.stop_replica(name))

    async def stop_prefix(self, prefix: str, *, directory: PathSource) -> None:
        await self._client._call(_wire.stop_prefix_replica(prefix, directory=directory))


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
        self, *, durability: Literal["none", "server", "cloud"] | None = None, timeout: Duration | None = None
    ) -> models.ShutdownResult:
        return await self._client._call(_wire.shutdown(durability=durability, timeout=timeout))


class AsyncFailovers(_AsyncGroup):
    """Databases run on several identical machines."""

    async def start(
        self,
        path: PathSource,
        *,
        name: str | None = None,
        timeout: Duration | None = None,
        discard_local: bool = False,
        on_role_change: Callable[[str], None] | None = None,
        on_refused: Callable[[str], None] | None = None,
        on_error: Callable[[LitebackupError], None] | None = None,
    ) -> AsyncFailover:
        client = self._client
        call = _wire.start_failover(path, name=name, timeout=timeout, discard_local=discard_local)
        end = await client._call(_wire.events_page(after=-1))
        status = await client._call(call)
        client._ensure_open()
        failover = AsyncFailover(client, status, end.next, on_role_change=on_role_change, on_refused=on_refused, on_error=on_error)
        client._failovers.add(failover)
        return failover
