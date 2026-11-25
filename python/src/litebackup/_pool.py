"""Pools: databases kept open for calls, the way an app serving many tenants needs them.

``use`` is for a database a backup rule on this machine covers: it stays here,
open between calls. ``lease`` is for a database that moves between machines: the
first lease acquires the write lock, and the pool keeps it, with the open handle,
until ``idle_release`` runs out (a minute by default), ``release``, or ``close``.

The app coordinates handovers: another machine's lease with ``lock_timeout``
succeeds once this one released the database. A lease on a database whose lock
was lost meanwhile fails with lock_lost before its block, and the next lease
acquires again.

A close that fails never releases: a connection may still be open, and another
machine must not write while this one still can.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Generic, Literal, Protocol, TypeVar, cast

from ._generated import models
from ._wire import (
    DURABILITY_CLOUD,
    DURABILITY_SERVER,
    JOURNAL_MODE_ROLLBACK,
    ROLE_ORIGIN,
    STATE_CONFLICTED,
    STATE_OFFLOADED,
    Durability,
    Duration,
    PathSource,
    absolute_path,
    seconds,
)
from .errors import LEASE_CLEANUP_CLOSE, LEASE_CLEANUP_RELEASE, ErrorCode, LeaseCleanupError, LitebackupError, invalid_argument

if TYPE_CHECKING:
    from .client import AsyncLitebackup, Litebackup

DB = TypeVar("DB")

DEFAULT_CLEANUP_TIMEOUT = 600.0
"""Seconds that bound closing and releasing a leased database unless cleanup_timeout says otherwise."""
DEFAULT_IDLE_RELEASE = 60.0
"""Seconds a lease stays unused before the pool releases it, unless idle_release says otherwise."""
DEFAULT_OFFLOAD_IDLE_CLOSE = 120.0
"""Seconds a database whose backup rule offloads (maximum_local_databases) stays open unused before the pool
closes it, so the rule can offload it, unless idle_close says otherwise. It is well inside the client's
10-minute idle window."""
CLOUD_DURABILITY_POLL_SECONDS = 1.0
"""How often a lease with cloud durability asks whether the bucket holds its version yet."""
POOL_CHECK_SECONDS = 15.0
"""How often a pool looks for idle databases; tests shorten it."""
IdleStep = Literal["release", "close"]
IDLE_RELEASE_STEP: Final[IdleStep] = "release"
"""An idle check releases an unused lease."""
IDLE_CLOSE_STEP: Final[IdleStep] = "close"
"""An idle check closes an unused rule database."""
_MINIMUM_RELEASE_SECONDS = 0.001
"""A release timeout of zero means the daemon's default, so a spent cleanup timeout sends the smallest one instead."""

logger = logging.getLogger("litebackup")


class Adapter(Protocol[DB]):
    """Opens and closes the handle a Pool yields to its calls.

    open must open an existing file and never create one: a missing file is an
    offloaded copy the pool brings back, never an empty new database; raise
    LitebackupError with code not_found for it. close must roll back a transaction
    a call left unfinished, never commit it, and raise when a connection may still
    be open: the pool releases a leased database only after close succeeds.
    ``litebackup.integrations.sqlite3.adapter()`` is one.
    """

    def open(self, path: str) -> DB: ...

    def close(self, db: DB) -> None: ...


class AsyncAdapter(Protocol[DB]):
    """The asyncio form of Adapter, such as ``litebackup.integrations.aiosqlite.adapter()``."""

    async def open(self, path: str) -> DB: ...

    async def close(self, db: DB) -> None: ...


@dataclass(frozen=True, slots=True)
class _PoolSettings:
    """What a pool was created with, checked once."""

    idle_close: float | None
    idle_release: float | None
    """None never releases an unused lease."""
    cleanup_seconds: float
    machine_label: str | None
    on_release_error: Callable[[LeaseCleanupError], None] | None


def _pool_settings(
    adapter: object,
    idle_close: Duration | None,
    idle_release: Duration | None,
    cleanup_timeout: Duration | None,
    machine_label: str | None,
    on_release_error: Callable[[LeaseCleanupError], None] | None,
) -> _PoolSettings:
    if adapter is None:
        raise invalid_argument("a pool needs an adapter")
    cleanup_seconds = DEFAULT_CLEANUP_TIMEOUT if cleanup_timeout is None else seconds(cleanup_timeout, "the cleanup timeout")
    if not cleanup_seconds:
        raise invalid_argument("the cleanup timeout must be positive")
    return _PoolSettings(
        idle_close=seconds(idle_close, "the idle close"),
        idle_release=DEFAULT_IDLE_RELEASE if idle_release is None else seconds(idle_release, "the idle release"),
        cleanup_seconds=cleanup_seconds,
        machine_label=machine_label,
        on_release_error=on_release_error,
    )


@dataclass(frozen=True, slots=True)
class _LeaseSettings:
    """One lease call's settings; the release that ends the lease uses the last call's."""

    path: PathSource | None
    require_existing: bool
    durability: Durability | None
    offload: bool
    lock_timeout: float | None
    acquire_timeout: float | None
    maintenance: bool

    @property
    def release_durability(self) -> Durability:
        return self.durability or DURABILITY_SERVER


def _lease_settings(
    path: PathSource | None,
    require_existing: bool,
    durability: str | None,
    offload: bool,
    lock_timeout: Duration | None,
    acquire_timeout: Duration | None,
    maintenance: bool,
) -> _LeaseSettings:
    if durability not in (None, DURABILITY_SERVER, DURABILITY_CLOUD):
        raise invalid_argument("a lease's durability must be server or cloud")
    return _LeaseSettings(
        path=path,
        require_existing=require_existing,
        durability=cast("Durability | None", durability),
        offload=offload,
        lock_timeout=seconds(lock_timeout, "the lock timeout"),
        acquire_timeout=seconds(acquire_timeout, "the acquire timeout"),
        maintenance=maintenance,
    )


class _PoolEntry(Generic[DB]):
    """One database of a pool.

    turn serializes opening, bringing back, acquiring, lease calls, and releasing;
    use calls share the open handle without it. users and used are guarded
    by the pool's mutex.
    """

    def __init__(self, name: str, turn: threading.Lock | asyncio.Lock) -> None:
        self.name = name
        self.turn = turn
        self.db: DB | None = None
        self.path = ""
        self.opened = False
        """Set while db is open."""
        self.leased = False
        """Set while this pool holds the write lock through lease."""
        self.lease_settings: _LeaseSettings | None = None
        self.last_call_failed = False
        """Keeps the release from offloading after a failed call."""
        self.idle_close: float | None = None
        """Seconds a rule's database stays open unused; None never closes it."""
        self.close_after_use = False
        """Set for a rollback-journal database whose rule offloads rollback-journal databases: its rule offloads it
        while no process has it open, so the pool closes it as soon as the last use block exits."""
        self.users = 0
        """Calls between joining and leaving, bodies included."""
        self.used: float | None = None


def _create_empty_file(path: PathSource) -> None:
    """Creates an empty file and its directory unless something is already there.

    The daemon treats an empty file as no copy: a new database when the server has
    none, replaced by the server's head otherwise.
    """
    file = absolute_path(path)
    try:
        # A file where the directory should be fails here too, as FileExistsError.
        os.makedirs(os.path.dirname(file), exist_ok=True)
    except OSError as error:
        raise invalid_argument(f"creating {file} failed: {error}") from error
    try:
        os.close(os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        pass
    except OSError as error:
        raise invalid_argument(f"creating {file} failed: {error}") from error


def _raise_together(failures: list[BaseException], message: str) -> None:
    """Raises one failure alone, or several as a group, first one first."""
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup(f"{message} failed in {len(failures)} ways", failures)


def _close_failed(entry: _PoolEntry[Any], error: BaseException) -> LeaseCleanupError:
    failure = LeaseCleanupError(
        LEASE_CLEANUP_CLOSE,
        entry.name,
        f"closing {entry.name} failed, so it was not released and stays acquired by this machine: {error}. "
        f"Close every connection to {entry.path}, then call pool.release({entry.name!r}) again",
        {"step": LEASE_CLEANUP_CLOSE, "name": entry.name, "path": entry.path, "acquired": True},
    )
    failure.__cause__ = error
    return failure


def _release_failed(entry: _PoolEntry[Any], durability: str, error: BaseException) -> LeaseCleanupError:
    failure = LeaseCleanupError(
        LEASE_CLEANUP_RELEASE,
        entry.name,
        f"releasing {entry.name} failed: {error}. Its file at {entry.path} is intact and this machine may still hold "
        f"its write lock; call pool.release({entry.name!r}) again",
        {"step": LEASE_CLEANUP_RELEASE, "name": entry.name, "path": entry.path, "durability": durability},
    )
    failure.__cause__ = error
    return failure


def _use_close_failed(entry: _PoolEntry[Any], error: BaseException) -> LeaseCleanupError:
    failure = LeaseCleanupError(
        LEASE_CLEANUP_CLOSE,
        entry.name,
        f"closing {entry.name} after its last use failed, so it stays open and its rule cannot offload it: {error}. "
        "The pool tries again at its next idle check",
        {"step": LEASE_CLEANUP_CLOSE, "name": entry.name, "path": entry.path, "acquired": False},
    )
    failure.__cause__ = error
    return failure


def _lease_lost(name: str) -> LitebackupError:
    return LitebackupError(
        ErrorCode.LOCK_LOST,
        f"this machine no longer holds the write lock of {name}, so the pool dropped its lease; the next lease acquires it again",
    )


def _lock_lost(status: models.DatabaseStatus | None) -> bool:
    """Whether a leased database's status shows its lock was lost; None is a database the daemon no longer tracks."""
    return status is None or not status.acquired or status.state == STATE_CONFLICTED


def _not_rule_database(name: str) -> LitebackupError:
    return invalid_argument(
        f"{name} is not a backup rule's database on this machine; use lease() for a database that moves between machines"
    )


def _is_rule_database(status: models.DatabaseStatus) -> bool:
    return status.role == ROLE_ORIGIN and not status.acquired and not status.failover and bool(status.backup_rule_id)


def _pool_closed() -> LitebackupError:
    return LitebackupError(ErrorCode.CLOSED, "the pool is closed")


class _PoolCore(Generic[DB]):
    """The entries and rules a pool keeps, shared by Pool and AsyncPool."""

    def __init__(self, settings: _PoolSettings) -> None:
        self._settings = settings
        self._mutex = threading.Lock()
        self._entries: dict[str, _PoolEntry[DB]] = {}
        self._rules: dict[str, models.BackupRule] = {}
        self._active_calls = 0
        self._closing = False
        """Set by close: new calls are refused."""

    def _new_turn(self) -> threading.Lock | asyncio.Lock:
        raise NotImplementedError

    def _calls_finished(self) -> None:
        """Called under the mutex once a closing pool has no active call."""

    def _join(self, name: str) -> _PoolEntry[DB]:
        """Counts a call on the name's entry, creating it."""
        if not name:
            raise invalid_argument("a pool call needs a database name")
        with self._mutex:
            if self._closing:
                raise _pool_closed()
            entry = self._entries.get(name)
            if entry is None:
                entry = self._entries[name] = _PoolEntry(name, self._new_turn())
            entry.users += 1
            self._active_calls += 1
            return entry

    def _leave(self, entry: _PoolEntry[DB]) -> None:
        """Ends a call on an entry; an entry with nothing open or leased is dropped."""
        with self._mutex:
            entry.users -= 1
            self._active_calls -= 1
            entry.used = time.monotonic()
            if entry.users == 0 and not entry.opened and not entry.leased and self._entries.get(entry.name) is entry:
                del self._entries[entry.name]
            if self._closing and self._active_calls == 0:
                self._calls_finished()

    def _start_closing(self) -> bool:
        """Refuses new calls; False when the pool was closing already."""
        with self._mutex:
            if self._closing:
                return False
            self._closing = True
            if self._active_calls == 0:
                self._calls_finished()
            return True

    def _all_entries(self) -> list[_PoolEntry[DB]]:
        with self._mutex:
            return list(self._entries.values())

    def _idle_entries(self) -> list[_PoolEntry[DB]]:
        with self._mutex:
            return [entry for entry in self._entries.values() if entry.users == 0 and entry.used is not None]

    def _idle_seconds(self, entry: _PoolEntry[DB], now: float) -> float | None:
        """How long an entry has been unused, or None while a call uses it."""
        with self._mutex:
            if entry.users > 0 or entry.used is None:
                return None
            return now - entry.used

    def _remove_unused_entry(self, entry: _PoolEntry[DB]) -> None:
        """Drops an entry with nothing open or leased once no call uses it."""
        with self._mutex:
            if self._entries.get(entry.name) is entry and entry.users == 0 and not entry.opened and not entry.leased:
                del self._entries[entry.name]

    def _cached_rule(self, rule_id: str) -> models.BackupRule | None:
        with self._mutex:
            return self._rules.get(rule_id)

    def _cache_rules(self, rules: list[models.BackupRule], rule_id: str) -> models.BackupRule | None:
        with self._mutex:
            for listed in rules:
                self._rules[listed.id] = listed
            return self._rules.get(rule_id)

    def _rule_lookup_needed(self, status: models.DatabaseStatus) -> bool:
        return not self._settings.idle_close or status.journal_mode == JOURNAL_MODE_ROLLBACK

    def _set_rule_closing(self, entry: _PoolEntry[DB], status: models.DatabaseStatus, rule: models.BackupRule | None) -> None:
        """Sets when a rule's database closes: after its idle close, and after each use under a rule that
        offloads rollback-journal databases."""
        entry.idle_close = self._idle_close_seconds(rule)
        entry.close_after_use = status.journal_mode == JOURNAL_MODE_ROLLBACK and rule is not None and bool(rule.offload_rollback_journal)

    def _last_use_ended(self, entry: _PoolEntry[DB]) -> bool:
        """Whether an entry that closes after use has no active call and an open handle; the caller holds its turn."""
        with self._mutex:
            return entry.close_after_use and entry.users == 0 and entry.opened and not entry.leased

    def _idle_close_seconds(self, rule: models.BackupRule | None) -> float | None:
        if self._settings.idle_close:
            return self._settings.idle_close
        if rule is not None and (rule.maximum_local_databases or 0) > 0:
            return DEFAULT_OFFLOAD_IDLE_CLOSE
        return None

    def _release_durability_and_offload(self, entry: _PoolEntry[DB]) -> tuple[Durability, bool]:
        lease_settings = entry.lease_settings
        if lease_settings is None:
            return DURABILITY_SERVER, False
        return lease_settings.release_durability, lease_settings.offload and not entry.last_call_failed

    def _idle_step(self, entry: _PoolEntry[DB], now: float) -> IdleStep | None:
        """What an idle check does with an entry whose turn the caller holds: release, close, or nothing."""
        idle_seconds = self._idle_seconds(entry, now)
        if idle_seconds is None:
            return None
        idle_release = self._settings.idle_release
        if entry.leased and idle_release and idle_seconds >= idle_release:
            return IDLE_RELEASE_STEP
        if not entry.leased and entry.opened and entry.close_after_use:
            # Its close after the last use failed: try again at once.
            return IDLE_CLOSE_STEP
        if not entry.leased and entry.opened and entry.idle_close and idle_seconds >= entry.idle_close:
            return IDLE_CLOSE_STEP
        return None

    def _acquired_path(self, acquired: models.AcquireResult, lease_settings: _LeaseSettings, name: str) -> str:
        if not acquired.database.acquired:
            raise invalid_argument(f"{name} is a backup rule's database on this machine; use use() for it")
        if acquired.database.path:
            return acquired.database.path
        return absolute_path(lease_settings.path) if lease_settings.path else ""


class Pool(_PoolCore[DB]):
    """Keeps databases open for calls; safe to share between threads. Create it with ``databases.pool()``::

        with litebackup.databases.pool(adapter=sqlite3_adapter()) as pool:
            with pool.lease("agents/42.sqlite", path="data/agents/42.sqlite") as db:
                db.execute("INSERT INTO steps (body) VALUES (?)", ("tests passed",))
                db.commit()

    ``use`` is for a database a backup rule on this machine covers, ``lease`` for one
    that moves between machines. Close the pool when done: it waits for active
    blocks, then releases every leased database and closes every open one. Never
    close a pool from inside one of its blocks.
    """

    def __init__(self, client: Litebackup, adapter: Adapter[DB], settings: _PoolSettings) -> None:
        super().__init__(settings)
        self._client = client
        self._adapter = adapter
        self._calls_done = threading.Condition(self._mutex)
        self._no_calls = False
        self._stopped = threading.Event()
        self._idle_thread = threading.Thread(target=self._close_idle_databases, name="litebackup-pool-idle", daemon=True)
        self._idle_thread.start()

    def _new_turn(self) -> threading.Lock:
        return threading.Lock()

    def _calls_finished(self) -> None:
        self._no_calls = True
        self._calls_done.notify_all()

    def __enter__(self) -> Pool[DB]:
        return self

    def __exit__(self, error_type: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()

    @contextmanager
    def use(self, name: str) -> Iterator[DB]:
        """Yields the open handle of a database a backup rule on this machine covers.

        The pool opens it on first use and keeps it open; an offloaded copy is brought
        back first with acquire, never recreated empty. A rollback-journal database
        under a rule with offload_rollback_journal is closed as soon as its last use
        block exits instead, because that rule offloads it while nothing has it open. Uses of one database share the
        handle, concurrently, so the adapter's handle must be safe to share between
        threads. A name no rule on this machine covers, or an acquired database, is
        refused with invalid_argument; lease it instead.
        """
        entry = self._join(name)
        try:
            with cast(threading.Lock, entry.turn):
                if entry.leased:
                    raise invalid_argument(f"{name} is leased in this pool; use lease() for it")
                if not entry.opened:
                    self._open_rule_database(entry)
                db = cast(DB, entry.db)
            yield db
        finally:
            self._leave(entry)
            if entry.close_after_use:
                self._close_after_last_use(entry)

    def _close_after_last_use(self, entry: _PoolEntry[DB]) -> None:
        """Closes a database its rule offloads while nothing has it open, once no use block is active."""
        failure: LeaseCleanupError | None = None
        with cast(threading.Lock, entry.turn):
            if not self._last_use_ended(entry):
                return
            try:
                self._adapter.close(cast(DB, entry.db))
            except Exception as error:
                failure = _use_close_failed(entry, error)
            else:
                entry.db, entry.opened = None, False
                self._remove_unused_entry(entry)
        if failure is not None:
            self._report_release_error(failure)

    def _open_rule_database(self, entry: _PoolEntry[DB]) -> None:
        """Opens a rule's database, bringing an offloaded copy back first, and tries once more when the copy
        was offloaded between the two."""
        for attempt in range(2):
            status = self._client.databases.get(entry.name)
            if not _is_rule_database(status):
                raise _not_rule_database(entry.name)
            if status.state == STATE_OFFLOADED:
                self._client.databases.acquire(entry.name)
            try:
                db = self._adapter.open(status.path)
            except BaseException:
                if attempt == 0 and not os.path.exists(status.path):
                    continue
                raise
            entry.db, entry.path, entry.opened = db, status.path, True
            rule = self._database_rule(cast(str, status.backup_rule_id)) if self._rule_lookup_needed(status) else None
            self._set_rule_closing(entry, status, rule)
            return

    def _database_rule(self, rule_id: str) -> models.BackupRule | None:
        """The backup rule with rule_id, from the pool's cache or rules.list."""
        rule = self._cached_rule(rule_id)
        if rule is None:
            rule = self._cache_rules(self._client.rules.list(), rule_id)
        return rule

    @contextmanager
    def lease(
        self,
        name: str,
        *,
        path: PathSource | None = None,
        require_existing: bool = False,
        durability: Durability | None = None,
        offload: bool = False,
        lock_timeout: Duration | None = None,
        acquire_timeout: Duration | None = None,
        maintenance: bool = False,
    ) -> Iterator[DB]:
        """Yields the open handle of a database that moves between machines, exclusively.

        Leases of one name run one at a time, and no other machine writes while the
        pool keeps the lock. The first lease acquires the write lock, creating an
        empty file and its directory at ``path`` when nothing is there unless
        ``require_existing``, and opens it; later leases reuse the lock and the handle.
        ``lock_timeout`` is how long that acquire waits while another machine holds the
        write lock; without it the lease fails at once with lock_unavailable.
        ``acquire_timeout`` bounds downloading the head; ``maintenance`` checkpoints on
        release and VACUUMs past the freelist threshold.

        A later lease first asks the daemon whether this machine still holds the lock:
        when it lost it, the handle is closed, the lease dropped, and lock_lost raised
        without entering the block; the next lease acquires again. A loss while a block
        is active shows at the next lease.

        With ``durability``, a block that ends normally syncs and returns only once the
        server, or the bucket with ``"cloud"``, holds what it committed; without it,
        the block's writes sync in the background. The release that ends the lease
        waits for the same durability, the server when None, and with ``offload``
        removes the local file, unless the last call failed: its block raised or its
        durability wait did. A block that raises keeps the lease and its error propagates.

        The lease ends after ``idle_release`` without a block, on ``release``, or on
        ``close``; the app coordinates handovers between machines. The adapters never
        commit for the block: commit what it should keep.
        """
        lease_settings = _lease_settings(path, require_existing, durability, offload, lock_timeout, acquire_timeout, maintenance)
        entry = self._join(name)
        try:
            with cast(threading.Lock, entry.turn):
                if entry.opened and not entry.leased:
                    raise invalid_argument(f"{name} is a backup rule's database in this pool; use use() for it")
                if entry.leased:
                    self._check_lease(entry)
                entry.lease_settings = lease_settings
                if not entry.leased:
                    self._acquire_lease(entry, lease_settings)
                elif not entry.opened:
                    self._reopen_lease(entry)
                # The call counts as failed until its work and its durability wait both finished.
                entry.last_call_failed = True
                yield cast(DB, entry.db)
                if lease_settings.durability is not None:
                    self._wait_durable(name, lease_settings.durability)
                entry.last_call_failed = False
        finally:
            self._leave(entry)

    def _check_lease(self, entry: _PoolEntry[DB]) -> None:
        """Drops a lease whose lock this machine lost and raises lock_lost; the caller holds entry.turn."""
        try:
            status: models.DatabaseStatus | None = self._client.databases.get(entry.name)
        except LitebackupError as error:
            if error.code != ErrorCode.NOT_FOUND:
                raise
            status = None
        if _lock_lost(status):
            self._drop_lost_lease(entry)
            raise _lease_lost(entry.name)

    def _reopen_lease(self, entry: _PoolEntry[DB]) -> None:
        """Opens the handle of a lease whose release failed after its handle closed."""
        try:
            entry.db = self._adapter.open(entry.path)
        except BaseException:
            entry.last_call_failed = True
            raise
        entry.opened = True

    def _acquire_lease(self, entry: _PoolEntry[DB], lease_settings: _LeaseSettings) -> None:
        """Acquires the write lock and opens the database; one that does not open is released again at once."""
        if lease_settings.path and not lease_settings.require_existing:
            _create_empty_file(lease_settings.path)
        acquired = self._client.databases.acquire(
            entry.name,
            path=lease_settings.path,
            maintenance=lease_settings.maintenance,
            machine_label=self._settings.machine_label,
            timeout=lease_settings.acquire_timeout,
            lock_timeout=lease_settings.lock_timeout,
        )
        path = self._acquired_path(acquired, lease_settings, entry.name)
        try:
            db = self._adapter.open(path)
        except BaseException as error:
            # Nothing is open: release the lock again, without offload.
            entry.path, entry.leased, entry.last_call_failed = path, True, True
            try:
                self._release_lease(entry)
            except LeaseCleanupError as release_error:
                _raise_together([error, release_error], f"leasing {entry.name}")
            raise
        entry.db, entry.path, entry.opened, entry.leased = db, path, True, True

    def _wait_durable(self, name: str, durability: Durability) -> None:
        """Syncs now and returns once the server, or the bucket with cloud, holds the version the sync reached."""
        status = self._client.databases.sync(name)
        if durability != DURABILITY_CLOUD:
            return
        target = status.synced_server.version
        while status.synced_cloud.version < target:
            time.sleep(CLOUD_DURABILITY_POLL_SECONDS)
            status = self._client.databases.get(name)

    def release(self, name: str) -> None:
        """Ends the lease of a database once an active lease block returns: closes the handle, then releases
        the write lock once the final version reaches the lease's durability.

        Raises LeaseCleanupError when closing or releasing fails, and invalid_argument for a
        database the pool does not lease.
        """
        with self._mutex:
            entry = self._entries.get(name)
        if entry is None:
            raise invalid_argument(f"{name} is not leased in this pool")
        with cast(threading.Lock, entry.turn):
            if not entry.leased:
                raise invalid_argument(f"{name} is not leased in this pool")
            self._release_lease(entry)

    def _release_lease(self, entry: _PoolEntry[DB]) -> None:
        """Closes the handle and releases the lock within one cleanup timeout; the caller holds entry.turn.

        Closing runs on the calling thread and is not cut short; the release gets what closing left.
        """
        deadline = time.monotonic() + self._settings.cleanup_seconds
        if entry.opened:
            try:
                self._adapter.close(cast(DB, entry.db))
            except Exception as error:
                raise _close_failed(entry, error) from error
            entry.db, entry.opened = None, False
        durability, offload = self._release_durability_and_offload(entry)
        timeout = max(deadline - time.monotonic(), _MINIMUM_RELEASE_SECONDS)
        try:
            self._client.databases.release(entry.name, durability=durability, offload=offload, timeout=timeout)
        except Exception as error:
            raise _release_failed(entry, durability, error) from error
        entry.leased = False
        self._remove_unused_entry(entry)

    def _drop_lost_lease(self, entry: _PoolEntry[DB]) -> None:
        """Closes the handle of a database whose lock was lost, quietly, and drops the lease; the copy stays as
        the daemon left it. The caller holds entry.turn."""
        if entry.opened:
            try:
                self._adapter.close(cast(DB, entry.db))
            except Exception as error:
                logger.warning("closing %s after its write lock was lost failed: %s", entry.name, error)
            entry.db, entry.opened = None, False
        entry.leased = False

    def close(self) -> None:
        """Refuses new calls, waits for every active use and lease block, then releases every leased database
        and closes every open one.

        A failure raises alone, several as an ExceptionGroup; a LeaseCleanupError names
        a database that stays acquired. A second close returns at once.
        """
        if not self._start_closing():
            return
        with self._calls_done:
            self._calls_done.wait_for(lambda: self._no_calls)
        self._stopped.set()
        if self._idle_thread is not threading.current_thread():
            self._idle_thread.join()
        failures: list[BaseException] = []
        for entry in self._all_entries():
            with cast(threading.Lock, entry.turn):
                if entry.leased:
                    try:
                        self._release_lease(entry)
                    except LeaseCleanupError as error:
                        failures.append(error)
                elif entry.opened:
                    try:
                        self._adapter.close(cast(DB, entry.db))
                    except Exception as error:
                        failures.append(error)
                    else:
                        entry.db, entry.opened = None, False
                        self._remove_unused_entry(entry)
        _raise_together(failures, "closing the pool")

    def _close_idle_databases(self) -> None:
        """Closes rule databases past their idle close and releases leases past idle_release, until the pool closes."""
        while not self._stopped.wait(POOL_CHECK_SECONDS):
            now = time.monotonic()
            for entry in self._idle_entries():
                failure = self._close_idle_entry(entry, now)
                if failure is not None:
                    self._report_release_error(failure)

    def _close_idle_entry(self, entry: _PoolEntry[DB], now: float) -> LeaseCleanupError | None:
        """Releases or closes one idle entry; returns a failed release to report once its turn is free."""
        turn = cast(threading.Lock, entry.turn)
        if not turn.acquire(blocking=False):
            return None
        try:
            idle_step = self._idle_step(entry, now)
            if idle_step == IDLE_RELEASE_STEP:
                try:
                    self._release_lease(entry)
                except LeaseCleanupError as error:
                    return error
            elif idle_step == IDLE_CLOSE_STEP:
                try:
                    self._adapter.close(cast(DB, entry.db))
                except Exception as error:
                    # The entry keeps its handle, so the next check and close() try again.
                    logger.warning("closing the idle database %s failed: %s", entry.name, error)
                    return None
                entry.db, entry.opened = None, False
                self._remove_unused_entry(entry)
            return None
        finally:
            turn.release()

    def _report_release_error(self, error: LeaseCleanupError) -> None:
        """Hands a failed background release to on_release_error on its own thread, which close() never waits
        for, so the callback may call the pool; logs it without a callback."""
        callback = self._settings.on_release_error
        if callback is None:
            logger.warning("the pool could not release %s: %s", error.name, error)
            return
        threading.Thread(target=callback, args=(error,), name="litebackup-pool-release-error", daemon=True).start()


class AsyncPool(_PoolCore[DB]):
    """The asyncio form of Pool; create it with ``databases.pool()`` on AsyncLitebackup::

        async with litebackup.databases.pool(adapter=aiosqlite_adapter()) as pool:
            async with pool.lease("agents/42.sqlite", path="data/agents/42.sqlite") as db:
                await db.execute("INSERT INTO steps (body) VALUES (?)", ("tests passed",))
                await db.commit()

    Its idle task starts on the first call, in that call's event loop; use one pool
    from one event loop.
    """

    def __init__(self, client: AsyncLitebackup, adapter: AsyncAdapter[DB], settings: _PoolSettings) -> None:
        super().__init__(settings)
        self._client = client
        self._adapter = adapter
        self._idle_task: asyncio.Task[None] | None = None
        self._stopped: asyncio.Event | None = None
        self._no_calls: asyncio.Event | None = None
        self._no_calls_reached = False
        self._close_task: asyncio.Task[None] | None = None
        self._close_reported = False

    def _new_turn(self) -> asyncio.Lock:
        return asyncio.Lock()

    def _calls_finished(self) -> None:
        self._no_calls_reached = True
        if self._no_calls is not None:
            self._no_calls.set()

    async def __aenter__(self) -> AsyncPool[DB]:
        return self

    async def __aexit__(self, error_type: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None) -> None:
        await self.close()

    def _start_idle_task(self) -> None:
        if self._idle_task is None:
            self._stopped = asyncio.Event()
            self._idle_task = asyncio.get_running_loop().create_task(self._close_idle_databases(), name="litebackup-pool-idle")

    @asynccontextmanager
    async def use(self, name: str) -> AsyncIterator[DB]:
        """See Pool.use."""
        entry = self._join(name)
        try:
            self._start_idle_task()
            async with cast(asyncio.Lock, entry.turn):
                if entry.leased:
                    raise invalid_argument(f"{name} is leased in this pool; use lease() for it")
                if not entry.opened:
                    await self._open_rule_database(entry)
                db = cast(DB, entry.db)
            yield db
        finally:
            self._leave(entry)
            if entry.close_after_use:
                await self._close_after_last_use(entry)

    async def _close_after_last_use(self, entry: _PoolEntry[DB]) -> None:
        """See Pool._close_after_last_use."""
        failure: LeaseCleanupError | None = None
        async with cast(asyncio.Lock, entry.turn):
            if not self._last_use_ended(entry):
                return
            try:
                async with asyncio.timeout(self._settings.cleanup_seconds):
                    await self._adapter.close(cast(DB, entry.db))
            except Exception as error:
                failure = _use_close_failed(entry, error)
            else:
                entry.db, entry.opened = None, False
                self._remove_unused_entry(entry)
        if failure is not None:
            self._report_release_error(failure)

    async def _open_rule_database(self, entry: _PoolEntry[DB]) -> None:
        """See Pool._open_rule_database."""
        for attempt in range(2):
            status = await self._client.databases.get(entry.name)
            if not _is_rule_database(status):
                raise _not_rule_database(entry.name)
            if status.state == STATE_OFFLOADED:
                await self._client.databases.acquire(entry.name)
            try:
                db = await self._adapter.open(status.path)
            except BaseException:
                if attempt == 0 and not os.path.exists(status.path):
                    continue
                raise
            entry.db, entry.path, entry.opened = db, status.path, True
            rule = await self._database_rule(cast(str, status.backup_rule_id)) if self._rule_lookup_needed(status) else None
            self._set_rule_closing(entry, status, rule)
            return

    async def _database_rule(self, rule_id: str) -> models.BackupRule | None:
        """See Pool._database_rule."""
        rule = self._cached_rule(rule_id)
        if rule is None:
            rule = self._cache_rules(await self._client.rules.list(), rule_id)
        return rule

    @asynccontextmanager
    async def lease(
        self,
        name: str,
        *,
        path: PathSource | None = None,
        require_existing: bool = False,
        durability: Durability | None = None,
        offload: bool = False,
        lock_timeout: Duration | None = None,
        acquire_timeout: Duration | None = None,
        maintenance: bool = False,
    ) -> AsyncIterator[DB]:
        """See Pool.lease. A block that is cancelled, or that catches its cancellation and returns, keeps the
        lease without offload, and the cancellation propagates."""
        lease_settings = _lease_settings(path, require_existing, durability, offload, lock_timeout, acquire_timeout, maintenance)
        entry = self._join(name)
        try:
            self._start_idle_task()
            async with cast(asyncio.Lock, entry.turn):
                if entry.opened and not entry.leased:
                    raise invalid_argument(f"{name} is a backup rule's database in this pool; use use() for it")
                if entry.leased:
                    await self._check_lease(entry)
                entry.lease_settings = lease_settings
                if not entry.leased:
                    await self._acquire_lease(entry, lease_settings)
                elif not entry.opened:
                    await self._reopen_lease(entry)
                # The call counts as failed until its work and its durability wait both finished.
                entry.last_call_failed = True
                yield cast(DB, entry.db)
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    # The block caught its cancellation and returned: it still counts as cancelled.
                    raise asyncio.CancelledError()
                if lease_settings.durability is not None:
                    await self._wait_durable(name, lease_settings.durability)
                entry.last_call_failed = False
        finally:
            self._leave(entry)

    async def _check_lease(self, entry: _PoolEntry[DB]) -> None:
        """See Pool._check_lease."""
        try:
            status: models.DatabaseStatus | None = await self._client.databases.get(entry.name)
        except LitebackupError as error:
            if error.code != ErrorCode.NOT_FOUND:
                raise
            status = None
        if _lock_lost(status):
            await self._drop_lost_lease(entry)
            raise _lease_lost(entry.name)

    async def _reopen_lease(self, entry: _PoolEntry[DB]) -> None:
        """See Pool._reopen_lease."""
        try:
            entry.db = await self._adapter.open(entry.path)
        except BaseException:
            entry.last_call_failed = True
            raise
        entry.opened = True

    async def _acquire_lease(self, entry: _PoolEntry[DB], lease_settings: _LeaseSettings) -> None:
        """See Pool._acquire_lease."""
        if lease_settings.path and not lease_settings.require_existing:
            await asyncio.to_thread(_create_empty_file, lease_settings.path)
        acquired = await self._client.databases.acquire(
            entry.name,
            path=lease_settings.path,
            maintenance=lease_settings.maintenance,
            machine_label=self._settings.machine_label,
            timeout=lease_settings.acquire_timeout,
            lock_timeout=lease_settings.lock_timeout,
        )
        path = self._acquired_path(acquired, lease_settings, entry.name)
        try:
            db = await self._adapter.open(path)
        except BaseException as error:
            entry.path, entry.leased, entry.last_call_failed = path, True, True
            try:
                await self._release_lease(entry)
            except LeaseCleanupError as release_error:
                _raise_together([error, release_error], f"leasing {entry.name}")
            raise
        entry.db, entry.path, entry.opened, entry.leased = db, path, True, True

    async def _wait_durable(self, name: str, durability: Durability) -> None:
        """See Pool._wait_durable."""
        status = await self._client.databases.sync(name)
        if durability != DURABILITY_CLOUD:
            return
        target = status.synced_server.version
        while status.synced_cloud.version < target:
            await asyncio.sleep(CLOUD_DURABILITY_POLL_SECONDS)
            status = await self._client.databases.get(name)

    async def release(self, name: str) -> None:
        """See Pool.release."""
        with self._mutex:
            entry = self._entries.get(name)
        if entry is None:
            raise invalid_argument(f"{name} is not leased in this pool")
        async with cast(asyncio.Lock, entry.turn):
            if not entry.leased:
                raise invalid_argument(f"{name} is not leased in this pool")
            await self._release_lease(entry)

    async def _release_lease(self, entry: _PoolEntry[DB]) -> None:
        """See Pool._release_lease; a close that outlasts the cleanup timeout fails as a close."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.cleanup_seconds
        if entry.opened:
            try:
                async with asyncio.timeout_at(deadline):
                    await self._adapter.close(cast(DB, entry.db))
            except Exception as error:
                raise _close_failed(entry, error) from error
            entry.db, entry.opened = None, False
        durability, offload = self._release_durability_and_offload(entry)
        timeout = max(deadline - loop.time(), _MINIMUM_RELEASE_SECONDS)
        try:
            await self._client.databases.release(entry.name, durability=durability, offload=offload, timeout=timeout)
        except Exception as error:
            raise _release_failed(entry, durability, error) from error
        entry.leased = False
        self._remove_unused_entry(entry)

    async def _drop_lost_lease(self, entry: _PoolEntry[DB]) -> None:
        """See Pool._drop_lost_lease."""
        if entry.opened:
            try:
                async with asyncio.timeout(self._settings.cleanup_seconds):
                    await self._adapter.close(cast(DB, entry.db))
            except Exception as error:
                logger.warning("closing %s after its write lock was lost failed: %s", entry.name, error)
            entry.db, entry.opened = None, False
        entry.leased = False

    async def close(self) -> None:
        """See Pool.close. The cleanup is shielded from cancellation: a cancelled close keeps releasing in the
        background, and awaiting close again waits for it and raises its failures."""
        if self._close_task is None:
            self._start_closing()
            self._close_task = asyncio.get_running_loop().create_task(self._close_pool(), name="litebackup-pool-close")
        elif self._close_task.done() and self._close_reported:
            return
        await asyncio.shield(self._close_task)
        self._close_reported = True

    async def _close_pool(self) -> None:
        if not self._no_calls_reached:
            self._no_calls = asyncio.Event()
            if not self._no_calls_reached:
                await self._no_calls.wait()
        if self._idle_task is not None and self._stopped is not None:
            self._stopped.set()
            await self._idle_task
        failures: list[BaseException] = []
        for entry in self._all_entries():
            async with cast(asyncio.Lock, entry.turn):
                if entry.leased:
                    try:
                        await self._release_lease(entry)
                    except LeaseCleanupError as error:
                        failures.append(error)
                elif entry.opened:
                    try:
                        async with asyncio.timeout(self._settings.cleanup_seconds):
                            await self._adapter.close(cast(DB, entry.db))
                    except Exception as error:
                        failures.append(error)
                    else:
                        entry.db, entry.opened = None, False
                        self._remove_unused_entry(entry)
        _raise_together(failures, "closing the pool")

    async def _close_idle_databases(self) -> None:
        """See Pool._close_idle_databases; the pool's close stops it between checks."""
        stopped = cast(asyncio.Event, self._stopped)
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), POOL_CHECK_SECONDS)
                return
            except TimeoutError:
                pass
            now = time.monotonic()
            for entry in self._idle_entries():
                failure = await self._close_idle_entry(entry, now)
                if failure is not None:
                    self._report_release_error(failure)

    async def _close_idle_entry(self, entry: _PoolEntry[DB], now: float) -> LeaseCleanupError | None:
        """See Pool._close_idle_entry."""
        turn = cast(asyncio.Lock, entry.turn)
        if turn.locked():
            return None
        async with turn:
            idle_step = self._idle_step(entry, now)
            if idle_step == IDLE_RELEASE_STEP:
                try:
                    await self._release_lease(entry)
                except LeaseCleanupError as error:
                    return error
            elif idle_step == IDLE_CLOSE_STEP:
                try:
                    async with asyncio.timeout(self._settings.cleanup_seconds):
                        await self._adapter.close(cast(DB, entry.db))
                except Exception as error:
                    logger.warning("closing the idle database %s failed: %s", entry.name, error)
                    return None
                entry.db, entry.opened = None, False
                self._remove_unused_entry(entry)
            return None

    def _report_release_error(self, error: LeaseCleanupError) -> None:
        """Hands a failed background release to on_release_error in its own event loop callback, outside the
        idle task close() waits for; logs it without a callback."""
        callback = self._settings.on_release_error
        if callback is None:
            logger.warning("the pool could not release %s: %s", error.name, error)
            return
        asyncio.get_running_loop().call_soon(callback, error)


def new_pool(
    client: Litebackup,
    adapter: Adapter[DB],
    idle_close: Duration | None,
    idle_release: Duration | None,
    cleanup_timeout: Duration | None,
    machine_label: str | None,
    on_release_error: Callable[[LeaseCleanupError], None] | None,
) -> Pool[DB]:
    settings = _pool_settings(adapter, idle_close, idle_release, cleanup_timeout, machine_label, on_release_error)
    return Pool(client, adapter, settings)


def new_async_pool(
    client: AsyncLitebackup,
    adapter: AsyncAdapter[DB],
    idle_close: Duration | None,
    idle_release: Duration | None,
    cleanup_timeout: Duration | None,
    machine_label: str | None,
    on_release_error: Callable[[LeaseCleanupError], None] | None,
) -> AsyncPool[DB]:
    settings = _pool_settings(adapter, idle_close, idle_release, cleanup_timeout, machine_label, on_release_error)
    return AsyncPool(client, adapter, settings)


__all__ = [
    "DEFAULT_CLEANUP_TIMEOUT",
    "DEFAULT_IDLE_RELEASE",
    "DEFAULT_OFFLOAD_IDLE_CLOSE",
    "Adapter",
    "AsyncAdapter",
    "AsyncPool",
    "Pool",
    "new_async_pool",
    "new_pool",
]
