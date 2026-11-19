"""Managed scopes: acquire → open → work → close → release.

A close that fails never releases: a connection may still be open, and another
machine must not write while this one still can. Failed or cancelled work still
closes and releases, without offload, and its error comes first.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, cast

from ._wire import Duration, PathSource, absolute_path, seconds
from .errors import ErrorCode, LitebackupError, invalid_argument

if TYPE_CHECKING:
    from .client import AsyncLitebackup, Litebackup

Connection = TypeVar("Connection")
Connection_co = TypeVar("Connection_co", covariant=True)
DEFAULT_CLEANUP_TIMEOUT = 600.0
"""Seconds that bound closing and releasing unless cleanup_timeout says otherwise."""

logger = logging.getLogger("litebackup")


class ScopeAdapter(Protocol[Connection]):
    """Opens and closes the connection a managed scope yields.

    close must roll back a transaction the work left unfinished, never commit it,
    and raise when a connection may still be open: the scope releases the database
    only after close succeeds. ``litebackup.integrations.sqlite3.adapter()`` is one.
    """

    def open(self, path: str) -> Connection: ...

    def close(self, connection: Connection) -> None: ...


class AsyncScopeAdapter(Protocol[Connection]):
    """The async form of ScopeAdapter, such as ``litebackup.integrations.aiosqlite.adapter()``."""

    async def open(self, path: str) -> Connection: ...

    async def close(self, connection: Connection) -> None: ...


@dataclass(frozen=True, slots=True)
class _Scope:
    name: str
    durability: Literal["server", "cloud"]
    offload: bool
    cleanup_seconds: float

    def release_failed(self, path: str, error: BaseException) -> LitebackupError:
        failure = LitebackupError(
            ErrorCode.SCOPE_CLEANUP_FAILED,
            f"releasing {self.name} failed: {error}. Its file at {path} is intact and this machine may still hold its write lock; "
            f"call databases.release({self.name!r}) again",
            details={"step": "release", "name": self.name, "path": path, "durability": self.durability},
        )
        failure.__cause__ = error
        return failure

    def close_failed(self, path: str, error: BaseException) -> LitebackupError:
        failure = LitebackupError(
            ErrorCode.SCOPE_CLEANUP_FAILED,
            f"closing {self.name} failed, so it was not released and stays acquired by this machine: {error}. "
            f"Close every connection to {path}, then call databases.release({self.name!r})",
            details={"step": "close", "name": self.name, "path": path, "acquired": True},
        )
        failure.__cause__ = error
        return failure


def _scope(name: str, durability: str | None, cleanup_timeout: Duration | None, offload: bool, adapter: object) -> _Scope:
    if durability not in ("server", "cloud"):
        raise invalid_argument("a managed database needs a durability of server or cloud")
    if adapter is None:
        raise invalid_argument("a managed database needs an adapter")
    cleanup_seconds = DEFAULT_CLEANUP_TIMEOUT if cleanup_timeout is None else seconds(cleanup_timeout, "the cleanup timeout")
    if not cleanup_seconds:
        raise invalid_argument("the cleanup timeout must be positive")
    return _Scope(name, cast(Literal["server", "cloud"], durability), offload, cleanup_seconds)


def _raise(errors: list[BaseException], name: str) -> None:
    """Raises one error alone, or several as a group, first one first."""
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup(f"the managed database {name} failed in {len(errors)} ways; the first error ended the work", errors)


@contextmanager
def managed(
    client: Litebackup,
    name: str,
    *,
    durability: Literal["server", "cloud"],
    adapter: ScopeAdapter[Connection],
    to: PathSource | None = None,
    offload: bool = False,
    cleanup_timeout: Duration | None = None,
    machine_label: str | None = None,
    maintenance: bool = False,
    on_lost: Literal["conflict", "block"] = "conflict",
    acquire_timeout: Duration | None = None,
) -> Iterator[Connection]:
    scope = _scope(name, durability, cleanup_timeout, offload, adapter)
    acquired = client.databases.acquire(
        name, to=to, maintenance=maintenance, machine_label=machine_label, on_lost=on_lost, timeout=acquire_timeout
    )
    path = acquired.database.path or (absolute_path(to) if to else "")

    def release(offload: bool) -> LitebackupError | None:
        try:
            client.databases.release(name, wait=scope.durability, offload=offload, timeout=scope.cleanup_seconds)
        except Exception as error:
            return scope.release_failed(path, error)
        return None

    try:
        connection = adapter.open(path)
    except BaseException as error:
        release_error = release(False)
        _raise([error] if release_error is None else [error, release_error], name)
        raise
    raised: list[BaseException] = []
    try:
        yield connection
    except BaseException as error:
        raised.append(error)
    try:
        adapter.close(connection)
    except Exception as error:
        raised.append(scope.close_failed(path, error))
        _raise(raised, name)
    release_error = release(not raised and scope.offload)
    if release_error is not None:
        raised.append(release_error)
    _raise(raised, name)


async def _until_done(cleanup: asyncio.Task[list[BaseException]]) -> tuple[list[BaseException], asyncio.CancelledError | None]:
    """Awaits the cleanup task through repeated shields; returns its errors and any cancellation that arrived meanwhile."""
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(cleanup), cancellation
        except asyncio.CancelledError as error:
            if cleanup.done():
                return cleanup.result(), cancellation or error
            cancellation = error


def _raise_after_cleanup(raised: list[BaseException], cancellation: asyncio.CancelledError | None, name: str) -> None:
    """A cancellation is re-raised as itself, with cleanup failures attached; anything else as _raise does."""
    if cancellation is None:
        cancellation = next((error for error in raised if isinstance(error, asyncio.CancelledError)), None)
    if cancellation is not None:
        failures = [error for error in raised if error is not cancellation and not isinstance(error, asyncio.CancelledError)]
        for failure in failures:
            logger.warning("the managed database %s was cancelled and its cleanup failed: %s", name, failure)
            cancellation.add_note(f"cleanup of {name} failed: {failure}")
        if failures:
            cancellation.__cause__ = failures[0]
        raise cancellation
    _raise(raised, name)


@asynccontextmanager
async def managed_async(
    client: AsyncLitebackup,
    name: str,
    *,
    durability: Literal["server", "cloud"],
    adapter: AsyncScopeAdapter[Connection],
    to: PathSource | None = None,
    offload: bool = False,
    cleanup_timeout: Duration | None = None,
    machine_label: str | None = None,
    maintenance: bool = False,
    on_lost: Literal["conflict", "block"] = "conflict",
    acquire_timeout: Duration | None = None,
) -> AsyncIterator[Connection]:
    scope = _scope(name, durability, cleanup_timeout, offload, adapter)
    acquired = await client.databases.acquire(
        name, to=to, maintenance=maintenance, machine_label=machine_label, on_lost=on_lost, timeout=acquire_timeout
    )
    path = acquired.database.path or (absolute_path(to) if to else "")

    async def release(offload: bool) -> list[BaseException]:
        try:
            await client.databases.release(name, wait=scope.durability, offload=offload, timeout=scope.cleanup_seconds)
        except Exception as error:
            return [scope.release_failed(path, error)]
        return []

    async def close_and_release(connection: Connection, work_failed: bool) -> list[BaseException]:
        try:
            async with asyncio.timeout(scope.cleanup_seconds):
                await adapter.close(connection)
        except Exception as error:
            return [scope.close_failed(path, error)]
        # The release call's own deadline is the cleanup timeout plus the grace period.
        return await release(not work_failed and scope.offload)

    try:
        connection = await adapter.open(path)
    except BaseException as error:
        cleanup_errors, cancellation = await _until_done(asyncio.ensure_future(release(False)))
        _raise_after_cleanup([error, *cleanup_errors], cancellation, name)
        raise
    raised: list[BaseException] = []
    try:
        yield connection
    except BaseException as error:
        raised.append(error)
    cleanup_errors, cancellation = await _until_done(asyncio.ensure_future(close_and_release(connection, bool(raised))))
    _raise_after_cleanup([*raised, *cleanup_errors], cancellation, name)
