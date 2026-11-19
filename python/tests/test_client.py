"""Client behavior the contract fixtures do not cover: connections, close, binaries, sessions, and releases."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
from pathlib import Path

import pytest

from litebackup import AsyncLitebackup, Litebackup, LitebackupError, litebackup_binary, session_socket
from litebackup.release import SignatureError, find_release

from .contract_daemon import ContractDaemon, Received, Reply

METADATA = Path(__file__).resolve().parents[2] / "testdata" / "contract" / "binary"


def empty_rules(_received: Received) -> Reply:
    return 200, "application/json", b'{"backups":[]}'


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
            assert client.client_version == "1.0.0"
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


def test_close_ends_a_call_in_flight() -> None:
    release = threading.Event()

    def slow(_received: Received) -> Reply:
        release.wait(10)
        return 200, "application/json", b'{"events":[],"next":0}'

    daemon = ContractDaemon(slow)
    try:
        client = Litebackup(socket_path=daemon.socket_path)
        client.connect()
        outcome: list[BaseException] = []

        def watch() -> None:
            try:
                client.events.page(wait=30)
            except LitebackupError as error:
                outcome.append(error)

        thread = threading.Thread(target=watch)
        thread.start()
        while not daemon.received:
            threading.Event().wait(0.01)
        client.close()
        thread.join(5)
        assert not thread.is_alive()
        assert isinstance(outcome[0], LitebackupError) and outcome[0].code == "closed"
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


def executable(path: Path) -> str:
    path.write_text("#!/bin/sh\nexit 0\n")
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
    assert litebackup_binary({**environment, "LITEBACKUP_PATH": "/opt/litebackup"}) == "/opt/litebackup"
    assert litebackup_binary({**environment, "LITEBACKUP_PATH": "/opt/litebackup"}, "/explicit") == "/explicit"
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


def test_join_tokens_are_checked() -> None:
    with pytest.raises(LitebackupError) as raised:
        Litebackup(token="litebackup_key_abc", socket_path="/tmp/x.sock")
    assert raised.value.code == "invalid_argument"


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
