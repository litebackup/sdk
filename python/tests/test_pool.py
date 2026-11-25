"""Pool behavior the lifecycle fixtures do not cover: kept locks, lost locks, idle close and release, and closing.

They mirror the Go SDK's pool_test.go.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import threading
import time
import typing
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from litebackup import AsyncLitebackup, ErrorCode, LeaseCleanupError, Litebackup, LitebackupError, _pool, _wire
from litebackup._generated import models
from litebackup._pool import _create_empty_file

from .contract_daemon import ContractDaemon, Received, Reply

LEASED_NAME = "agents/7.sqlite"
RULE_NAME = "tenants/42.sqlite"


def acquired_status(path: str) -> dict[str, Any]:
    """The status of a database this machine acquired."""
    return {
        "name": LEASED_NAME,
        "path": path,
        "role": "origin",
        "state": "synced",
        "acquired": True,
        "failover": False,
        "local_changed": False,
        "synced_server": {"version": 3},
        "synced_cloud": {"version": 3},
        "journal_mode": "wal",
        "page_count": 2,
        "sync_count": 3,
        "hint_misses": 0,
    }


def rule_status(path: str, database_state: str) -> dict[str, Any]:
    """The status of a backup rule's database."""
    return {**acquired_status(path), "name": RULE_NAME, "acquired": False, "state": database_state, "backup_rule_id": "rule-1"}


class LeaseDaemon:
    """Answers a pool's requests for one database: saves each database request as a step and keeps the
    database's status for status reads."""

    def __init__(self, status: dict[str, Any], rules: list[dict[str, Any]] | None = None) -> None:
        self.steps: list[str] = []
        self.status = status
        self.rules = rules or []
        self.failing: set[str] = set()
        """Steps answered with unavailable."""
        self.mutex = threading.Lock()
        self.daemon = ContractDaemon(self.answer)

    def answer(self, received: Received) -> Reply:
        if received.path == "/v1/backup-rules/list":
            self.steps.append("rules")
            return self.encode({"rules": self.rules})
        step = received.path.removeprefix("/v1/databases/")
        self.steps.append(step)
        if step in self.failing:
            return 503, "application/json", json.dumps({"code": "unavailable", "message": f"{step} failed"}).encode()
        with self.mutex:
            if step == "acquire":
                self.status["state"] = "syncing"
                return self.encode({"database": self.status, "decision": "downloaded"})
            if step == "release":
                return self.encode({"database": self.status, "checkpointed": False, "vacuumed": False})
            return self.encode(self.status)

    @staticmethod
    def encode(answer: object) -> Reply:
        return 200, "application/json", json.dumps(answer).encode()

    def request_bodies(self, path: str) -> list[Any]:
        return [received.json() for received in self.daemon.received if received.path == path]


class StepAdapter:
    def __init__(self, steps: list[str], close_seconds: float = 0, failing_closes: int = 0) -> None:
        self.steps = steps
        self.close_seconds = close_seconds
        self.failing_closes = failing_closes
        """How many closes fail before one succeeds."""

    def open(self, path: str) -> str:
        self.steps.append("open")
        return "db"

    def close(self, db: str) -> None:
        time.sleep(self.close_seconds)
        self.steps.append("close")
        if self.failing_closes:
            self.failing_closes -= 1
            raise RuntimeError("a connection is still open")


class AsyncStepAdapter:
    def __init__(self, steps: list[str], close_seconds: float = 0) -> None:
        self.steps = steps
        self.close_seconds = close_seconds

    async def open(self, path: str) -> str:
        self.steps.append("open")
        return "db"

    async def close(self, db: str) -> None:
        await asyncio.sleep(self.close_seconds)
        self.steps.append("close")


@pytest.fixture
def lease_daemons() -> Iterator[Callable[..., LeaseDaemon]]:
    started: list[LeaseDaemon] = []

    def start(status: dict[str, Any], rules: list[dict[str, Any]] | None = None) -> LeaseDaemon:
        daemon = LeaseDaemon(status, rules)
        started.append(daemon)
        return daemon

    yield start
    for daemon in started:
        daemon.daemon.check_sdk_version()
        daemon.daemon.close()


def wait_until(condition: Callable[[], bool], seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


async def wait_until_async(condition: Callable[[], bool], seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def test_lease_keeps_lock_between_calls(tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    """Only the first lease acquires and opens, a lease with durability syncs without releasing, and close
    closes and releases once."""
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    with Litebackup(socket_path=daemon.daemon.socket_path) as client, client.databases.pool(StepAdapter(daemon.steps)) as pool:
        for durability in (None, "server", None):
            with pool.lease(LEASED_NAME, path=path, durability=durability) as db:
                assert db == "db"
                daemon.steps.append("work")
    assert daemon.steps == ["acquire", "open", "work", "status", "work", "sync", "status", "work", "close", "release"]
    assert os.path.exists(path)


async def test_async_lease_keeps_lock_between_calls(tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    async with (
        AsyncLitebackup(socket_path=daemon.daemon.socket_path) as client,
        client.databases.pool(AsyncStepAdapter(daemon.steps)) as pool,
    ):
        for durability in (None, "server", None):
            async with pool.lease(LEASED_NAME, path=path, durability=durability) as db:
                assert db == "db"
                daemon.steps.append("work")
    assert daemon.steps == ["acquire", "open", "work", "status", "work", "sync", "status", "work", "close", "release"]


def test_failed_block_keeps_the_lease_and_does_not_offload(tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    with Litebackup(socket_path=daemon.daemon.socket_path) as client:
        pool = client.databases.pool(StepAdapter(daemon.steps))
        with pytest.raises(RuntimeError), pool.lease(LEASED_NAME, path=path, offload=True):
            raise RuntimeError("work failed")
        assert pool._entries[LEASED_NAME].leased
        pool.close()
    assert daemon.steps == ["acquire", "open", "close", "release"]
    assert daemon.request_bodies("/v1/databases/release") == [{"name": LEASED_NAME, "durability": "server", "timeout_millis": 600000}]


def test_release_ends_the_lease_and_denies_one_the_pool_does_not_lease(tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    with Litebackup(socket_path=daemon.daemon.socket_path) as client, client.databases.pool(StepAdapter(daemon.steps)) as pool:
        with pool.lease(LEASED_NAME, path=path, durability="cloud", offload=True):
            pass
        pool.release(LEASED_NAME)
        assert daemon.steps == ["acquire", "open", "sync", "close", "release"]
        assert daemon.request_bodies("/v1/databases/release")[0]["offload"] is True
        with pytest.raises(LitebackupError) as raised:
            pool.release(LEASED_NAME)
        assert raised.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(LitebackupError) as closed:
        pool.lease(LEASED_NAME).__enter__()
    assert closed.value.code == ErrorCode.CLOSED


def test_idle_release_reports_a_failed_release(
    tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """idle_release releases an unused lease; a release that fails goes to on_release_error and the lease stays."""
    monkeypatch.setattr(_pool, "POOL_CHECK_SECONDS", 0.01)
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    daemon.failing.add("release")
    reported: list[LeaseCleanupError] = []
    with Litebackup(socket_path=daemon.daemon.socket_path) as client:
        pool = client.databases.pool(StepAdapter(daemon.steps), idle_release=0.05, on_release_error=reported.append)
        with pool.lease(LEASED_NAME, path=path):
            pass
        wait_until(lambda: bool(reported))
        assert reported[0].step == "release" and reported[0].name == LEASED_NAME
        assert pool._entries[LEASED_NAME].leased
        daemon.failing.clear()
        pool.close()
    assert daemon.steps[-1] == "release" and daemon.steps.count("close") == 1


def test_lease_cleanup_timeout_is_shared(tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    """The release the pool's close sends gets what closing left of the cleanup timeout."""
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    with (
        Litebackup(socket_path=daemon.daemon.socket_path) as client,
        client.databases.pool(StepAdapter(daemon.steps, close_seconds=0.5), cleanup_timeout=2) as pool,
        pool.lease(LEASED_NAME, path=path),
    ):
        pass
    (release,) = daemon.request_bodies("/v1/databases/release")
    assert 1000 <= release["timeout_millis"] <= 1600


async def test_async_lease_cleanup_timeout_is_shared(tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    async with (
        AsyncLitebackup(socket_path=daemon.daemon.socket_path) as client,
        client.databases.pool(AsyncStepAdapter(daemon.steps, close_seconds=0.5), cleanup_timeout=2) as pool,
        pool.lease(LEASED_NAME, path=path),
    ):
        pass
    (release,) = daemon.request_bodies("/v1/databases/release")
    assert 1000 <= release["timeout_millis"] <= 1600


def test_use_brings_back_and_keeps_open(tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    """An offloaded rule database used from several threads is brought back once and opened once; lease is
    refused for it, and use for an acquired database."""
    path = str(tmp_path / "42.sqlite")
    daemon = lease_daemons(rule_status(path, "offloaded"))
    with Litebackup(socket_path=daemon.daemon.socket_path) as client, client.databases.pool(StepAdapter(daemon.steps)) as pool:
        calls: list[str] = []
        failures: list[BaseException] = []

        def use_database() -> None:
            try:
                with pool.use(RULE_NAME) as db:
                    calls.append(db)
            except BaseException as error:
                failures.append(error)

        threads = [threading.Thread(target=use_database) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not failures and calls == ["db"] * 4
        assert daemon.steps.count("open") == 1 and daemon.steps.count("acquire") == 1
        with pytest.raises(LitebackupError) as leased, pool.lease(RULE_NAME):
            pass
        assert leased.value.code == ErrorCode.INVALID_ARGUMENT
        with daemon.mutex:
            daemon.status.update(name=LEASED_NAME, acquired=True)
            del daemon.status["backup_rule_id"]
        with pytest.raises(LitebackupError) as used, pool.use(LEASED_NAME):
            pass
        assert used.value.code == ErrorCode.INVALID_ARGUMENT


async def test_async_use_brings_back_and_keeps_open(tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    path = str(tmp_path / "42.sqlite")
    daemon = lease_daemons(rule_status(path, "offloaded"))
    async with (
        AsyncLitebackup(socket_path=daemon.daemon.socket_path) as client,
        client.databases.pool(AsyncStepAdapter(daemon.steps)) as pool,
    ):

        async def use_database() -> str:
            async with pool.use(RULE_NAME) as db:
                await asyncio.sleep(0.01)
                return db

        assert await asyncio.gather(*(use_database() for _ in range(4))) == ["db"] * 4
        assert daemon.steps.count("open") == 1 and daemon.steps.count("acquire") == 1
        with pytest.raises(LitebackupError) as leased:
            async with pool.lease(RULE_NAME):
                pass
        assert leased.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    ("maximum_local_databases", "idle_close", "closed"),
    [(0, None, False), (0, 0.05, True), (5, None, True)],
)
def test_use_closes_idle_only_when_rule_offloads(
    maximum_local_databases: int,
    idle_close: float | None,
    closed: bool,
    tmp_path: Path,
    lease_daemons: Callable[..., LeaseDaemon],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule database without a local limit stays open while idle; idle_close, or a rule that offloads,
    closes it."""
    monkeypatch.setattr(_pool, "POOL_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(_pool, "DEFAULT_OFFLOAD_IDLE_CLOSE", 0.05)
    path = str(tmp_path / "42.sqlite")
    Path(path).touch()
    rule = {"id": "rule-1", "path": path, "name": RULE_NAME, "prefix": "", "watch": "inotify", "failover": False}
    rule["maximum_local_databases"] = maximum_local_databases
    daemon = lease_daemons(rule_status(path, "synced"), [rule])
    with (
        Litebackup(socket_path=daemon.daemon.socket_path) as client,
        client.databases.pool(StepAdapter(daemon.steps), idle_close=idle_close) as pool,
    ):
        with pool.use(RULE_NAME):
            pass
        time.sleep(0.3)
        assert ("close" in daemon.steps) == closed, daemon.steps


async def test_async_use_closes_idle_when_rule_offloads(
    tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_pool, "POOL_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(_pool, "DEFAULT_OFFLOAD_IDLE_CLOSE", 0.05)
    path = str(tmp_path / "42.sqlite")
    rule = {"id": "rule-1", "path": path, "name": RULE_NAME, "prefix": "", "watch": "inotify", "maximum_local_databases": 5}
    daemon = lease_daemons(rule_status(path, "synced"), [rule])
    async with (
        AsyncLitebackup(socket_path=daemon.daemon.socket_path) as client,
        client.databases.pool(AsyncStepAdapter(daemon.steps)) as pool,
    ):
        async with pool.use(RULE_NAME):
            pass
        await wait_until_async(lambda: "close" in daemon.steps)
        assert RULE_NAME not in pool._entries


def test_pool_denies_bad_settings() -> None:
    with Litebackup(socket_path="/nonexistent/client.sock") as client:
        assert client.databases.pool(StepAdapter([]))._settings.idle_release == _pool.DEFAULT_IDLE_RELEASE == 60
        assert client.databases.pool(StepAdapter([]), idle_release=0)._settings.idle_release is None
        for options in ({"cleanup_timeout": 0}, {"idle_close": -1}, {"idle_release": -1}):
            with pytest.raises(LitebackupError) as raised:
                client.databases.pool(StepAdapter([]), **options)  # type: ignore[arg-type]
            assert raised.value.code == ErrorCode.INVALID_ARGUMENT, options
        with client.databases.pool(StepAdapter([])) as pool:
            for lease_options in ({"durability": "none"}, {"lock_timeout": -1}, {"acquire_timeout": -1}):
                with pytest.raises(LitebackupError) as denied, pool.lease(LEASED_NAME, **lease_options):
                    pass
                assert denied.value.code == ErrorCode.INVALID_ARGUMENT, lease_options
            with pytest.raises(LitebackupError) as unnamed, pool.use(""):
                pass
            assert unnamed.value.code == ErrorCode.INVALID_ARGUMENT


def test_create_empty_file_denies_a_path_whose_directory_is_a_file(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("")
    for to in (tmp_path / "file" / "app.db", tmp_path / "file" / "sub" / "app.db"):
        with pytest.raises(LitebackupError) as raised:
            _create_empty_file(to)
        assert raised.value.code == "invalid_argument", to
    existing = tmp_path / "existing.db"
    existing.write_text("unreleased_pool")
    _create_empty_file(existing)
    assert existing.read_text() == "unreleased_pool"


@pytest.mark.parametrize("lost_status", [{"acquired": False}, {"state": "conflicted"}])
def test_lease_on_a_lost_lock_raises_lock_lost(
    lost_status: dict[str, Any], tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]
) -> None:
    """A lease on a database this machine no longer holds closes the handle, drops the lease, and raises lock_lost
    without entering the block; the next lease acquires again."""
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    with Litebackup(socket_path=daemon.daemon.socket_path) as client, client.databases.pool(StepAdapter(daemon.steps)) as pool:
        with pool.lease(LEASED_NAME, path=path):
            pass
        with daemon.mutex:
            daemon.status.update(lost_status)
        entered = False
        with pytest.raises(LitebackupError) as raised, pool.lease(LEASED_NAME, path=path):
            entered = True
        assert raised.value.code == ErrorCode.LOCK_LOST and not entered
        assert not pool._entries.get(LEASED_NAME) or not pool._entries[LEASED_NAME].leased
        with daemon.mutex:
            daemon.status.update(acquired_status(path))
        with pool.lease(LEASED_NAME, path=path):
            pass
        assert daemon.steps == ["acquire", "open", "status", "close", "acquire", "open"]


async def test_async_lease_on_a_lost_lock_raises_lock_lost(tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    async with (
        AsyncLitebackup(socket_path=daemon.daemon.socket_path) as client,
        client.databases.pool(AsyncStepAdapter(daemon.steps)) as pool,
    ):
        async with pool.lease(LEASED_NAME, path=path):
            pass
        with daemon.mutex:
            daemon.status["state"] = "conflicted"
        with pytest.raises(LitebackupError) as raised:
            async with pool.lease(LEASED_NAME, path=path):
                raise AssertionError("the block ran on a lost lock")
        assert raised.value.code == ErrorCode.LOCK_LOST
        with daemon.mutex:
            daemon.status.update(acquired_status(path))
        async with pool.lease(LEASED_NAME, path=path):
            pass
        assert daemon.steps == ["acquire", "open", "status", "close", "acquire", "open"]


@pytest.mark.parametrize("client_mode", ["sync", "async"])
async def test_idle_release_releases_an_unused_lease_with_its_durability(
    client_mode: str, tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lease no block used for idle_release is closed and released with its durability; idle_release=0 keeps it."""
    monkeypatch.setattr(_pool, "POOL_CHECK_SECONDS", 0.01)
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    if client_mode == "sync":
        with Litebackup(socket_path=daemon.daemon.socket_path) as client:
            pool = client.databases.pool(StepAdapter(daemon.steps), idle_release=0.05)
            with pool.lease(LEASED_NAME, path=path, durability="cloud"):
                pass
            await asyncio.to_thread(wait_until, lambda: "release" in daemon.steps)
            pool.close()
            unreleased_pool = client.databases.pool(StepAdapter(daemon.steps), idle_release=0)
            with unreleased_pool.lease(LEASED_NAME, path=path):
                pass
            await asyncio.sleep(0.2)
            assert daemon.steps.count("release") == 1
            unreleased_pool.close()
    else:
        async with (
            AsyncLitebackup(socket_path=daemon.daemon.socket_path) as async_client,
            async_client.databases.pool(AsyncStepAdapter(daemon.steps), idle_release=0.05) as async_pool,
        ):
            async with async_pool.lease(LEASED_NAME, path=path, durability="cloud"):
                pass
            await wait_until_async(lambda: "release" in daemon.steps)
    assert daemon.steps[:5] == ["acquire", "open", "sync", "close", "release"]
    assert daemon.request_bodies("/v1/databases/release")[0]["durability"] == "cloud"


@pytest.mark.parametrize("call", ["use", "lease"])
def test_close_awaits_active_blocks(call: str, tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    """close refuses new calls at once, but closes a handle only after the block using it returned."""
    path = str(tmp_path / "42.sqlite")
    Path(path).touch()
    daemon = lease_daemons(rule_status(path, "synced") if call == "use" else acquired_status(path))
    block_entered = threading.Event()
    block_return_allowed = threading.Event()
    with Litebackup(socket_path=daemon.daemon.socket_path) as client:
        pool = client.databases.pool(StepAdapter(daemon.steps), idle_close=60)

        def block() -> None:
            blocks = pool.use(RULE_NAME) if call == "use" else pool.lease(LEASED_NAME, path=path)
            with blocks:
                block_entered.set()
                block_return_allowed.wait(10)
                daemon.steps.append("block returned")

        blocking = threading.Thread(target=block)
        blocking.start()
        block_entered.wait(10)
        closing = threading.Thread(target=pool.close)
        closing.start()
        wait_until(lambda: pool._closing)
        with pytest.raises(LitebackupError) as denied, pool.lease("agents/8.sqlite"):
            pass
        assert denied.value.code == ErrorCode.CLOSED
        time.sleep(0.1)
        assert "close" not in daemon.steps
        block_return_allowed.set()
        blocking.join()
        closing.join()
    assert daemon.steps.index("block returned") < daemon.steps.index("close")


async def test_async_close_awaits_active_blocks(tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    block_return_allowed = asyncio.Event()
    async with AsyncLitebackup(socket_path=daemon.daemon.socket_path) as client:
        pool = client.databases.pool(AsyncStepAdapter(daemon.steps))
        block_entered = asyncio.Event()

        async def block() -> None:
            async with pool.lease(LEASED_NAME, path=path):
                block_entered.set()
                await block_return_allowed.wait()
                daemon.steps.append("block returned")

        blocking = asyncio.create_task(block())
        await block_entered.wait()
        closing = asyncio.create_task(pool.close())
        await asyncio.sleep(0.1)
        assert "close" not in daemon.steps
        block_return_allowed.set()
        await blocking
        await closing
    assert daemon.steps[-3:] == ["block returned", "close", "release"]


@pytest.mark.parametrize("client_mode", ["sync", "async"])
async def test_lease_reopens_the_handle_after_a_failed_release(
    client_mode: str, tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]
) -> None:
    """A release that failed after closing the handle keeps the lease; the next lease opens the handle again."""
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    if client_mode == "sync":
        with Litebackup(socket_path=daemon.daemon.socket_path) as client, client.databases.pool(StepAdapter(daemon.steps)) as pool:
            with pool.lease(LEASED_NAME, path=path):
                pass
            daemon.failing.add("release")
            with pytest.raises(LeaseCleanupError) as raised:
                pool.release(LEASED_NAME)
            daemon.failing.clear()
            with pool.lease(LEASED_NAME, path=path) as db:
                assert db == "db"
    else:
        async with (
            AsyncLitebackup(socket_path=daemon.daemon.socket_path) as async_client,
            async_client.databases.pool(AsyncStepAdapter(daemon.steps)) as async_pool,
        ):
            async with async_pool.lease(LEASED_NAME, path=path):
                pass
            daemon.failing.add("release")
            with pytest.raises(LeaseCleanupError) as raised:
                await async_pool.release(LEASED_NAME)
            daemon.failing.clear()
            async with async_pool.lease(LEASED_NAME, path=path) as db:
                assert db == "db"
    assert raised.value.step == "release"
    assert daemon.steps == ["acquire", "open", "close", "release", "status", "open", "close", "release"]


def test_failed_idle_close_keeps_the_handle(
    tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule database whose idle close failed keeps its entry and handle, so the next check closes it."""
    monkeypatch.setattr(_pool, "POOL_CHECK_SECONDS", 0.01)
    path = str(tmp_path / "42.sqlite")
    Path(path).touch()
    daemon = lease_daemons(rule_status(path, "synced"))
    with Litebackup(socket_path=daemon.daemon.socket_path) as client:
        adapter = StepAdapter(daemon.steps, failing_closes=1)
        with client.databases.pool(adapter, idle_close=0.05) as pool:
            with pool.use(RULE_NAME):
                pass
            wait_until(lambda: daemon.steps.count("close") == 2)
            wait_until(lambda: RULE_NAME not in pool._entries)
    assert daemon.steps.count("open") == 1


def test_on_release_error_calls_the_pool(
    tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """on_release_error runs outside the entry's turn and the idle thread, so it can call release() and close()."""
    monkeypatch.setattr(_pool, "POOL_CHECK_SECONDS", 0.01)
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    daemon.failing.add("release")
    released = threading.Event()
    with Litebackup(socket_path=daemon.daemon.socket_path) as client:
        pool = client.databases.pool(StepAdapter(daemon.steps), idle_release=0.05)

        def release_again(error: LeaseCleanupError) -> None:
            daemon.failing.clear()
            pool.release(error.name)
            released.set()

        pool._settings = dataclasses.replace(pool._settings, on_release_error=release_again)
        with pool.lease(LEASED_NAME, path=path):
            pass
        assert released.wait(10), "the callback never released the lease"
        pool.close()
    assert daemon.steps[-2:] == ["release", "release"]


async def test_cancelled_async_close_still_releases(tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon]) -> None:
    """A close cancelled during cleanup keeps releasing; awaiting close again waits for it."""
    path = str(tmp_path / "7.sqlite")
    daemon = lease_daemons(acquired_status(path))
    async with AsyncLitebackup(socket_path=daemon.daemon.socket_path) as client:
        pool = client.databases.pool(AsyncStepAdapter(daemon.steps, close_seconds=0.3))
        async with pool.lease(LEASED_NAME, path=path):
            pass
        closing = asyncio.create_task(pool.close())
        await asyncio.sleep(0.05)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert "release" not in daemon.steps
        await pool.close()
        assert daemon.steps[-2:] == ["close", "release"]
        await pool.close()


def test_wire_constants_match_the_generated_models() -> None:
    hints = typing.get_type_hints(models.DatabaseStatus)
    assert {_wire.STATE_OFFLOADED, _wire.STATE_CONFLICTED} <= set(typing.get_args(hints["state"]))
    assert typing.get_args(_wire.DatabaseState) == typing.get_args(hints["state"])
    assert typing.get_args(_wire.DatabaseRole) == typing.get_args(hints["role"])
    assert _wire.ROLE_ORIGIN in typing.get_args(hints["role"])
    assert {_wire.DURABILITY_SERVER, _wire.DURABILITY_CLOUD} == set(typing.get_args(models.Durability))


def journal_rule(path: str, offload_rollback_journal: bool) -> dict[str, Any]:
    return {
        "id": "rule-1",
        "path": path,
        "name": RULE_NAME,
        "prefix": "",
        "watch": "inotify",
        "maximum_local_databases": 5,
        "offload_rollback_journal": offload_rollback_journal,
        "failover": False,
    }


ROLLBACK_CASES = [("rollback", True, True), ("rollback", False, False), ("wal", True, False)]


@pytest.mark.parametrize(("journal_mode", "offload_rollback_journal", "closed_after_use"), ROLLBACK_CASES)
def test_use_closes_a_rollback_database_its_rule_offloads(
    journal_mode: str,
    offload_rollback_journal: bool,
    closed_after_use: bool,
    tmp_path: Path,
    lease_daemons: Callable[..., LeaseDaemon],
) -> None:
    """A rollback-journal database under a rule with offload_rollback_journal closes after each use and opens
    again on the next; WAL databases and rules without the flag stay open."""
    path = str(tmp_path / "42.sqlite")
    Path(path).touch()
    daemon = lease_daemons({**rule_status(path, "synced"), "journal_mode": journal_mode}, [journal_rule(path, offload_rollback_journal)])
    with Litebackup(socket_path=daemon.daemon.socket_path) as client, client.databases.pool(StepAdapter(daemon.steps)) as pool:
        for _ in range(2):
            with pool.use(RULE_NAME) as db:
                assert db == "db"
                assert "close" not in daemon.steps[-1:]
        assert (RULE_NAME not in pool._entries) == closed_after_use
        assert daemon.steps.count("rules") == 1
        if closed_after_use:
            assert daemon.steps == ["status", "open", "rules", "close", "status", "open", "close"]
        else:
            assert daemon.steps == ["status", "open", "rules"]


@pytest.mark.parametrize(("journal_mode", "offload_rollback_journal", "closed_after_use"), ROLLBACK_CASES)
async def test_async_use_closes_a_rollback_database_its_rule_offloads(
    journal_mode: str,
    offload_rollback_journal: bool,
    closed_after_use: bool,
    tmp_path: Path,
    lease_daemons: Callable[..., LeaseDaemon],
) -> None:
    path = str(tmp_path / "42.sqlite")
    daemon = lease_daemons({**rule_status(path, "synced"), "journal_mode": journal_mode}, [journal_rule(path, offload_rollback_journal)])
    async with (
        AsyncLitebackup(socket_path=daemon.daemon.socket_path) as client,
        client.databases.pool(AsyncStepAdapter(daemon.steps)) as pool,
    ):

        async def use_database() -> None:
            async with pool.use(RULE_NAME):
                await asyncio.sleep(0.01)

        # Concurrent uses share one handle, closed once the last of them exits.
        await asyncio.gather(use_database(), use_database())
        await use_database()
        if closed_after_use:
            assert daemon.steps == ["status", "open", "rules", "close", "status", "open", "close"]
            assert RULE_NAME not in pool._entries
        else:
            assert daemon.steps == ["status", "open", "rules"]


def test_failed_close_after_use_is_reported_and_retried(
    tmp_path: Path, lease_daemons: Callable[..., LeaseDaemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed close after the last use goes to on_release_error and keeps the entry; the idle check closes it."""
    monkeypatch.setattr(_pool, "POOL_CHECK_SECONDS", 0.01)
    path = str(tmp_path / "42.sqlite")
    Path(path).touch()
    daemon = lease_daemons({**rule_status(path, "synced"), "journal_mode": "rollback"}, [journal_rule(path, True)])
    reported: list[LeaseCleanupError] = []
    with Litebackup(socket_path=daemon.daemon.socket_path) as client:
        pool = client.databases.pool(StepAdapter(daemon.steps, failing_closes=1), on_release_error=reported.append)
        with pool.use(RULE_NAME):
            pass
        wait_until(lambda: bool(reported))
        assert reported[0].step == "close" and reported[0].details["acquired"] is False
        wait_until(lambda: daemon.steps.count("close") == 2)
        wait_until(lambda: RULE_NAME not in pool._entries)
        pool.close()
    assert daemon.steps.count("open") == 1
