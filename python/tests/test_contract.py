"""Runs the language-neutral fixtures in testdata/contract, which the Go and TypeScript SDKs run too.

Every fixture runs against both Litebackup and AsyncLitebackup.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from litebackup import (
    MINIMUM_DAEMON_VERSION,
    AcquireResult,
    AsyncLitebackup,
    DatabaseStatus,
    ErrorCode,
    LeaseCleanupError,
    Litebackup,
    LitebackupError,
    ReleaseResult,
    ShutdownResult,
)
from litebackup._binary import litebackup_binary
from litebackup._credential import credential_type
from litebackup._decode import decode
from litebackup._foreground import start_failure

from .contract_daemon import ContractDaemon, Received, Reply

CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "testdata" / "contract"
Client = Litebackup | AsyncLitebackup


def fixture_names(fixture_directory: str) -> list[str]:
    return sorted(path.stem for path in (CONTRACT_ROOT / fixture_directory).glob("*.json"))


def fixture_umask() -> int:
    """This process's umask, read by setting it and putting it back, apart from how the SDK reads it."""
    previous = os.umask(0)
    os.umask(previous)
    return previous


def read_fixture(fixture_directory: str, name: str, cwd: str = "/") -> dict[str, Any]:
    text = (CONTRACT_ROOT / fixture_directory / f"{name}.json").read_text()
    substituted = text.replace("{cwd}", json.dumps(cwd)[1:-1]).replace('"{umask}"', str(fixture_umask()))
    loaded: dict[str, Any] = json.loads(substituted)
    return loaded


def duration(text: str | None) -> float | None:
    """A fixture duration such as 1500ms or -1s, in seconds."""
    if text is None:
        return None
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)(ms|s|m|h)", text)
    assert match, text
    return float(match[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match[2]]


def path_argument(value: Any) -> Any:
    """{"$path": ...} becomes a relative pathlib.Path, which resolves against the working directory."""
    if isinstance(value, dict) and "$path" in value:
        return Path(value["$path"])
    return value


def operation_call(operation: str, args: dict[str, Any]) -> tuple[str, str, list[Any], dict[str, Any]]:
    """The group, method, and arguments of a fixture operation in the Python API."""
    keyword: dict[str, Any] = {}

    def pass_keywords(*names: str, convert: Callable[[Any], Any] = lambda value: value) -> None:
        for name in names:
            if name in args:
                keyword[name] = convert(args[name])

    group, _, method = operation.partition(".")
    positional: list[Any] = []
    if operation in ("session.status", "databases.page"):
        pass_keywords("after", "limit")
    elif operation == "session.shutdown":
        pass_keywords("durability")
        pass_keywords("timeout", convert=duration)
    elif operation in ("databases.get", "databases.sync", "databases.writer", "backups.stop", "replicas.stop"):
        positional = [args["name"]]
    elif operation == "databases.offload":
        positional = [args["name"]]
        pass_keywords("durability")
    elif operation == "databases.restore":
        positional = [args["name"]]
        keyword["path"] = path_argument(args.get("path"))
        pass_keywords("version")
        pass_keywords("time", convert=datetime.fromisoformat)
        pass_keywords("umask")
    elif operation == "databases.revert":
        positional = [args["name"]]
        pass_keywords("version")
        pass_keywords("time", convert=datetime.fromisoformat)
    elif operation == "databases.acquire":
        positional = [args["name"]]
        pass_keywords("path", convert=path_argument)
        pass_keywords("maintenance", "offload_on_release", "offload_durability", "machine_label", "discard_local")
        pass_keywords("timeout", convert=duration)
        pass_keywords("lock_timeout", convert=duration)
        pass_keywords("umask")
    elif operation == "databases.release":
        positional = [args["name"]]
        pass_keywords("durability", "offload")
        pass_keywords("timeout", convert=duration)
    elif operation == "backups.start":
        positional = [path_argument(args["source"])]
        pass_keywords("name", "prefix", "maximum_local_databases", "offload_durability", "offload_rollback_journal")
    elif operation == "rules.stop":
        positional = [args["id"]]
    elif operation == "replicas.start":
        positional = [args["name"]]
        pass_keywords("path", convert=path_argument)
        pass_keywords("write_protection")
        pass_keywords("timeout", convert=duration)
    elif operation == "replicas.start_prefix":
        positional = [args["prefix"]]
        keyword["directory"] = args.get("directory")
        pass_keywords("write_protection")
    elif operation == "replicas.stop_prefix":
        positional = [args["prefix"]]
        keyword["directory"] = args.get("directory")
    elif operation == "events.page":
        pass_keywords("after", "limit")
        pass_keywords("wait", convert=duration)
    elif operation not in ("rules.list", "replicas.list"):
        raise AssertionError(f"no Python mapping for {operation}")
    return group, method, positional, keyword


async def invoke(client: Client, operation: str, args: dict[str, Any]) -> Any:
    group, method, positional, keyword = operation_call(operation, args)
    answer = getattr(getattr(client, group), method)(*positional, **keyword)
    return await answer if inspect.isawaitable(answer) else answer


async def connect(client: Client) -> None:
    answer = client.connect()
    if inspect.isawaitable(answer):
        await answer


async def close(client: Client) -> None:
    answer = client.close()
    if inspect.isawaitable(answer):
        await answer


@pytest.fixture(params=["sync", "async"])
def client_mode(request: pytest.FixtureRequest) -> str:
    client_mode: str = request.param
    return client_mode


def make_client(client_mode: str, daemon: ContractDaemon) -> Client:
    return Litebackup(socket_path=daemon.socket_path) if client_mode == "sync" else AsyncLitebackup(socket_path=daemon.socket_path)


async def call_client(client_mode: str, client: Client, function: Callable[[Client], Any]) -> Any:
    """Runs a call; the sync client runs in a thread so the event loop stays free."""
    if client_mode == "sync":
        return await asyncio.to_thread(function, client)
    return await function(client)


@pytest.fixture
def daemons() -> Iterator[list[ContractDaemon]]:
    started: list[ContractDaemon] = []
    yield started
    for daemon in started:
        daemon.check_sdk_version()
        daemon.close()


def start_daemon(daemons: list[ContractDaemon], reply: Callable[[Received], Reply], client_version: str | None = "1.6.0") -> ContractDaemon:
    daemon = ContractDaemon(reply, client_version)
    daemons.append(daemon)
    return daemon


@pytest.mark.parametrize("name", fixture_names("requests"))
async def test_requests(name: str, client_mode: str, cwd: str, daemons: list[ContractDaemon]) -> None:
    fixture = read_fixture("requests", name, cwd)
    daemon = start_daemon(daemons, lambda _received: (200, "application/json", json.dumps(fixture.get("response", {})).encode()))
    client = make_client(client_mode, daemon)
    try:
        await connect(client)
        if "error" in fixture:
            with pytest.raises(LitebackupError) as raised:
                await invoke(client, fixture["operation"], fixture["args"])
            assert raised.value.code == fixture["error"]["code"]
            assert daemon.received == []
            return
        await invoke(client, fixture["operation"], fixture["args"])
        assert len(daemon.received) == 1
        sent = daemon.received[0]
        expected = fixture["request"]
        assert (sent.method, sent.path, sent.query) == (expected["method"], expected["path"], expected["query"])
        assert sent.json() == expected.get("body")
    finally:
        await close(client)


@pytest.mark.parametrize("name", fixture_names("errors"))
async def test_errors(name: str, client_mode: str, daemons: list[ContractDaemon]) -> None:
    fixture = read_fixture("errors", name)
    response = fixture["response"]
    daemon = start_daemon(daemons, lambda _received: (response["status"], response["content_type"], response["body"].encode()))
    client = make_client(client_mode, daemon)
    try:
        with pytest.raises(LitebackupError) as raised:
            await invoke(client, "rules.list", {})
        expected = fixture["expect"]
        error = raised.value
        assert (error.code, error.message, error.status, error.details) == (
            expected["code"],
            expected["message"],
            expected["status"],
            expected["details"],
        )
        assert str(error) == f"{expected['code']}: {expected['message']}"
    finally:
        await close(client)


@pytest.mark.parametrize("name", fixture_names("version"))
async def test_version(name: str, client_mode: str, daemons: list[ContractDaemon]) -> None:
    fixture = read_fixture("version", name)
    daemon = start_daemon(daemons, lambda _received: (500, "text/plain", b""), fixture["client_version"])
    client = make_client(client_mode, daemon)
    try:
        if fixture["accepted"]:
            await connect(client)
            return
        with pytest.raises(LitebackupError) as raised:
            await connect(client)
        assert raised.value.code == fixture["expect"]["code"]
        assert MINIMUM_DAEMON_VERSION in raised.value.message and "litebackup drain" in raised.value.message
    finally:
        await close(client)


def exchange_daemon(daemons: list[ContractDaemon], exchanges: list[dict[str, Any]]) -> ContractDaemon:
    position = iter(exchanges)

    def reply(received: Received) -> Reply:
        exchange = next(position, None)
        if exchange is None:
            return 500, "application/json", b'{"code":"unexpected","message":"no more exchanges"}'
        assert {"method": received.method, "path": received.path, "query": received.query} == exchange["request"]
        return exchange["response"]["status"], "application/json", json.dumps(exchange["response"]["body"]).encode()

    return start_daemon(daemons, reply)


def iteration(client: Client, fixture: dict[str, Any]) -> Any:
    args = fixture["args"]
    operation = fixture["operation"]
    if operation == "databases.all":
        return client.databases.all()
    if operation == "databases.versions":
        return client.databases.all_versions(args["name"], after=args.get("after", 0), limit=args.get("limit"))
    if operation == "events.watch":
        return client.events.watch(after=args.get("after", 0), wait=duration(args.get("wait")))
    raise AssertionError(f"no Python mapping for {operation}")


def with_spent_cleanup(body: Any, expected: Any) -> Any:
    """The release gets what closing left of the cleanup timeout, so a timeout_millis up to a
    second short of the fixture's full timeout reads as the fixture's value."""
    if not (isinstance(body, dict) and isinstance(expected, dict)):
        return body
    sent, want = body.get("timeout_millis"), expected.get("timeout_millis")
    if isinstance(sent, int) and isinstance(want, int) and 0 <= want - sent <= 1000:
        return {**body, "timeout_millis": want}
    return body


def fixture_value(value: Any) -> Any:
    """An iterated field as a fixture writes it: times as RFC 3339 in UTC."""
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value


PAGE_FIXTURES = [(directory, name) for directory in ("pagination", "events") for name in fixture_names(directory)]


@pytest.mark.parametrize("directory_and_name", PAGE_FIXTURES)
async def test_pagination_and_events(directory_and_name: tuple[str, str], client_mode: str, daemons: list[ContractDaemon]) -> None:
    fixture_directory, name = directory_and_name
    fixture = read_fixture(fixture_directory, name)
    if "python" in fixture.get("skip", {}):
        pytest.skip(fixture["skip"]["python"])
    daemon = exchange_daemon(daemons, fixture["exchanges"])
    client = make_client(client_mode, daemon)
    item_limit = fixture.get("item_limit")
    key = fixture["item_key"]
    items: list[Any] = []
    ended: BaseException | None = None

    def list_items_sync(sync_client: Client) -> None:
        nonlocal ended
        try:
            for item in iteration(sync_client, fixture):
                items.append(fixture_value(getattr(item, key)))
                if item_limit is not None and len(items) >= item_limit:
                    break
        except LitebackupError as error:
            ended = error

    async def list_items_async(async_client: Client) -> None:
        nonlocal ended
        try:
            async for item in iteration(async_client, fixture):
                items.append(fixture_value(getattr(item, key)))
                if item_limit is not None and len(items) >= item_limit:
                    break
        except LitebackupError as error:
            ended = error

    try:
        await call_client(client_mode, client, list_items_sync if client_mode == "sync" else list_items_async)
        assert items == fixture["expect"]["items"]
        if "error" in fixture["expect"]:
            assert isinstance(ended, LitebackupError) and ended.code == fixture["expect"]["error"]["code"]
        elif ended is not None:
            raise ended
        assert len(daemon.received) == len(fixture["exchanges"])
    finally:
        await close(client)


class StepAdapter:
    """Saves its steps and fails open or close on demand."""

    def __init__(self, steps: list[str], failing: set[str]) -> None:
        self.steps = steps
        self.failing = failing
        self.open_path: str | None = None
        self.open_error = RuntimeError("open failed")
        self.close_error = RuntimeError("close failed")

    def open(self, path: str) -> str:
        self.steps.append("open")
        self.open_path = path
        if "open" in self.failing:
            raise self.open_error
        return "connection"

    def close(self, db: str) -> None:
        self.steps.append("close")
        if "close" in self.failing:
            raise self.close_error


class AsyncStepAdapter(StepAdapter):
    async def open(self, path: str) -> str:  # type: ignore[override]
        return StepAdapter.open(self, path)

    async def close(self, db: str) -> None:  # type: ignore[override]
        StepAdapter.close(self, db)


def flatten(error: BaseException | None) -> list[BaseException]:
    if error is None:
        return []
    if isinstance(error, BaseExceptionGroup):
        return list(error.exceptions)
    return [error]


def lifecycle_daemon(daemons: list[ContractDaemon], fixture: dict[str, Any], steps: list[str], failing: set[str]) -> ContractDaemon:
    """Saves each database request as a step."""
    name = fixture["lease"]["name"]

    def reply(received: Received) -> Reply:
        step = received.path.removeprefix("/v1/databases/")
        steps.append(step)
        if step in failing:
            return 503, "application/json", json.dumps({"code": "unavailable", "message": f"{step} failed"}).encode()
        if step == "acquire":
            return 200, "application/json", json.dumps(fixture["acquire_response"]).encode()
        if step == "sync":
            synced = {
                "name": name,
                "path": "/x",
                "role": "origin",
                "state": "synced",
                "acquired": True,
                "synced_server": {"version": 3},
                "synced_cloud": {"version": 3},
            }
            return 200, "application/json", json.dumps(synced).encode()
        released = {
            "database": {"name": name, "path": "/x", "role": "origin", "state": "synced"},
            "checkpointed": False,
            "vacuumed": False,
        }
        return 200, "application/json", json.dumps(released).encode()

    return start_daemon(daemons, reply)


def check_raised(call: str, raised: BaseException | None, expected_errors: list[dict[str, Any]], raised_sources: dict[str, Any]) -> None:
    """Compares what a call raised with a fixture's list, as the Go and TypeScript runners do."""
    errors = flatten(raised)
    assert len(errors) == len(expected_errors), f"{call} raised {errors!r}"
    for actual, expected in zip(errors, expected_errors, strict=True):
        if expected.get("source") == "work":
            assert actual is raised_sources["work_error"], call
        elif expected.get("source") == "open":
            assert actual is raised_sources["adapter"].open_error, call
        elif expected.get("code") == "cancelled" and raised_sources["client_mode"] == "async":
            assert isinstance(actual, asyncio.CancelledError), call
        else:
            assert isinstance(actual, LitebackupError) and actual.code == expected["code"], actual
            if "details" in expected:
                assert actual.details == expected["details"]
            if "step" in expected:
                assert isinstance(actual, LeaseCleanupError) and actual.step == expected["step"], actual
            if expected.get("cause") == "close":
                assert actual.__cause__ is raised_sources["adapter"].close_error
            if expected.get("cause") == "release":
                assert isinstance(actual.__cause__, LitebackupError) and actual.__cause__.code == "unavailable"


@pytest.mark.parametrize("name", fixture_names("lifecycle"))
async def test_lifecycle(name: str, client_mode: str, cwd: str, daemons: list[ContractDaemon]) -> None:
    """Creates a pool, runs one lease with the fixture's settings, then closes the pool."""
    fixture = read_fixture("lifecycle", name, cwd)
    if "python" in fixture.get("skip", {}):
        pytest.skip(fixture["skip"]["python"])
    if client_mode == "sync" and "cancel" in fixture:
        pytest.skip("the sync client has no cancellation; async tasks cover it")
    failing = set(fixture["fail"])
    steps: list[str] = []
    lease = fixture["lease"]
    daemon = lifecycle_daemon(daemons, fixture, steps, failing)
    client = make_client(client_mode, daemon)
    adapter = StepAdapter(steps, failing) if client_mode == "sync" else AsyncStepAdapter(steps, failing)
    pool_options: dict[str, Any] = {"cleanup_timeout": duration(lease.get("cleanup_timeout"))}
    lease_options = {
        "durability": lease.get("durability"),
        "path": lease.get("path"),
        "offload": lease.get("offload", False),
        "require_existing": lease.get("require_existing", False),
    }
    raised_sources: dict[str, Any] = {"work_error": None, "adapter": adapter, "client_mode": client_mode}
    raised: BaseException | None = None
    close_raised: BaseException | None = None

    def work() -> None:
        steps.append("work")
        if "work" in failing:
            raised_sources["work_error"] = RuntimeError("work failed")
            raise raised_sources["work_error"]

    def lease_sync(sync_client: Client) -> None:
        nonlocal raised, close_raised
        assert isinstance(sync_client, Litebackup)
        try:
            pool = sync_client.databases.pool(adapter, **pool_options)
        except BaseException as error:
            raised = error
            return
        try:
            with pool.lease(lease["name"], **lease_options):
                work()
        except BaseException as error:
            raised = error
        try:
            pool.close()
        except BaseException as error:
            close_raised = error

    async def lease_async(async_client: Client) -> None:
        nonlocal raised, close_raised
        assert isinstance(async_client, AsyncLitebackup)
        try:
            pool = async_client.databases.pool(cast(AsyncStepAdapter, adapter), **pool_options)
        except BaseException as error:
            raised = error
            return

        async def lease_task() -> None:
            async with pool.lease(lease["name"], **lease_options):
                steps.append("work")
                if fixture.get("cancel") in ("work", "work-returns"):
                    task = asyncio.current_task()
                    assert task is not None
                    task.cancel()
                    try:
                        await asyncio.sleep(1)
                    except asyncio.CancelledError as error:
                        if fixture["cancel"] == "work":
                            raised_sources["work_error"] = error
                            raise
                if "work" in failing:
                    raised_sources["work_error"] = RuntimeError("work failed")
                    raise raised_sources["work_error"]

        await async_client.connect()
        task = asyncio.create_task(lease_task())
        if fixture.get("cancel") == "before":
            task.cancel()
        try:
            await task
        except BaseException as error:
            raised = error
        try:
            await pool.close()
        except BaseException as error:
            close_raised = error

    try:
        if client_mode == "sync":
            await asyncio.to_thread(lease_sync, client)
        else:
            await lease_async(client)
    finally:
        await close(client)
    expect = fixture["expect"]
    assert steps == expect["steps"]
    if expect.get("open_path"):
        assert adapter.open_path == expect["open_path"]
    if expect.get("created_file"):
        assert os.path.exists(expect["created_file"])
    if expect.get("missing_file"):
        assert not os.path.exists(expect["missing_file"])
    for received in daemon.received:
        if received.path == "/v1/databases/acquire":
            expected_body = expect["acquire_body"]
        elif received.path == "/v1/databases/release":
            expected_body = expect["release_body"]
        else:
            continue
        assert with_spent_cleanup(received.json(), expected_body) == expected_body, received.path
    check_raised("the lease", raised, expect["raised"], raised_sources)
    check_raised("closing the pool", close_raised, expect["close_raised"], raised_sources)


def test_fixture_directories_exist() -> None:
    assert os.path.isdir(CONTRACT_ROOT / "requests")


@pytest.mark.parametrize("name", fixture_names("credentials"))
def test_contract_credentials(name: str) -> None:
    fixture = read_fixture("credentials", name)
    expect = fixture.get("expect")
    if expect:
        with pytest.raises(LitebackupError) as raised:
            credential_type(fixture["api_key"])
        assert raised.value.code == expect["code"]
        secret = fixture["api_key"].strip().partition("@")[0]
        assert not secret or secret not in raised.value.message
        return
    assert credential_type(fixture["api_key"]) == fixture.get("type")


def test_status_times() -> None:
    """The databases-get answer carries the server time as synced_server.time and the archive time as synced_cloud.archived."""
    status = decode(DatabaseStatus, read_fixture("requests", "databases-get")["response"])
    assert status.synced_server.time == datetime.fromisoformat("2026-11-15T09:31:00Z")
    assert status.synced_cloud.archived == datetime.fromisoformat("2026-11-15T09:30:00Z")


def test_state_times() -> None:
    """Each state's timestamp decodes: an offloaded database's, a retrying origin's, and a pending origin's."""
    offloaded = decode(DatabaseStatus, read_fixture("requests", "databases-offload")["response"])
    assert offloaded.offloaded == datetime.fromisoformat("2026-11-15T09:32:00Z")
    assert offloaded.synced == datetime.fromisoformat("2026-11-15T09:31:00Z")
    assert offloaded.released == datetime.fromisoformat("2026-11-15T09:31:30Z")
    retrying = decode(DatabaseStatus, read_fixture("requests", "databases-sync")["response"])
    assert retrying.state == "retrying"
    assert retrying.retrying == datetime.fromisoformat("2026-11-15T09:25:00Z")
    assert retrying.failed == datetime.fromisoformat("2026-11-15T09:29:00Z")
    assert retrying.error == "unavailable: the server did not answer"
    assert retrying.syncing == datetime.fromisoformat("2026-11-15T09:20:00Z")
    pending = decode(DatabaseStatus, read_fixture("requests", "databases-get-pending")["response"])
    assert pending.state == "pending"
    assert pending.pending == datetime.fromisoformat("2026-11-15T09:33:00Z")
    assert pending.syncing is None


def test_release_offloaded() -> None:
    """A release that offloaded says when it removed the file, and one that kept it has no offloaded."""
    offloaded = decode(ReleaseResult, read_fixture("requests", "release-all")["response"])
    assert offloaded.offloaded == datetime.fromisoformat("2026-11-15T09:32:00Z")
    assert decode(ReleaseResult, read_fixture("requests", "release-no-offload")["response"]).offloaded is None


def test_status_offload_durability() -> None:
    """An acquired database's answer carries offload_durability, and one not acquired has none."""
    acquired = decode(AcquireResult, read_fixture("requests", "acquire-all")["response"])
    assert acquired.database.offload_durability == "cloud"
    assert decode(DatabaseStatus, read_fixture("requests", "databases-get")["response"]).offload_durability is None


def test_shutdown_failed() -> None:
    """The shutdown-all answer carries a database the drain could not drain, with its code and message."""
    shutdown = decode(ShutdownResult, read_fixture("requests", "shutdown-all")["response"])
    assert shutdown.released_count == 2
    assert [(failure.name, failure.code, failure.message) for failure in shutdown.failures] == [
        ("orders.sqlite", ErrorCode.LOCK_UNAVAILABLE, "another machine writes the database (worker-3)")
    ]
    assert decode(ShutdownResult, read_fixture("requests", "shutdown-default")["response"]).failures == []


@pytest.mark.parametrize("override", json.loads((CONTRACT_ROOT / "binary" / "override-missing.json").read_text())["overrides"])
def test_missing_override(override: str, tmp_path: Path) -> None:
    """An override, the option or LITEBACKUP_PATH, that names no file is binary_not_found and names the path."""
    fixture = json.loads((CONTRACT_ROOT / "binary" / "override-missing.json").read_text())
    missing = str(tmp_path / fixture["missing_file"])
    environment = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp_path)}
    option: str | None = None
    if override == "option":
        option = missing
    elif override == "environment":
        environment["LITEBACKUP_PATH"] = missing
    else:
        pytest.fail(f"unknown override {override}")
    with pytest.raises(LitebackupError) as raised:
        litebackup_binary(environment, option)
    assert raised.value.code == fixture["code"]
    assert fixture["message_contains"] in raised.value.message and missing in raised.value.message


def test_override_that_exists(tmp_path: Path) -> None:
    """A file behind a directory this user may not enter resolves; a script whose interpreter is missing is launch_failed."""
    fixture = json.loads((CONTRACT_ROOT / "binary" / "override-missing.json").read_text())
    environment = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    if os.geteuid() != 0:
        denied = tmp_path / fixture["denied_file"]
        denied.parent.mkdir(parents=True)
        denied.write_text("#!/bin/sh\n")
        denied.chmod(0o755)
        denied.parent.chmod(0)
        try:
            assert litebackup_binary(environment, str(denied)) == str(denied)
        finally:
            denied.parent.chmod(0o755)
    script = tmp_path / "litebackup"
    script.write_text(fixture["interpreter_script"])
    script.chmod(0o755)
    with Litebackup(session="jobs", binary_path=str(script), environment=environment) as client, pytest.raises(LitebackupError) as raised:
        client.backups.once(tmp_path / "a.db")
    assert raised.value.code == fixture["interpreter_code"]
    assert fixture["interpreter_message_contains"] in raised.value.message


def test_override_under_a_file(tmp_path: Path) -> None:
    """An override under a regular file is binary_not_found; a start error is binary_not_found only when the file is gone."""
    fixture = json.loads((CONTRACT_ROOT / "binary" / "override-missing.json").read_text())
    under_file = tmp_path / fixture["not_directory_file"]
    under_file.parent.write_text("a file, not a directory")
    environment = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    client = Litebackup(session="jobs", binary_path=str(under_file), environment=environment)
    with client, pytest.raises(LitebackupError) as raised:
        client.backups.once(tmp_path / "a.db")
    assert raised.value.code == fixture["code"]
    assert start_failure(str(under_file), NotADirectoryError()).code == fixture["code"]
    existing = tmp_path / "litebackup"
    existing.write_text("#!/bin/sh\n")
    assert start_failure(str(existing), NotADirectoryError()).code == fixture["interpreter_code"]
