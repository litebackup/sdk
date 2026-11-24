"""Client behavior the contract fixtures do not cover: connections, close, binaries, sessions, and releases."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from litebackup import (
    API_KEY_ENVIRONMENT,
    AsyncLitebackup,
    ErrorCode,
    Litebackup,
    LitebackupError,
    _decode,
    _failover,
    _foreground,
    _launch,
    _wire,
    litebackup_binary,
    session_socket,
)
from litebackup._async_transport import AsyncTransport
from litebackup._binary import start_failure_message
from litebackup._generated import error_codes as spec_codes
from litebackup._generated import models
from litebackup._transport import SyncTransport
from litebackup.release import SignatureError, find_release

from .contract_daemon import ContractDaemon, Received, Reply

METADATA = Path(__file__).resolve().parents[2] / "testdata" / "contract" / "binary"
# The refusal a test daemon reports for a failover's file.
REFUSED_MESSAGE = "/data/app.sqlite differs from the server head; it was left as it is"


def empty_rules(_received: Received) -> Reply:
    return 200, "application/json", b'{"rules":[]}'


def test_keep_alive_reuses_one_connection() -> None:
    daemon = ContractDaemon(empty_rules)
    connections: set[int] = set()
    original = daemon._server.RequestHandlerClass.handle  # type: ignore[attr-defined]

    def counting_handle(handler: object) -> None:
        connections.add(id(handler))
        original(handler)

    daemon._server.RequestHandlerClass.handle = counting_handle  # type: ignore[attr-defined]
    try:
        with Litebackup(socket_path=daemon.socket_path) as client:
            for _ in range(5):
                assert client.rules.list() == []
            assert client.client_version == "1.1.0"
        assert len(connections) == 1
    finally:
        daemon.close()


def test_close_fails_later_calls_and_never_shuts_down() -> None:
    daemon = ContractDaemon(empty_rules)
    try:
        client = Litebackup(socket_path=daemon.socket_path)
        client.rules.list()
        client.close()
        with pytest.raises(LitebackupError) as raised:
            client.rules.list()
        assert raised.value.code == "closed"
        assert all(received.path != "/v1/session/shutdown" for received in daemon.received)
    finally:
        daemon.close()


def test_close_ends_a_pending_call() -> None:
    release = threading.Event()

    def slow(_received: Received) -> Reply:
        release.wait(10)
        return 200, "application/json", b'{"events":[],"next":0}'

    daemon = ContractDaemon(slow)
    try:
        client = Litebackup(socket_path=daemon.socket_path)
        client.connect()
        raised: list[BaseException] = []

        def watch() -> None:
            try:
                client.events.page(wait=30)
            except LitebackupError as error:
                raised.append(error)

        thread = threading.Thread(target=watch)
        thread.start()
        while not daemon.received:
            threading.Event().wait(0.01)
        client.close()
        thread.join(5)
        assert not thread.is_alive()
        assert isinstance(raised[0], LitebackupError) and raised[0].code == "closed"
    finally:
        release.set()
        daemon.close()


async def test_async_cancellation_propagates_and_the_client_keeps_working() -> None:
    gate = threading.Event()

    def slow_then_fast(received: Received) -> Reply:
        if received.query.get("wait_millis"):
            gate.wait(10)
        return 200, "application/json", b'{"events":[],"next":0}'

    daemon = ContractDaemon(slow_then_fast)
    try:
        async with AsyncLitebackup(socket_path=daemon.socket_path) as client:
            task = asyncio.create_task(client.events.page(wait=30))
            while not daemon.received:  # noqa: ASYNC110 - the daemon thread fills the list
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            gate.set()
            assert (await client.events.page()).next == 0
    finally:
        gate.set()
        daemon.close()


def test_explicit_socket_never_launches(tmp_path: Path) -> None:
    with (
        pytest.raises(LitebackupError) as raised,
        Litebackup(socket_path=str(tmp_path / "missing.sock"), binary_path="/bin/false") as client,
    ):
        client.rules.list()
    assert raised.value.code == "connection_failed"


def executable(path: Path, script: str = "exit 0") -> str:
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_binary_resolution_order(tmp_path: Path) -> None:
    on_path = tmp_path / "path"
    on_path.mkdir()
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    local = executable(home / ".local" / "bin" / "litebackup")
    environment = {"PATH": str(on_path), "HOME": str(home)}
    assert litebackup_binary(environment) == local
    found = executable(on_path / "litebackup")
    assert litebackup_binary(environment) == found
    (tmp_path / "opt").mkdir()
    (tmp_path / "explicit").mkdir()
    override = executable(tmp_path / "opt" / "litebackup")
    explicit = executable(tmp_path / "explicit" / "litebackup")
    assert litebackup_binary({**environment, "LITEBACKUP_PATH": override}) == override
    assert litebackup_binary({**environment, "LITEBACKUP_PATH": override}, explicit) == explicit
    with pytest.raises(LitebackupError) as raised:
        litebackup_binary({"PATH": str(tmp_path / "none"), "HOME": str(tmp_path / "nohome")})
    assert raised.value.code == "binary_not_found" and "cli.sh" in raised.value.message


def test_missing_binary_is_reported_on_connect(tmp_path: Path) -> None:
    environment = {"PATH": str(tmp_path), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    with pytest.raises(LitebackupError) as raised, Litebackup(session="nobinary", environment=environment) as client:
        client.connect()
    assert raised.value.code == "binary_not_found"


def test_session_sockets(tmp_path: Path) -> None:
    missing = str(tmp_path / "client.json")
    assert session_socket(None, {"XDG_RUNTIME_DIR": "/run/user/7", "HOME": str(tmp_path)}, missing) == (
        "default",
        "/run/user/7/litebackup/default/client.sock",
    )
    assert session_socket(None, {"LITEBACKUP_SESSION": "jobs", "XDG_RUNTIME_DIR": "/r", "HOME": str(tmp_path)}, missing)[0] == "jobs"
    service = tmp_path / ".config" / "litebackup"
    service.mkdir(parents=True)
    (service / "service.json").write_text(json.dumps({"runtime_directory": "/var/run/litebackup-user"}))
    assert session_socket("web", {"HOME": str(tmp_path)}, missing)[1] == "/var/run/litebackup-user/web/client.sock"
    system = tmp_path / "client.json"
    system.write_text(json.dumps({"uid": os.getuid(), "runtime_directory": "/run/litebackup"}))
    assert session_socket("web", {"HOME": str(tmp_path)}, str(system))[1] == "/run/litebackup/web/client.sock"
    with pytest.raises(LitebackupError):
        session_socket("Bad Name", {}, missing)


INVITE = "litebackup_invite_exampleinvitevalue@backups.example.com"
JOIN_TOKEN = "litebackup_join_exampletokenvalue@backups.example.com"
API_KEY = "litebackup_examplekeyvalue@backups.example.com"


def test_invite_credentials_are_refused_without_echoing_them() -> None:
    for arguments in ({"api_key": INVITE}, {"environment": {API_KEY_ENVIRONMENT: INVITE}}):
        with pytest.raises(LitebackupError) as raised:
            Litebackup(socket_path="/tmp/x.sock", **arguments)  # type: ignore[arg-type]
        assert raised.value.code == "invalid_argument"
        assert "invite token" in raised.value.message and "exampleinvitevalue" not in raised.value.message


def credential_stand_in(tmp_path: Path) -> tuple[str, Path, dict[str, str]]:
    """A litebackup stand-in that appends its command and the LITEBACKUP_API_KEY it got ("unset" without one) to a file."""
    binary = tmp_path / "litebackup"
    calls = tmp_path / "calls"
    binary.write_text(f"#!/bin/sh\nprintf '%s %s\\n' \"$1\" \"${{LITEBACKUP_API_KEY-unset}}\" >> '{calls}'\n")
    binary.chmod(0o755)
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    return str(binary), calls, environment


# Each connect: LITEBACKUP_API_KEY, the api_key argument, and what launch got. Without
# either the daemon uses the key it saved.
CONNECTS = [
    (None, JOIN_TOKEN, f"launch {JOIN_TOKEN}"),
    (JOIN_TOKEN, API_KEY, f"launch {API_KEY}"),
    (API_KEY, None, f"launch {API_KEY}"),
    (JOIN_TOKEN, None, f"launch {JOIN_TOKEN}"),
    (None, None, "launch unset"),
    # An empty api_key is no credential, whatever the variable holds.
    (JOIN_TOKEN, "", "launch "),
]


@pytest.mark.parametrize(("variable", "api_key", "launched"), CONNECTS)
async def test_launch_gets_the_credential(tmp_path: Path, variable: str | None, api_key: str | None, launched: str) -> None:
    for index, client_type in enumerate((Litebackup, AsyncLitebackup)):
        directory = tmp_path / str(index)
        directory.mkdir()
        binary, calls, environment = credential_stand_in(directory)
        if variable is not None:
            environment[API_KEY_ENVIRONMENT] = variable
        client = client_type(session="unit", binary_path=binary, environment=environment, api_key=api_key)
        with pytest.raises(LitebackupError):
            if isinstance(client, AsyncLitebackup):
                await client.connect()
            else:
                client.connect()
        if isinstance(client, AsyncLitebackup):
            await client.close()
        else:
            client.close()
        assert calls.read_text().splitlines() == [launched]


def test_foreground_backup_gets_the_api_key(tmp_path: Path) -> None:
    binary, calls, environment = credential_stand_in(tmp_path)
    environment[API_KEY_ENVIRONMENT] = JOIN_TOKEN
    with (
        Litebackup(session="unit", binary_path=binary, environment=environment, api_key=API_KEY) as client,
        contextlib.suppress(LitebackupError),
    ):
        client.backups.once(tmp_path / "app.sqlite")
    assert calls.read_text().splitlines() == [f"backup {API_KEY}"]


def test_release_metadata() -> None:
    release = find_release((METADATA / "package-metadata.json").read_text())
    assert release.version == "0.3.30" and release.channels == ["stable"]
    assert release.file("litebackup-linux-amd64").sha256 == "3338a925ace256760fe48c1fb660c6ffdcc9be570fef5af7f40f8d34ae9391d7"
    with pytest.raises(LookupError):
        find_release((METADATA / "package-metadata.json").read_text(), version="0.3.1")
    with pytest.raises(SignatureError):
        find_release((METADATA / "package-metadata-tampered.json").read_text())
    with pytest.raises(SignatureError):
        find_release(
            (METADATA / "package-metadata.json").read_text(),
            keys={"01a1ead6-4b94-79ca-81f4-3a67c112bc0b": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"},
        )


async def test_stopping_with_an_empty_name_or_id_sends_nothing() -> None:
    daemon = ContractDaemon(empty_rules)
    try:
        with Litebackup(socket_path=daemon.socket_path) as client:
            with pytest.raises(LitebackupError) as raised:
                client.backups.stop("")
            assert raised.value.code == "invalid_argument"
            with pytest.raises(LitebackupError) as raised:
                client.rules.stop("")
            assert raised.value.code == "invalid_argument"
        async with AsyncLitebackup(socket_path=daemon.socket_path) as async_client:
            with pytest.raises(LitebackupError) as raised:
                await async_client.backups.stop("")
            assert raised.value.code == "invalid_argument"
            with pytest.raises(LitebackupError) as raised:
                await async_client.rules.stop("")
            assert raised.value.code == "invalid_argument"
        assert daemon.received == []
    finally:
        daemon.close()


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_launch_failed_carries_the_command_message(tmp_path: Path) -> None:
    """launch_failed carries the launch command's own message, which it asks for as JSON."""
    client_types: tuple[type[Litebackup] | type[AsyncLitebackup], ...] = (Litebackup, AsyncLitebackup)
    for index, client_type in enumerate(client_types):
        directory = tmp_path / str(index)
        directory.mkdir()
        binary = directory / "litebackup"
        arguments = directory / "arguments"
        binary.write_text(
            "#!/bin/sh\n"
            f"echo \"$@\" > '{arguments}'\n"
            f'echo \'{{"event":"warning","code":"{spec_codes.NOT_CONFIGURED}","message":"the server waits for setup"}}\' >&2\n'
            f'echo \'{{"code":"{spec_codes.UNAUTHENTICATED}","message":"this machine has not joined a server"}}\' >&2\n'
            "exit 1\n"
        )
        binary.chmod(0o755)
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(directory), "XDG_RUNTIME_DIR": str(directory / "run")}
        client = client_type(session="unit", binary_path=str(binary), environment=environment)
        with pytest.raises(LitebackupError) as raised:
            if isinstance(client, AsyncLitebackup):
                await client.connect()
            else:
                client.connect()
        if isinstance(client, AsyncLitebackup):
            await client.close()
        else:
            client.close()
        assert raised.value.code == ErrorCode.LAUNCH_FAILED
        assert raised.value.message == "starting Litebackup failed: this machine has not joined a server"
        assert arguments.read_text() == "launch --session unit --stdout-format json\n"
    assert _launch.launch_refusal("plain text\n") == (ErrorCode.LAUNCH_FAILED, "plain text")


def test_a_launch_that_refuses_another_credential_keeps_drain_required(tmp_path: Path) -> None:
    message = "drain it first with litebackup drain, then change the key"
    binary = executable(tmp_path / "litebackup", f'echo \'{{"code":"{spec_codes.DRAIN_REQUIRED}","message":"{message}"}}\' >&2\nexit 1')
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    client = Litebackup(session="unit", binary_path=binary, environment=environment)
    with pytest.raises(LitebackupError) as raised:
        client.connect()
    client.close()
    assert raised.value.code == ErrorCode.DRAIN_REQUIRED
    assert raised.value.message == f"starting Litebackup failed: {message}"


async def test_cancelling_an_async_launch_reaps_the_daemon(tmp_path: Path) -> None:
    pid_file = tmp_path / "launch.pid"
    binary = executable(tmp_path / "litebackup", f'echo $$ > "{pid_file}"\nexec {shutil.which("sleep")} 30')
    environment = {"PATH": str(tmp_path), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    client = AsyncLitebackup(session="slowlaunch", binary_path=binary, environment=environment)
    task = asyncio.create_task(client.connect())
    while not pid_file.exists() or not pid_file.read_text().strip():  # noqa: ASYNC110 - the launch process writes the file
        await asyncio.sleep(0.01)
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not process_exists(pid)
    await client.close()


def sleeping_binary(tmp_path: Path) -> tuple[str, Path, dict[str, str]]:
    """A litebackup stub that records its PID and sleeps, with an environment that finds nothing else."""
    pid_file = tmp_path / "job.pid"
    binary = executable(tmp_path / "litebackup", f'echo $$ > "{pid_file}"\nexec {shutil.which("sleep")} 30')
    return binary, pid_file, {"PATH": str(tmp_path), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}


def started_pid(pid_file: Path) -> int:
    for _ in range(500):
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text())
        threading.Event().wait(0.01)
    raise AssertionError("the job did not start")


async def test_once_on_a_closed_client_runs_nothing(tmp_path: Path) -> None:
    binary, pid_file, environment = sleeping_binary(tmp_path)
    client = Litebackup(session="jobs", binary_path=binary, environment=environment)
    client.close()
    with pytest.raises(LitebackupError) as raised:
        client.backups.once(tmp_path / "app.sqlite")
    assert raised.value.code == "closed"
    async_client = AsyncLitebackup(session="jobs", binary_path=binary, environment=environment)
    await async_client.close()
    with pytest.raises(LitebackupError) as raised:
        await async_client.backups.once(tmp_path / "app.sqlite")
    assert raised.value.code == "closed"
    assert not pid_file.exists()


def test_close_ends_a_running_foreground_backup(tmp_path: Path) -> None:
    binary, pid_file, environment = sleeping_binary(tmp_path)
    client = Litebackup(session="jobs", binary_path=binary, environment=environment)
    raised: list[BaseException] = []

    def back_up() -> None:
        try:
            client.backups.once(tmp_path / "app.sqlite")
        except LitebackupError as error:
            raised.append(error)

    thread = threading.Thread(target=back_up)
    thread.start()
    pid = started_pid(pid_file)
    client.close()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(raised[0], LitebackupError) and raised[0].code == "closed"
    assert not process_exists(pid)


async def test_async_close_ends_a_running_foreground_backup(tmp_path: Path) -> None:
    binary, pid_file, environment = sleeping_binary(tmp_path)
    client = AsyncLitebackup(session="jobs", binary_path=binary, environment=environment)
    task = asyncio.create_task(client.backups.once(tmp_path / "app.sqlite"))
    pid = await asyncio.to_thread(started_pid, pid_file)
    await client.close()
    assert not process_exists(pid)
    with pytest.raises(LitebackupError) as raised:
        await asyncio.wait_for(task, 5)
    assert raised.value.code == "closed"


def stubborn_binary(tmp_path: Path) -> tuple[str, Path, dict[str, str]]:
    """A litebackup stub that ignores SIGTERM."""
    pid_file = tmp_path / "job.pid"
    script = f'trap "" TERM\necho $$ > "{pid_file}"\nwhile :; do {shutil.which("sleep")} 0.05; done'
    binary = executable(tmp_path / "litebackup", script)
    return binary, pid_file, {"PATH": str(tmp_path), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}


async def test_cancelling_once_kills_a_job_that_ignores_sigterm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_foreground, "STOP_GRACE_SECONDS", 0.2)
    binary, pid_file, environment = stubborn_binary(tmp_path)
    client = AsyncLitebackup(session="jobs", binary_path=binary, environment=environment)
    task = asyncio.create_task(client.backups.once(tmp_path / "app.sqlite"))
    pid = await asyncio.to_thread(started_pid, pid_file)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not process_exists(pid)
    await client.close()


def test_sync_once_stops_after_its_timeout_and_grace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_wire, "DEADLINE_GRACE_SECONDS", 0.2)
    binary, pid_file, environment = stubborn_binary(tmp_path)
    with Litebackup(session="jobs", binary_path=binary, environment=environment) as client:
        with pytest.raises(LitebackupError) as raised:
            client.backups.once(tmp_path / "app.sqlite", timeout=0.1)
        assert raised.value.code == "timeout"
        assert not process_exists(int(pid_file.read_text()))


def test_close_during_connect_leaves_no_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(tmp_path / "client.sock"))
    listener.listen()
    transport = SyncTransport(str(tmp_path / "client.sock"))
    opened: list[socket.socket] = []

    closer: list[threading.Thread] = []

    class ClosingSocket(socket.socket):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            opened.append(self)
            # close() from another thread while the connection is being set up; give it a moment to finish.
            closed = threading.Event()

            def close() -> None:
                transport.close()
                closed.set()

            closer.append(threading.Thread(target=close))
            closer[0].start()
            closed.wait(0.2)

    monkeypatch.setattr(socket, "socket", ClosingSocket)
    try:
        started = time.monotonic()
        with pytest.raises(LitebackupError) as raised:
            transport.request("GET", "/v1/backup-rules/list", None, 10)
        assert raised.value.code == "closed"
        assert time.monotonic() - started < 5, "the call waited for its deadline instead of seeing close()"
        closer[0].join(5)
        assert len(opened) == 1 and opened[0].fileno() == -1
    finally:
        listener.close()


async def test_async_close_during_connect_leaves_no_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    accepted: list[asyncio.StreamWriter] = []
    server = await asyncio.start_unix_server(lambda _reader, writer: accepted.append(writer), str(tmp_path / "client.sock"))
    transport = AsyncTransport(str(tmp_path / "client.sock"))
    opened: list[asyncio.StreamWriter] = []
    original = asyncio.open_unix_connection

    async def closing_open(path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await original(path)
        opened.append(writer)
        await transport.close()
        return reader, writer

    monkeypatch.setattr(asyncio, "open_unix_connection", closing_open)
    try:
        with pytest.raises(LitebackupError) as raised:
            await transport.request("GET", "/v1/backup-rules/list", None, 5)
        assert raised.value.code == "closed"
        assert len(opened) == 1 and opened[0].is_closing()
    finally:
        # The server may accept the connection only after the client side closed it.
        for _ in range(100):
            if accepted:
                break
            await asyncio.sleep(0.01)
        server.close()
        for writer in accepted:
            writer.close()
        await server.wait_closed()


class FailoverDaemon:
    """Answers a failover start, then every long-poll with a role change, so role callbacks keep coming."""

    def __init__(self) -> None:
        self.page = 0
        self.stopped_when_stop_arrived: list[bool] = []
        self.failover: Any = None
        self.daemon = ContractDaemon(self.reply)

    def reply(self, received: Received) -> Reply:
        database = {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "origin", "state": "syncing"}
        if received.path == "/v1/failovers/start":
            writer = {"name": "app.sqlite", "active": True, "this_machine": True, "writer_machine_label": "a"}
            body: object = {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "origin", "database": database, "writer": writer}
        elif received.path == "/v1/backup-rules/stop":
            self.stopped_when_stop_arrived.append(bool(self.failover and self.failover.stopped))
            body = {"lease_pending": False}
        elif received.query.get("after") == "-1":
            body = {"events": [], "next": 0}
        else:
            threading.Event().wait(0.01)
            self.page += 1
            role = "replica" if self.page % 2 else "origin"
            event = {"id": self.page, "time": "2026-01-01T00:00:00Z", "type": "role_changed", "message": role, "name": "app.sqlite"}
            body = {"events": [{**event, "role": role}], "next": self.page}
        return 200, "application/json", json.dumps(body).encode()


def test_stopping_following_awaits_a_running_callback_and_stops_callbacks() -> None:
    fake = FailoverDaemon()
    entered = threading.Event()
    gate = threading.Event()
    calls: list[str] = []

    def on_role_change(role: str) -> None:
        calls.append(role)
        entered.set()
        gate.wait(5)

    try:
        with Litebackup(socket_path=fake.daemon.socket_path) as client:
            failover = client.failovers.start("/data/app.sqlite", on_role_change=on_role_change)
            assert entered.wait(5)
            ending = threading.Thread(target=failover._stop_following)
            ending.start()
            ending.join(0.2)
            assert ending.is_alive(), "_stop_following() returned while a callback was running"
            gate.set()
            ending.join(5)
            assert not ending.is_alive()
            count = len(calls)
            threading.Event().wait(0.1)
            assert len(calls) == count
            thread = failover._thread
        assert not thread.is_alive()
    finally:
        gate.set()
        fake.daemon.close()


async def test_failover_stop_turns_off_callbacks_before_the_stop_request() -> None:
    fake = FailoverDaemon()
    try:
        with Litebackup(socket_path=fake.daemon.socket_path) as client:
            fake.failover = client.failovers.start("/data/app.sqlite")
            fake.failover.stop()
        async with AsyncLitebackup(socket_path=fake.daemon.socket_path) as async_client:
            fake.failover = await async_client.failovers.start("/data/app.sqlite")
            await fake.failover.stop()
        assert fake.stopped_when_stop_arrived == [True, True]
    finally:
        fake.daemon.close()


async def test_failover_sends_discard_local_only_when_asked() -> None:
    starts: list[object] = []

    class FailoverStartDaemon(FailoverDaemon):
        def reply(self, received: Received) -> Reply:
            if received.path == "/v1/failovers/start":
                starts.append(received.json())
            return super().reply(received)

    fake = FailoverStartDaemon()
    try:
        with Litebackup(socket_path=fake.daemon.socket_path) as client:
            fake.failover = client.failovers.start("/data/app.sqlite")
            fake.failover.stop()
            fake.failover = client.failovers.start("/data/app.sqlite", discard_local=True)
            fake.failover.stop()
        async with AsyncLitebackup(socket_path=fake.daemon.socket_path) as async_client:
            fake.failover = await async_client.failovers.start("/data/app.sqlite", discard_local=True)
            await fake.failover.stop()
        assert starts == [
            {"path": "/data/app.sqlite"},
            {"path": "/data/app.sqlite", "discard_local": True},
            {"path": "/data/app.sqlite", "discard_local": True},
        ]
    finally:
        fake.daemon.close()


def test_close_ends_a_failover_callback_waiting_on_the_client() -> None:
    hang = threading.Event()

    class HangingWriter(FailoverDaemon):
        def reply(self, received: Received) -> Reply:
            if received.path == "/v1/databases/writer":
                hang.wait(10)
            return super().reply(received)

    fake = HangingWriter()
    entered = threading.Event()
    raised: list[BaseException | None] = []

    started = threading.Event()

    def on_role_change(role: str) -> None:
        # The start role's call may come before start() returns and binds failover.
        assert started.wait(5)
        if entered.is_set():
            return
        entered.set()
        try:
            failover.writer()
            raised.append(None)
        except LitebackupError as error:
            raised.append(error)

    try:
        client = Litebackup(socket_path=fake.daemon.socket_path)
        failover = client.failovers.start("/data/app.sqlite", on_role_change=on_role_change)
        started.set()
        assert entered.wait(5)
        threading.Event().wait(0.1)
        closing = threading.Thread(target=client.close)
        closing.start()
        closing.join(5)
        assert not closing.is_alive(), "close() waited on a callback that waits on the client"
        assert len(raised) == 1 and isinstance(raised[0], LitebackupError) and raised[0].code == "closed"
    finally:
        hang.set()
        fake.daemon.close()


class ExpiredCursorDaemon:
    """Answers a failover's first long-poll with cursor_expired, then a fresh cursor, a role of origin, and one role change."""

    def __init__(self, failed_role_reads: int = 0) -> None:
        self.failed_role_reads = failed_role_reads
        self.reads: list[str] = []
        self.log_end_reads = 0
        self.role_reads = 0
        self.fresh_pages = 0
        self.daemon = ContractDaemon(self.reply)

    def reply(self, received: Received) -> Reply:
        database = {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "origin", "state": "syncing"}
        after = received.query.get("after", "0")
        status = 200
        if received.path == "/v1/failovers/start":
            writer = {"name": "app.sqlite", "active": True, "this_machine": False, "writer_machine_label": "a"}
            body: object = {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "replica", "database": database, "writer": writer}
        elif received.path == "/v1/backup-rules/stop":
            self.reads.append("stop")
            body = {"lease_pending": False}
        elif received.path == "/v1/databases/status":
            assert received.query.get("name") == "app.sqlite"
            self.role_reads += 1
            if self.role_reads <= self.failed_role_reads:
                self.reads.append("role failed")
                status, body = 503, {"code": spec_codes.UNAVAILABLE, "message": "the daemon is busy"}
            else:
                self.reads.append("role")
                body = database
        elif after == "-1":
            self.log_end_reads += 1
            if self.log_end_reads > 1:
                self.reads.append("log end")
            body = {"events": [], "next": 7 if self.log_end_reads == 1 else 20}
        elif after == "7":
            status = 410
            body = {"code": ErrorCode.CURSOR_EXPIRED, "message": "events after 7 are no longer kept"}
        elif after == "20" and self.fresh_pages == 0:
            self.fresh_pages += 1
            self.reads.append("after 20")
            event = {"id": 21, "time": "2026-01-01T00:00:00Z", "type": "role_changed", "message": "replica", "name": "app.sqlite"}
            body = {"events": [{**event, "role": "replica"}], "next": 21}
        else:
            threading.Event().wait(0.05)
            body = {"events": [], "next": int(after)}
        return status, "application/json", json.dumps(body).encode()

    def resume_expectations(self) -> tuple[list[str], list[str]]:
        """The errors and roles the failover reports, then the reads the daemon sees, once it follows from the fresh cursor."""
        role_read_failed = self.failed_role_reads > 0
        # The role the start answered comes first.
        reported = ["replica", ErrorCode.CURSOR_EXPIRED, *([spec_codes.UNAVAILABLE] if role_read_failed else []), "origin", "replica"]
        # The log's end is read once: a failed role read keeps the cursor it took.
        return reported, ["log end", *(["role failed"] if role_read_failed else []), "role", "log end", "after 20"]


@pytest.mark.parametrize("failed_role_reads", [0, 1])
def test_failover_resumes_from_the_log_end_on_an_expired_cursor(failed_role_reads: int) -> None:
    fake = ExpiredCursorDaemon(failed_role_reads)
    heard: list[str] = []
    followed = threading.Event()

    def on_role_change(role: str) -> None:
        heard.append(role)
        # The first replica is the start's; the second comes from the fresh cursor.
        if heard.count("replica") == 2:
            followed.set()

    try:
        with Litebackup(socket_path=fake.daemon.socket_path) as client:
            failover = client.failovers.start(
                "/data/app.sqlite", on_role_change=on_role_change, on_error=lambda error: heard.append(error.code)
            )
            assert followed.wait(5)
            threading.Event().wait(0.2)
            assert (heard, fake.reads) == fake.resume_expectations()
            failover.stop()
    finally:
        fake.daemon.close()


@pytest.mark.parametrize("failed_role_reads", [0, 1])
async def test_async_failover_resumes_from_the_log_end_on_an_expired_cursor(failed_role_reads: int) -> None:
    fake = ExpiredCursorDaemon(failed_role_reads)
    heard: list[str] = []
    followed = asyncio.Event()

    def on_role_change(role: str) -> None:
        heard.append(role)
        # The first replica is the start's; the second comes from the fresh cursor.
        if heard.count("replica") == 2:
            followed.set()

    try:
        async with AsyncLitebackup(socket_path=fake.daemon.socket_path) as client:
            failover = await client.failovers.start(
                "/data/app.sqlite", on_role_change=on_role_change, on_error=lambda error: heard.append(error.code)
            )
            await asyncio.wait_for(followed.wait(), 5)
            await asyncio.sleep(0.2)
            assert (heard, fake.reads) == fake.resume_expectations()
            await failover.stop()
    finally:
        fake.daemon.close()


def test_failover_stopped_on_an_expired_cursor_reads_nothing_more() -> None:
    fake = ExpiredCursorDaemon()
    started = threading.Event()
    stopped = threading.Event()
    try:
        with Litebackup(socket_path=fake.daemon.socket_path) as client:

            def stop_on_error(error: LitebackupError) -> None:
                assert started.wait(5)
                failover.stop()
                stopped.set()

            failover = client.failovers.start("/data/app.sqlite", on_error=stop_on_error)
            started.set()
            assert stopped.wait(5)
            failover._join()
            assert fake.reads == ["stop"]
    finally:
        fake.daemon.close()


class RoleDaemon:
    """Starts an origin failover, then answers long-polls and role reads normally, every one with an error, or none.

    Once returned is set, the log's end is 9 and the long-poll from the start's cursor 7 holds a role change
    older than the role read and a refusal made while the daemon was stopped.
    """

    def __init__(self, long_polls: str = "empty pages") -> None:
        self.long_polls = long_polls
        self.returned = False
        # What role reads answer, how many there were, and the events the look past the log end 9 finds.
        self.role_read = "origin"
        self.role_reads = 0
        self.raced_changes: dict[str, Any] = {"events": [], "next": 9}
        # From which role read on the database reads as conflicted, as a refused file does; 0 never.
        self.conflicted_from_read = 0
        self.log_end_cursors: list[str] = []
        self.hang = threading.Event()
        self.daemon = ContractDaemon(self.reply)

    def reply(self, received: Received) -> Reply:
        database = {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "origin", "state": "syncing"}
        after = received.query.get("after", "0")
        status = 200
        if received.path == "/v1/failovers/start":
            writer = {"name": "app.sqlite", "active": True, "this_machine": True, "writer_machine_label": "a"}
            body: object = {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "origin", "database": database, "writer": writer}
        elif received.path == "/v1/backup-rules/stop":
            body = {"lease_pending": False}
        elif "wait_millis" not in received.query and received.path == "/v1/events/list":
            if self.returned and after != "-1":
                # The look past the log end after the role read.
                body = self.raced_changes if after == "9" else {"events": [], "next": int(after)}
            else:
                if self.returned:
                    self.log_end_cursors.append(after)
                # Once returned, the first log end read is 9 and later ones find the raced changes' end.
                body = {
                    "events": [],
                    "next": 7 if not self.returned else 9 if len(self.log_end_cursors) == 1 else self.raced_changes["next"],
                }
        elif self.long_polls != "empty pages":
            # Long-polls and role reads fail alike, so the role stays unknown.
            if self.long_polls == "no answer":
                self.hang.wait(10)
            status, body = 503, {"code": spec_codes.UNAVAILABLE, "message": "the daemon is busy"}
        elif received.path == "/v1/databases/status":
            self.role_reads += 1
            body = {**database, "role": self.role_read}
            if 0 < self.conflicted_from_read <= self.role_reads:
                body["state"] = "conflicted"
        elif self.returned and after == "7":
            older = {"id": 8, "time": "2026-01-01T00:00:00Z", "type": "role_changed", "message": "replica", "name": "app.sqlite"}
            older["role"] = "replica"
            refusal = {"id": 9, "time": "2026-01-01T00:01:00Z", "type": "refused", "message": REFUSED_MESSAGE, "name": "app.sqlite"}
            refusal["path"] = "/data/app.sqlite"
            raced = self.raced_changes["events"] if isinstance(self.raced_changes["events"], list) else []
            body = {"events": [older, refusal, *raced], "next": max([9, *(event["id"] for event in raced)])}
        else:
            threading.Event().wait(0.05)
            body = {"events": [], "next": int(after)}
        return status, "application/json", json.dumps(body).encode()

    def close(self) -> None:
        self.hang.set()
        self.daemon.close()


def await_reported(condition: Any, seconds: float = 5) -> None:
    """Polls every 10 ms until the callbacks reported what condition checks, failing after seconds."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


async def await_reported_async(condition: Any, seconds: float = 5) -> None:
    """See await_reported."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def test_failover_reports_the_start_role_once_and_is_unknown_once_stopped() -> None:
    fake = RoleDaemon()
    roles: list[str] = []
    try:
        with Litebackup(socket_path=fake.daemon.socket_path) as client:
            failover = client.failovers.start("/data/app.sqlite", on_role_change=roles.append)
            await_reported(lambda: roles == ["origin"])
            time.sleep(0.2)
            assert roles == ["origin"]
            failover.stop()
            assert failover.role == "unknown" and not failover.is_origin
    finally:
        fake.close()


def test_failover_role_is_unknown_while_the_daemon_is_stopped() -> None:
    fake = RoleDaemon()
    roles: list[str] = []
    errors: list[str] = []
    refusals: list[str] = []
    try:
        with Litebackup(socket_path=fake.daemon.socket_path) as client:
            failover = client.failovers.start(
                "/data/app.sqlite",
                on_role_change=roles.append,
                on_refused=refusals.append,
                on_error=lambda error: errors.append(error.code),
            )
            await_reported(lambda: roles == ["origin"])
            assert failover.is_origin
            fake.daemon.stop()
            stopped = time.monotonic()
            await_reported(lambda: len(roles) == 2)
            assert time.monotonic() - stopped < 1, "the role becomes unknown at once"
            assert roles == ["origin", "unknown"] and not failover.is_origin
            assert errors[0] == ErrorCode.CONNECTION_FAILED
            fake.returned = True
            fake.daemon.serve()
            await_reported(lambda: refusals == [REFUSED_MESSAGE])
            time.sleep(0.2)
            # The role read reports origin; the older role change in the kept cursor's page is skipped,
            # and the older refusal is reported without changing the role the read gave.
            assert roles == ["origin", "unknown", "origin"] and failover.is_origin
            # The log's end is read before the role read and again after it.
            assert fake.log_end_cursors == ["-1", "-1"]
            failover.stop()
    finally:
        fake.close()


async def test_async_failover_role_is_unknown_while_the_daemon_is_stopped() -> None:
    fake = RoleDaemon()
    roles: list[str] = []
    refusals: list[str] = []
    try:
        async with AsyncLitebackup(socket_path=fake.daemon.socket_path) as client:
            failover = await client.failovers.start("/data/app.sqlite", on_role_change=roles.append, on_refused=refusals.append)
            await await_reported_async(lambda: roles == ["origin"])
            fake.daemon.stop()
            stopped = time.monotonic()
            await await_reported_async(lambda: len(roles) == 2)
            assert time.monotonic() - stopped < 1, "the role becomes unknown at once"
            assert roles == ["origin", "unknown"] and not failover.is_origin
            fake.returned = True
            fake.daemon.serve()
            await await_reported_async(lambda: refusals == [REFUSED_MESSAGE])
            await asyncio.sleep(0.2)
            assert roles == ["origin", "unknown", "origin"] and failover.is_origin
            # The log's end is read before the role read and again after it.
            assert fake.log_end_cursors == ["-1", "-1"]
            await failover.stop()
            assert failover.role == "unknown"
    finally:
        fake.close()


@pytest.mark.parametrize("long_polls", ["answered errors", "no answer"])
def test_failover_role_the_daemon_does_not_confirm_is_unknown_after_the_limit(long_polls: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_failover, "FAILOVER_EVENT_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(_failover, "ROLE_CONFIRMATION_LIMIT_SECONDS", 0.4)
    fake = RoleDaemon(long_polls)
    roles: list[str] = []
    errors: list[str] = []
    try:
        with Litebackup(socket_path=fake.daemon.socket_path) as client:
            started = time.monotonic()
            failover = client.failovers.start(
                "/data/app.sqlite", on_role_change=roles.append, on_error=lambda error: errors.append(error.code)
            )
            await_reported(lambda: len(roles) == 2)
            elapsed = time.monotonic() - started
            assert roles == ["origin", "unknown"] and not failover.is_origin
            assert 0.4 <= elapsed < 0.9, f"the role became unknown after {elapsed:.2f} s, want about 0.4"
            assert errors[0] == (ErrorCode.TIMEOUT if long_polls == "no answer" else spec_codes.UNAVAILABLE)
            fake.hang.set()
            failover.stop()
    finally:
        fake.close()


async def test_async_failover_role_the_daemon_does_not_answer_is_unknown_after_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_failover, "FAILOVER_EVENT_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(_failover, "ROLE_CONFIRMATION_LIMIT_SECONDS", 0.4)
    fake = RoleDaemon("no answer")
    roles: list[str] = []
    try:
        async with AsyncLitebackup(socket_path=fake.daemon.socket_path) as client:
            started = time.monotonic()
            failover = await client.failovers.start("/data/app.sqlite", on_role_change=roles.append)
            await await_reported_async(lambda: len(roles) == 2)
            elapsed = time.monotonic() - started
            assert roles == ["origin", "unknown"] and not failover.is_origin
            assert 0.4 <= elapsed < 0.9, f"the role became unknown after {elapsed:.2f} s, want about 0.4"
            fake.hang.set()
            await failover.stop()
    finally:
        fake.close()


def test_failover_reads_its_role_again_when_the_log_shows_a_role_change_after_the_role_read() -> None:
    fake = RoleDaemon()
    fake.role_read = "replica"
    raced = [
        {"id": 10, "time": "2026-01-01T00:02:00Z", "type": "role_changed", "message": "origin", "name": "app.sqlite", "role": "origin"},
        {"id": 11, "time": "2026-01-01T00:03:00Z", "type": "role_changed", "message": "replica", "name": "app.sqlite", "role": "replica"},
    ]
    fake.raced_changes = {"events": raced, "next": 11}
    roles: list[str] = []
    try:
        with Litebackup(socket_path=fake.daemon.socket_path) as client:
            failover = client.failovers.start("/data/app.sqlite", on_role_change=roles.append)
            await_reported(lambda: roles == ["origin"])
            fake.daemon.stop()
            await_reported(lambda: len(roles) == 2)
            fake.returned = True
            fake.daemon.serve()
            await_reported(lambda: len(roles) == 3)
            time.sleep(0.3)
            # The first role read raced the role changes, so it is read again; the long-poll from the kept cursor reports neither.
            assert roles == ["origin", "unknown", "replica"]
            assert fake.role_reads == 2
            failover.stop()
    finally:
        fake.close()


def test_failover_reads_its_role_again_when_the_log_shows_a_refusal_after_the_role_read() -> None:
    fake = RoleDaemon()
    fake.conflicted_from_read = 2
    refusal = {"id": 10, "time": "2026-01-01T00:02:00Z", "type": "refused", "message": REFUSED_MESSAGE, "name": "app.sqlite"}
    fake.raced_changes = {"events": [refusal], "next": 10}
    roles: list[str] = []
    refusals: list[str] = []
    try:
        with Litebackup(socket_path=fake.daemon.socket_path) as client:
            failover = client.failovers.start("/data/app.sqlite", on_role_change=roles.append, on_refused=refusals.append)
            await_reported(lambda: roles == ["origin"])
            fake.daemon.stop()
            await_reported(lambda: len(roles) == 2)
            fake.returned = True
            fake.daemon.serve()
            await_reported(lambda: fake.role_reads >= 2)
            time.sleep(0.3)
            # The first role read raced the refusal, so it is read again: no stale origin, and each refusal once.
            assert roles == ["origin", "unknown"] and not failover.is_origin
            assert refusals == [REFUSED_MESSAGE, REFUSED_MESSAGE]
            failover.stop()
    finally:
        fake.close()


def test_a_read_a_closing_client_ended_clears_the_role_without_a_callback() -> None:
    roles: list[str] = []
    status = _decode.decode(models.Failover, {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "origin"})
    failover = _failover._FailoverState(status, roles.append, None, None)
    progress = _failover._FollowProgress(7)
    # The client's close ends calls in flight with closed before it stops following.
    assert failover._retry_delay(LitebackupError(ErrorCode.CLOSED, "the client is closed"), progress) is None
    assert failover.role == "unknown" and roles == []


def fixed_log_end_events(after: int, own_change_in_scan: bool, newest: int) -> dict[str, Any]:
    """A page of events after after, as the daemon pages them: up to 1000, through newest, with next the last ID delivered.

    All are another database's, except this database's change to origin at 2500 when own_change_in_scan is set,
    and its change to replica at 2601, which exists only after the last look past the role read stopped at 2600.
    """
    events = []
    for event_id in range(after + 1, min(after + 1000, newest) + 1):
        event = {"id": event_id, "time": "2026-01-01T00:00:00Z", "type": "role_changed", "message": "origin", "name": "other.db"}
        event["role"] = "origin"
        if event_id == 2500 and own_change_in_scan:
            event["name"] = "app.sqlite"
        if event_id == 2601:
            event.update(name="app.sqlite", role="replica", message="replica")
        events.append(event)
    return {"events": events, "next": events[-1]["id"] if events else after}


class FixedLogEndDaemon:
    """Expires a failover's first cursor, then pages events through a log end of 2600 and one change after it.

    scan_condition is "unrelated events", "role change on a later page", or "expired cursor" for the look
    past the log end after the role read.
    """

    def __init__(self, scan_condition: str) -> None:
        self.scan_condition = scan_condition
        # The start's log end, the one after the expired cursor, then the end the look past the role read stops at.
        self.log_ends = [7, 100]
        self.scans = 0
        self.role_reads = 0
        self.want_role_reads = 1 if scan_condition == "unrelated events" else 2
        # The newest event: 2601 once the follower read its last role and the log end after it.
        self.newest = 2600
        self.daemon = ContractDaemon(self.reply)

    def reply(self, received: Received) -> Reply:
        database = {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "origin", "state": "syncing"}
        after = int(received.query.get("after", "0"))
        own_change_in_scan = self.scan_condition == "role change on a later page"
        status = 200
        if received.path == "/v1/failovers/start":
            writer = {"name": "app.sqlite", "active": True, "this_machine": False, "writer_machine_label": "a"}
            body: object = {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "replica", "database": database, "writer": writer}
        elif received.path == "/v1/backup-rules/stop":
            body = {"lease_pending": False}
        elif received.path == "/v1/databases/status":
            self.role_reads += 1
            body = database
        elif "wait_millis" in received.query:
            if after == 7:
                status, body = 410, {"code": ErrorCode.CURSOR_EXPIRED, "message": "events after 7 are no longer kept"}
            else:
                body = fixed_log_end_events(after, own_change_in_scan, self.newest)
                if not body["events"]:
                    threading.Event().wait(0.05)
        elif after == -1:
            end = self.log_ends.pop(0) if self.log_ends else self.newest
            if end == self.newest and self.role_reads == self.want_role_reads:
                # This database becomes a replica after the look stopped.
                self.newest = 2601
            body = {"events": [], "next": end}
        elif self.scan_condition == "expired cursor" and self.scans == 0:
            self.scans += 1
            status, body = 410, {"code": ErrorCode.CURSOR_EXPIRED, "message": "pruned"}
        else:
            body = fixed_log_end_events(after, own_change_in_scan, self.newest)
        return status, "application/json", json.dumps(body).encode()


SCAN_CONDITIONS = ["unrelated events", "role change on a later page", "expired cursor"]


@pytest.mark.parametrize("scan_condition", SCAN_CONDITIONS)
def test_failover_look_past_the_log_end_pages_through_a_fixed_log_end(scan_condition: str) -> None:
    fake = FixedLogEndDaemon(scan_condition)
    roles: list[str] = []
    try:
        with Litebackup(socket_path=fake.daemon.socket_path) as client:
            failover = client.failovers.start("/data/app.sqlite", on_role_change=roles.append)
            await_reported(lambda: len(roles) == 3)
            time.sleep(0.2)
            # The change to replica after the fixed log end reaches the callback through the long-poll.
            assert roles == ["replica", "origin", "replica"]
            assert fake.role_reads == fake.want_role_reads
            failover.stop()
    finally:
        fake.daemon.close()


@pytest.mark.parametrize("scan_condition", SCAN_CONDITIONS)
async def test_async_failover_look_past_the_log_end_pages_through_a_fixed_log_end(scan_condition: str) -> None:
    fake = FixedLogEndDaemon(scan_condition)
    roles: list[str] = []
    try:
        async with AsyncLitebackup(socket_path=fake.daemon.socket_path) as client:
            failover = await client.failovers.start("/data/app.sqlite", on_role_change=roles.append)
            await await_reported_async(lambda: len(roles) == 3)
            await asyncio.sleep(0.2)
            assert roles == ["replica", "origin", "replica"]
            assert fake.role_reads == fake.want_role_reads
            await failover.stop()
    finally:
        fake.daemon.close()


async def test_close_while_once_starts_drains_a_job_that_fills_its_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_foreground, "STOP_GRACE_SECONDS", 0.2)
    script = f'trap "" TERM\nwhile :; do {shutil.which("head")} -c 65536 /dev/zero >&2; done'
    binary = executable(tmp_path / "litebackup", script)
    environment = {"PATH": str(tmp_path), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    client = AsyncLitebackup(session="jobs", binary_path=binary, environment=environment)
    start = asyncio.create_subprocess_exec

    async def start_then_close(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await start(*args, **kwargs)
        await asyncio.sleep(0.2)  # let the job fill its stderr pipe
        client._closed = True
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_then_close)
    with pytest.raises(LitebackupError) as raised:
        await asyncio.wait_for(client.backups.once(tmp_path / "app.sqlite"), 10)
    assert raised.value.code == "closed"


def test_page_limits_outside_1_to_1000_are_refused_before_sending() -> None:
    daemon = ContractDaemon(lambda received: (200, "application/json", b'{"databases": []}'))
    try:
        with Litebackup(socket_path=daemon.socket_path) as client:
            for limit in (0, -1, 1001):
                for call in (client.session.status, client.databases.page):
                    with pytest.raises(LitebackupError) as raised:
                        call(limit=limit)
                    assert raised.value.code == "invalid_argument", (call, limit)
        assert not [received for received in daemon.received if received.path == "/v1/databases/list"]
    finally:
        daemon.close()


async def test_foreground_backup_with_a_missing_binary_is_binary_not_found(tmp_path: Path) -> None:
    environment = {"PATH": str(tmp_path), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    missing = str(tmp_path / "absent")
    with Litebackup(session="jobs", binary_path=missing, environment=environment) as client, pytest.raises(LitebackupError) as raised:
        client.backups.once(tmp_path / "a.db")
    assert raised.value.code == "binary_not_found"
    async with AsyncLitebackup(session="jobs", binary_path=missing, environment=environment) as async_client:
        with pytest.raises(LitebackupError) as raised:
            await async_client.backups.once(tmp_path / "a.db")
    assert raised.value.code == "binary_not_found"


def _kill_saved_pid(pid_file: Path) -> None:
    """Kills the process whose PID a stub saved, if it still runs."""
    with contextlib.suppress(FileNotFoundError, ValueError, ProcessLookupError):
        os.kill(int(pid_file.read_text()), 9)


def _job_environment(tmp_path: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}


async def test_async_once_stops_a_job_past_its_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_wire, "DEADLINE_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(_foreground, "STOP_GRACE_SECONDS", 0.2)
    binary = executable(tmp_path / "litebackup", 'trap "" TERM\nwhile :; do sleep 0.05; done')
    started = time.monotonic()
    async with AsyncLitebackup(session="jobs", binary_path=binary, environment=_job_environment(tmp_path)) as client:
        with pytest.raises(LitebackupError) as raised:
            await asyncio.wait_for(client.backups.once(tmp_path / "a.db", timeout=0.05), 10)
    assert raised.value.code == "timeout"
    assert time.monotonic() - started < 5


async def test_once_returns_when_a_child_the_job_started_keeps_its_output_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_foreground, "DRAIN_SECONDS", 0.2)
    child_file = tmp_path / "child.pid"
    binary = executable(tmp_path / "litebackup", f'sleep 30 >&2 &\necho $! > "{child_file}"\nexit 0')
    environment = _job_environment(tmp_path)
    try:
        started = time.monotonic()
        with Litebackup(session="jobs", binary_path=binary, environment=environment) as client:
            await asyncio.to_thread(client.backups.once, tmp_path / "a.db")
        async with AsyncLitebackup(session="jobs", binary_path=binary, environment=environment) as async_client:
            await asyncio.wait_for(async_client.backups.once(tmp_path / "a.db"), 10)
        assert time.monotonic() - started < 5
    finally:
        _kill_saved_pid(child_file)


async def test_cancelling_once_returns_when_a_child_the_job_started_keeps_its_output_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_foreground, "STOP_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(_foreground, "DRAIN_SECONDS", 0.2)
    pid_file = tmp_path / "job.pid"
    child_file = tmp_path / "child.pid"
    binary = executable(
        tmp_path / "litebackup",
        f'sleep 30 >&2 &\necho $! > "{child_file}"\ntrap "" TERM\necho $$ > "{pid_file}"\nwhile :; do sleep 0.05; done',
    )
    try:
        async with AsyncLitebackup(session="jobs", binary_path=binary, environment=_job_environment(tmp_path)) as client:
            task = asyncio.create_task(client.backups.once(tmp_path / "a.db"))
            await asyncio.to_thread(started_pid, pid_file)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
    finally:
        _kill_saved_pid(child_file)


async def test_async_job_reports_no_timeout_when_it_exited_before_the_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The exit check polls; a job that exits between two checks finished before the deadline.
    monkeypatch.setattr(_foreground, "_POLL_SECONDS", 1.0)
    monkeypatch.setattr(_foreground, "DRAIN_SECONDS", 0.1)
    process = await asyncio.create_subprocess_exec("sh", "-c", "sleep 0.05; exit 0", stderr=asyncio.subprocess.PIPE)
    _, timed_out = await _foreground.await_job_async(process, 0.3)
    assert not timed_out and process.returncode == 0


async def test_cancelling_once_while_its_deadline_stops_the_job_still_ends_the_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_wire, "DEADLINE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(_foreground, "STOP_GRACE_SECONDS", 0.5)
    pid_file = tmp_path / "job.pid"
    # Closes its error output and ignores SIGTERM, so only SIGKILL after the grace ends it.
    binary = executable(tmp_path / "litebackup", f'exec 2>&-\ntrap "" TERM\necho $$ > "{pid_file}"\nwhile :; do sleep 0.05; done')
    async with AsyncLitebackup(session="jobs", binary_path=binary, environment=_job_environment(tmp_path)) as client:
        task = asyncio.create_task(client.backups.once(tmp_path / "a.db", timeout=0.05))
        pid = await asyncio.to_thread(started_pid, pid_file)
        await asyncio.sleep(0.3)  # the deadline passed; the job is being stopped
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    for _ in range(40):
        if not process_exists(pid):
            break
        await asyncio.sleep(0.05)
    assert not process_exists(pid)


async def test_a_failed_job_keeps_its_first_diagnostic_after_long_output(tmp_path: Path) -> None:
    script = (
        """echo '{"code":"invalid_database","message":"broken.db: not a database"}' >&2\n"""
        + f"{shutil.which('head')} -c 70000 /dev/zero | tr '\\\\0' 'x' >&2\necho >&2\n"
        + """echo '{"code":"backup_failed","message":"one file failed"}' >&2\nexit 1"""
    )
    binary = executable(tmp_path / "litebackup", script)
    async with AsyncLitebackup(session="jobs", binary_path=binary, environment=_job_environment(tmp_path)) as client:
        with pytest.raises(LitebackupError) as raised:
            await client.backups.once(tmp_path / "a.db")
    assert raised.value.code == "backup_failed"
    assert "broken.db: not a database" in raised.value.message


_DIAGNOSED_FAILURE = (
    "2 files could not be backed up\n"
    "/var/lib/app/a.sqlite: another session writes this database (host-b)\n"
    "/var/lib/app/b.sqlite: not a database"
)


def _diagnosed_job_script() -> str:
    """A failing job whose two file diagnostics sit between long stretches of log output."""
    logs = f"{shutil.which('head')} -c 70000 /dev/zero | tr '\\\\0' 'x' >&2\necho >&2\n"
    return (
        logs
        + """echo '{"code":"busy","message":"/var/lib/app/a.sqlite: another session writes this database (host-b)"}' >&2\n"""
        + logs
        + logs
        + """echo '{"code":"invalid_database","message":"/var/lib/app/b.sqlite: not a database"}' >&2\n"""
        + logs
        + """echo '{"code":"busy","message":"2 files could not be backed up"}' >&2\nexit 1"""
    )


def test_a_failed_job_lists_every_file_diagnostic(tmp_path: Path) -> None:
    binary = executable(tmp_path / "litebackup", _diagnosed_job_script())
    with (
        Litebackup(session="jobs", binary_path=binary, environment=_job_environment(tmp_path)) as client,
        pytest.raises(LitebackupError) as raised,
    ):
        client.backups.once(tmp_path / "a.db")
    assert raised.value.code == ErrorCode.BUSY
    assert raised.value.message == _DIAGNOSED_FAILURE


async def test_a_failed_async_job_lists_every_file_diagnostic(tmp_path: Path) -> None:
    binary = executable(tmp_path / "litebackup", _diagnosed_job_script())
    async with AsyncLitebackup(session="jobs", binary_path=binary, environment=_job_environment(tmp_path)) as client:
        with pytest.raises(LitebackupError) as raised:
            await client.backups.once(tmp_path / "a.db")
    assert raised.value.code == ErrorCode.BUSY
    assert raised.value.message == _DIAGNOSED_FAILURE


def _many_files_job_script() -> str:
    """A failing job with 25 file diagnostics, some between long log output, whose
    final report ends without a line break while a process it started holds the output open."""
    logs = f"{shutil.which('head')} -c 40000 /dev/zero | tr '\\\\0' 'x' >&2\necho >&2\n"
    script = ""
    for number in range(1, 26):
        script += f"""echo '{{"code":"invalid_database","message":"/var/lib/app/{number}.sqlite: not a database"}}' >&2\n"""
        if number % 5 == 0:
            script += logs
    return (
        script
        + "sleep 5 >/dev/null &\n"
        + """printf '%s' '{"code":"invalid_database","message":"25 files could not be backed up"}' >&2\nexit 1"""
    )


async def test_a_failed_async_job_lists_its_first_file_diagnostics_in_order(tmp_path: Path) -> None:
    binary = executable(tmp_path / "litebackup", _many_files_job_script())
    async with AsyncLitebackup(session="jobs", binary_path=binary, environment=_job_environment(tmp_path)) as client:
        with pytest.raises(LitebackupError) as raised:
            await client.backups.once(tmp_path / "a.db")
    lines = raised.value.message.split("\n")
    assert lines[0] == "25 files could not be backed up"
    assert lines[1:] == [f"/var/lib/app/{number}.sqlite: not a database" for number in range(1, 21)]


def _repeating_job_script() -> str:
    """A failing job that reports one file 25 times before a second file."""
    return (
        "for n in $(seq 25); do\n"
        + """  echo '{"code":"busy","message":"/var/lib/app/a.sqlite: another session writes this database (host-b)"}' >&2\n"""
        + "done\n"
        + """echo '{"code":"invalid_database","message":"/var/lib/app/b.sqlite: not a database"}' >&2\n"""
        + """echo '{"code":"busy","message":"2 files could not be backed up"}' >&2\nexit 1"""
    )


def test_a_failed_job_counts_each_file_once(tmp_path: Path) -> None:
    binary = executable(tmp_path / "litebackup", _repeating_job_script())
    with (
        Litebackup(session="jobs", binary_path=binary, environment=_job_environment(tmp_path)) as client,
        pytest.raises(LitebackupError) as raised,
    ):
        client.backups.once(tmp_path / "a.db")
    assert raised.value.message == _DIAGNOSED_FAILURE


async def test_a_failed_async_job_counts_each_file_once(tmp_path: Path) -> None:
    binary = executable(tmp_path / "litebackup", _repeating_job_script())
    async with AsyncLitebackup(session="jobs", binary_path=binary, environment=_job_environment(tmp_path)) as client:
        with pytest.raises(LitebackupError) as raised:
            await client.backups.once(tmp_path / "a.db")
    assert raised.value.message == _DIAGNOSED_FAILURE


async def test_cancelling_an_async_close_still_ends_every_failover() -> None:
    fake = FailoverDaemon()
    try:
        client = AsyncLitebackup(socket_path=fake.daemon.socket_path)
        first = await client.failovers.start("/data/app.sqlite")
        second = await client.failovers.start("/data/app.sqlite")
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert first.stopped and second.stopped
    finally:
        fake.daemon.close()


def test_session_socket_refuses_a_shared_runtime_directory(tmp_path: Path) -> None:
    missing = str(tmp_path / "client.json")
    environment = {"TMPDIR": str(tmp_path), "HOME": str(tmp_path / "user")}
    runtime_root = tmp_path / f"litebackup-{os.getuid()}"
    assert session_socket(None, environment, missing)[1] == str(runtime_root / "litebackup" / "default" / "client.sock")
    assert runtime_root.stat().st_mode & 0o777 == 0o700
    runtime_root.chmod(0o777)
    with pytest.raises(LitebackupError) as raised:
        session_socket(None, environment, missing)
    assert raised.value.code == "invalid_argument"


def test_foreground_backup_reads_an_empty_api_key_as_none(tmp_path: Path) -> None:
    binary, calls, environment = credential_stand_in(tmp_path)
    environment[API_KEY_ENVIRONMENT] = JOIN_TOKEN
    with (
        Litebackup(session="unit", binary_path=binary, environment=environment, api_key="") as client,
        contextlib.suppress(LitebackupError),
    ):
        client.backups.once(tmp_path / "app.sqlite")
    assert calls.read_text().splitlines() == ["backup "]


def test_a_draining_event_makes_the_role_unknown_until_the_next_role_change() -> None:
    roles: list[str] = []
    status = _decode.decode(models.Failover, {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "origin"})
    failover = _failover._FailoverState(status, roles.append, None, None)
    progress = _failover._FollowProgress(7)
    draining = {"id": 8, "time": "2026-01-01T00:00:00Z", "type": "draining", "message": "this machine is draining", "name": "app.sqlite"}
    failover._apply_event(_decode.decode(models.Event, draining), progress)
    assert roles == ["unknown"] and not failover.is_origin
    origin = {"id": 9, "time": "2026-01-01T00:01:00Z", "type": "role_changed", "message": "origin", "name": "app.sqlite", "role": "origin"}
    failover._apply_event(_decode.decode(models.Event, origin), progress)
    assert roles == ["unknown", "origin"] and failover.is_origin


def test_a_role_unknown_after_draining_is_read_again_after_a_quiet_long_poll() -> None:
    status = _decode.decode(models.Failover, {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "origin"})
    failover = _failover._FailoverState(status, None, None, None)
    progress = _failover._FollowProgress(7)
    quiet = _decode.decode(models.EventList, {"events": [], "next": 8})
    failover._read_unknown_role_again(quiet, progress)
    assert not progress.role_read_pending
    draining = {"id": 8, "time": "2026-01-01T00:00:00Z", "type": "draining", "message": "this machine is draining", "name": "app.sqlite"}
    failover._apply_event(_decode.decode(models.Event, draining), progress)
    failover._read_unknown_role_again(_decode.decode(models.EventList, {"events": [draining], "next": 8}), progress)
    assert not progress.role_read_pending
    failover._read_unknown_role_again(quiet, progress)
    assert progress.role_read_pending and progress.log_end_pending


def test_a_role_read_while_the_daemon_drains_reports_unknown() -> None:
    draining = {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "origin", "state": "syncing", "draining": True}
    assert _failover._status_role(_decode.decode(models.DatabaseStatus, draining)) == "unknown"
    del draining["draining"]
    assert _failover._status_role(_decode.decode(models.DatabaseStatus, draining)) == "origin"
    # A refused file reads as conflicted, which makes the role unknown too.
    refused = {**draining, "role": "replica", "state": "conflicted"}
    assert _failover._status_role(_decode.decode(models.DatabaseStatus, refused)) == "unknown"


def test_a_role_callback_that_stops_the_failover_hears_no_refusal() -> None:
    status = _decode.decode(models.Failover, {"name": "app.sqlite", "path": "/data/app.sqlite", "role": "origin"})
    refusals: list[str] = []
    failover: _failover._FailoverState

    def stop_on_unknown(role: str) -> None:
        if role == "unknown":
            failover._mark_stopped()

    failover = _failover._FailoverState(status, stop_on_unknown, refusals.append, None)
    refusal = {"id": 9, "time": "2026-01-01T00:01:00Z", "type": "refused", "message": REFUSED_MESSAGE, "name": "app.sqlite"}
    failover._apply_event(_decode.decode(models.Event, refusal), _failover._FollowProgress(7))
    assert refusals == [] and failover.role == "unknown"


async def test_unusable_binary_names_its_interpreter(tmp_path: Path) -> None:
    """A binary that is there but cannot run names its interpreter line, at launch and in its message."""
    binary = tmp_path / "litebackup"
    binary.write_text("#!/nonexistent/litebackup-interpreter\n")
    binary.chmod(0o755)
    unusable_binary = (
        f"starting {binary} failed: it cannot run because the interpreter its first line names "
        "is not on this machine: #!/nonexistent/litebackup-interpreter"
    )
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    client_types: tuple[type[Litebackup] | type[AsyncLitebackup], ...] = (Litebackup, AsyncLitebackup)
    for client_type in client_types:
        client = client_type(session="unit", binary_path=str(binary), environment=environment)
        with pytest.raises(LitebackupError) as raised:
            if isinstance(client, AsyncLitebackup):
                await client.connect()
            else:
                client.connect()
        if isinstance(client, AsyncLitebackup):
            await client.close()
        else:
            client.close()
        assert raised.value.code == ErrorCode.LAUNCH_FAILED
        assert raised.value.message == unusable_binary
    assert start_failure_message(str(binary), NotADirectoryError()) == unusable_binary
    native = tmp_path / "native"
    native.write_bytes(b"\x7fELF")
    assert start_failure_message(str(native), FileNotFoundError()) == (
        f"starting {native} failed: it cannot run because its interpreter or dynamic loader is not on this machine"
    )
    absent = tmp_path / "absent"
    assert start_failure_message(str(absent), FileNotFoundError("no such file")) == f"starting {absent} failed: no such file"
