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
from typing import Any

import pytest

from litebackup import MINIMUM_DAEMON_VERSION, AsyncLitebackup, Litebackup, LitebackupError

from .contract_daemon import ContractDaemon, Received, Reply

CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "testdata" / "contract"
Client = Litebackup | AsyncLitebackup


def fixture_names(kind: str) -> list[str]:
    return sorted(path.stem for path in (CONTRACT_ROOT / kind).glob("*.json"))


def read_fixture(kind: str, name: str, cwd: str = "/") -> dict[str, Any]:
    text = (CONTRACT_ROOT / kind / f"{name}.json").read_text()
    loaded: dict[str, Any] = json.loads(text.replace("{cwd}", json.dumps(cwd)[1:-1]))
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

    def take(*names: str, convert: Callable[[Any], Any] = lambda value: value) -> None:
        for name in names:
            if name in args:
                keyword[name] = convert(args[name])

    group, _, method = operation.partition(".")
    positional: list[Any] = []
    if operation in ("session.status", "databases.page"):
        take("after", "limit")
    elif operation == "session.shutdown":
        take("wait")
        take("timeout", convert=duration)
    elif operation in ("databases.get", "databases.sync", "databases.offload", "databases.writer", "backups.stop", "replicas.stop"):
        positional = [args["name"]]
    elif operation == "databases.restore":
        positional = [args["name"]]
        keyword["to"] = path_argument(args.get("to"))
        take("version")
        take("time", convert=datetime.fromisoformat)
    elif operation == "databases.acquire":
        positional = [args["name"]]
        take("to", convert=path_argument)
        take("maintenance", "offload", "machine_label", "on_lost")
        take("timeout", convert=duration)
    elif operation == "databases.release":
        positional = [args["name"]]
        take("wait", "offload")
        take("timeout", convert=duration)
    elif operation == "backups.start":
        positional = [path_argument(args["source"])]
        take("name", "prefix", "maximum_local_databases")
    elif operation == "rules.stop":
        positional = [args["id"]]
    elif operation == "replicas.start":
        positional = [args["name"]]
        take("to", convert=path_argument)
        take("write_protection")
        take("timeout", convert=duration)
    elif operation == "replicas.start_prefix":
        positional = [args["prefix"]]
        keyword["to"] = args.get("to")
        take("write_protection")
    elif operation == "replicas.stop_prefix":
        positional = [args["prefix"]]
        keyword["to"] = args.get("to")
    elif operation == "conflicts.delete":
        positional = [args["id"]]
    elif operation == "events.page":
        take("after", "limit")
        take("wait", convert=duration)
    elif operation not in ("rules.list", "replicas.list", "conflicts.list"):
        raise AssertionError(f"no Python mapping for {operation}")
    return group, method, positional, keyword


async def invoke(client: Client, operation: str, args: dict[str, Any]) -> Any:
    group, method, positional, keyword = operation_call(operation, args)
    result = getattr(getattr(client, group), method)(*positional, **keyword)
    return await result if inspect.isawaitable(result) else result


async def connect(client: Client) -> None:
    result = client.connect()
    if inspect.isawaitable(result):
        await result


async def close(client: Client) -> None:
    result = client.close()
    if inspect.isawaitable(result):
        await result


@pytest.fixture(params=["sync", "async"])
def client_kind(request: pytest.FixtureRequest) -> str:
    kind: str = request.param
    return kind


def make_client(kind: str, daemon: ContractDaemon) -> Client:
    return Litebackup(socket_path=daemon.socket_path) if kind == "sync" else AsyncLitebackup(socket_path=daemon.socket_path)


async def call_client(kind: str, client: Client, function: Callable[[Client], Any]) -> Any:
    """Runs a call; the sync client runs in a thread so the event loop stays free."""
    if kind == "sync":
        return await asyncio.to_thread(function, client)
    return await function(client)


@pytest.fixture
def daemons() -> Iterator[list[ContractDaemon]]:
    started: list[ContractDaemon] = []
    yield started
    for daemon in started:
        daemon.check_sdk_version()
        daemon.close()


def start_daemon(daemons: list[ContractDaemon], reply: Callable[[Received], Reply], client_version: str | None = "1.0.0") -> ContractDaemon:
    daemon = ContractDaemon(reply, client_version)
    daemons.append(daemon)
    return daemon


@pytest.mark.parametrize("name", fixture_names("requests"))
async def test_requests(name: str, client_kind: str, cwd: str, daemons: list[ContractDaemon]) -> None:
    fixture = read_fixture("requests", name, cwd)
    daemon = start_daemon(daemons, lambda _received: (200, "application/json", json.dumps(fixture.get("response", {})).encode()))
    client = make_client(client_kind, daemon)
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
async def test_errors(name: str, client_kind: str, daemons: list[ContractDaemon]) -> None:
    fixture = read_fixture("errors", name)
    response = fixture["response"]
    daemon = start_daemon(daemons, lambda _received: (response["status"], response["content_type"], response["body"].encode()))
    client = make_client(client_kind, daemon)
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
async def test_version(name: str, client_kind: str, daemons: list[ContractDaemon]) -> None:
    fixture = read_fixture("version", name)
    daemon = start_daemon(daemons, lambda _received: (500, "text/plain", b""), fixture["client_version"])
    client = make_client(client_kind, daemon)
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
    if operation == "databases.iterate":
        return client.databases.iter_all()
    if operation == "databases.versions":
        return client.databases.iter_versions(args["name"], after=args.get("after", 0), limit=args.get("limit"))
    if operation == "events.watch":
        return client.events.watch(after=args.get("after", 0), wait=duration(args.get("wait")))
    raise AssertionError(f"no Python mapping for {operation}")


@pytest.mark.parametrize("kind_and_name", [(kind, name) for kind in ("pagination", "events") for name in fixture_names(kind)])
async def test_pagination_and_events(kind_and_name: tuple[str, str], client_kind: str, daemons: list[ContractDaemon]) -> None:
    kind, name = kind_and_name
    fixture = read_fixture(kind, name)
    if "python" in fixture.get("skip", {}):
        pytest.skip(fixture["skip"]["python"])
    daemon = exchange_daemon(daemons, fixture["exchanges"])
    client = make_client(client_kind, daemon)
    take = fixture.get("take")
    key = fixture["item_key"]
    items: list[Any] = []
    ended: BaseException | None = None

    def collect_sync(sync_client: Client) -> None:
        nonlocal ended
        try:
            for item in iteration(sync_client, fixture):
                items.append(getattr(item, key))
                if take is not None and len(items) >= take:
                    break
        except LitebackupError as error:
            ended = error

    async def collect_async(async_client: Client) -> None:
        nonlocal ended
        try:
            async for item in iteration(async_client, fixture):
                items.append(getattr(item, key))
                if take is not None and len(items) >= take:
                    break
        except LitebackupError as error:
            ended = error

    try:
        await call_client(client_kind, client, collect_sync if client_kind == "sync" else collect_async)
        assert items == fixture["expect"]["items"]
        if "error" in fixture["expect"]:
            assert isinstance(ended, LitebackupError) and ended.code == fixture["expect"]["error"]["code"]
        elif ended is not None:
            raise ended
        assert len(daemon.received) == len(fixture["exchanges"])
    finally:
        await close(client)


class RecordingAdapter:
    """Records steps and fails open or close on demand."""

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

    def close(self, connection: str) -> None:
        self.steps.append("close")
        if "close" in self.failing:
            raise self.close_error


class AsyncRecordingAdapter(RecordingAdapter):
    async def open(self, path: str) -> str:  # type: ignore[override]
        return RecordingAdapter.open(self, path)

    async def close(self, connection: str) -> None:  # type: ignore[override]
        RecordingAdapter.close(self, connection)


def flatten(error: BaseException | None) -> list[BaseException]:
    if error is None:
        return []
    if isinstance(error, BaseExceptionGroup):
        return list(error.exceptions)
    return [error]


@pytest.mark.parametrize("name", fixture_names("lifecycle"))
async def test_lifecycle(name: str, client_kind: str, cwd: str, daemons: list[ContractDaemon]) -> None:
    fixture = read_fixture("lifecycle", name, cwd)
    if "python" in fixture.get("skip", {}):
        pytest.skip(fixture["skip"]["python"])
    if client_kind == "sync" and "cancel" in fixture:
        pytest.skip("the sync client has no cancellation; async tasks cover it")
    failing = set(fixture["fail"])
    steps: list[str] = []
    scope = fixture["scope"]

    def reply(received: Received) -> Reply:
        step = received.path.removeprefix("/v1/databases/")
        steps.append(step)
        if step in failing:
            return 503, "application/json", json.dumps({"code": "unavailable", "message": f"{step} failed"}).encode()
        if step == "acquire":
            return 200, "application/json", json.dumps(fixture["acquire_response"]).encode()
        released = {
            "database": {"name": scope["name"], "path": "/x", "role": "origin", "state": "synced"},
            "checkpointed": False,
            "vacuumed": False,
            "offloaded": False,
        }
        return 200, "application/json", json.dumps(released).encode()

    daemon = start_daemon(daemons, reply)
    client = make_client(client_kind, daemon)
    adapter = RecordingAdapter(steps, failing) if client_kind == "sync" else AsyncRecordingAdapter(steps, failing)
    options = {
        "durability": scope.get("durability"),
        "to": scope.get("to"),
        "offload": scope.get("offload", False),
        "cleanup_timeout": duration(scope.get("cleanup_timeout")),
        "adapter": adapter,
    }
    work_error: BaseException | None = None
    raised: BaseException | None = None

    def run_sync(sync_client: Client) -> None:
        nonlocal work_error, raised
        assert isinstance(sync_client, Litebackup)
        try:
            with sync_client.databases.managed(scope["name"], **options):
                steps.append("work")
                if "work" in failing:
                    work_error = RuntimeError("work failed")
                    raise work_error
        except BaseException as error:
            raised = error

    async def run_async(async_client: Client) -> None:
        nonlocal work_error, raised
        assert isinstance(async_client, AsyncLitebackup)

        async def scope_task() -> None:
            nonlocal work_error
            async with async_client.databases.managed(scope["name"], **options):
                steps.append("work")
                if fixture.get("cancel") == "work":
                    task = asyncio.current_task()
                    assert task is not None
                    task.cancel()
                    try:
                        await asyncio.sleep(1)
                    except asyncio.CancelledError as error:
                        work_error = error
                        raise
                if "work" in failing:
                    work_error = RuntimeError("work failed")
                    raise work_error

        await async_client.connect()
        task = asyncio.create_task(scope_task())
        if fixture.get("cancel") == "before":
            task.cancel()
        try:
            await task
        except BaseException as error:
            raised = error

    try:
        if client_kind == "sync":
            await asyncio.to_thread(run_sync, client)
        else:
            await run_async(client)
    finally:
        await close(client)
    expect = fixture["expect"]
    assert steps == expect["steps"]
    if expect.get("open_path"):
        assert adapter.open_path == expect["open_path"]
    for received in daemon.received:
        expected_body = expect["acquire_body"] if received.path.endswith("/acquire") else expect["release_body"]
        assert received.json() == expected_body, received.path
    errors = flatten(raised)
    assert len(errors) == len(expect["raised"]), errors
    for actual, expected in zip(errors, expect["raised"], strict=True):
        if expected.get("source") == "work":
            assert actual is work_error
        elif expected.get("source") == "open":
            assert actual is adapter.open_error
        elif expected.get("code") == "cancelled" and client_kind == "async":
            assert isinstance(actual, asyncio.CancelledError)
        else:
            assert isinstance(actual, LitebackupError) and actual.code == expected["code"], actual
            if "details" in expected:
                assert actual.details == expected["details"]
            if expected.get("cause") == "close":
                assert actual.__cause__ is adapter.close_error
            if expected.get("cause") == "release":
                assert isinstance(actual.__cause__, LitebackupError) and actual.__cause__.code == "unavailable"


def test_fixture_directories_exist() -> None:
    assert os.path.isdir(CONTRACT_ROOT / "requests")
