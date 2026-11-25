"""Against a real server and daemons. Set LITEBACKUP_INTEGRATION=1 and LITEBACKUP_PATH."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from litebackup import AsyncLitebackup, ErrorCode, LeaseCleanupError, Litebackup, LitebackupError
from litebackup.integrations import aiosqlite as aiosqlite_support
from litebackup.integrations import sqlite3 as sqlite3_support

pytestmark = pytest.mark.skipif(
    os.environ.get("LITEBACKUP_INTEGRATION") != "1" or not os.environ.get("LITEBACKUP_PATH"),
    reason="set LITEBACKUP_INTEGRATION=1 and LITEBACKUP_PATH",
)
SDK_ROOT = Path(__file__).resolve().parents[2]


class DevelopmentServer:
    """A ``litebackup server run --development`` with a local bucket, as the Go and TypeScript suites use."""

    def __init__(self) -> None:
        self.binary = os.environ["LITEBACKUP_PATH"]
        self.directory = tempfile.mkdtemp(prefix="lbpy")
        self.tag = os.path.basename(self.directory).lower()
        self.data = os.path.join(self.directory, "server")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.address = f"127.0.0.1:{probe.getsockname()[1]}"
        self.environment = {
            **os.environ,
            "XDG_RUNTIME_DIR": os.path.join(self.directory, "run"),
            "XDG_STATE_HOME": os.path.join(self.directory, "state"),
            "LITEBACKUP_API_KEY": "",
            "LITEBACKUP_SESSION": "",
        }
        self.log = open(os.path.join(self.directory, "server.log"), "w")  # noqa: SIM115
        self.process = subprocess.Popen(
            [self.binary, "server", "run", "--development", "--data-directory", self.data, "--listen-address", self.address,
             "--archive-delay", "1s", "--minimum-sync-interval", "0s", "--session-lease", "3s", "--session-heartbeat", "1s"],
            stdout=self.log, stderr=self.log, env=self.environment,
        )  # fmt: skip
        deadline = time.monotonic() + 30
        while True:
            try:
                self.join_token("probe")
                break
            except subprocess.CalledProcessError:
                assert time.monotonic() < deadline, "the server never answered"
                time.sleep(0.1)
        self.admin(
            "bucket",
            "create",
            "--provider",
            "s3",
            "--endpoint",
            "file://" + os.path.join(self.directory, "bucket"),
            "--bucket",
            "bucket",
            "--name",
            "local",
        )
        self.admin("setup", "complete", "key_backup")
        self.admin("setup", "complete", "retention")
        self.sessions: list[str] = []

    def admin(self, *arguments: str) -> str:
        return subprocess.run(
            [self.binary, "server", *arguments, "--data-directory", self.data],
            check=True,
            capture_output=True,
            text=True,
            env=self.environment,
        ).stdout

    def join_token(self, name: str) -> str:
        output = subprocess.run(
            [self.binary, "server", "join-token", "create", "--name", name, "--data-directory", self.data, "--stdout-format", "json"],
            check=True, capture_output=True, text=True, env=self.environment,
        ).stdout  # fmt: skip
        return f"{json.loads(output)['token']}@http://{self.address}"

    def session(self, name: str) -> tuple[str, str]:
        """A session of this run and its join token."""
        session = f"{name}-{self.tag}"
        self.sessions.append(session)
        return session, self.join_token(session)

    def path(self, *elements: str) -> str:
        path = os.path.join(self.directory, *elements)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def close(self) -> None:
        for session in self.sessions:
            subprocess.run([self.binary, "drain", "--session", session], env=self.environment, capture_output=True, timeout=30, check=False)
        self.process.kill()
        self.process.wait()
        self.log.close()
        subprocess.run(["pkill", "-9", "-f", self.tag], check=False)
        shutil.rmtree(self.directory, ignore_errors=True)


@pytest.fixture
def server() -> Iterator[DevelopmentServer]:
    started = DevelopmentServer()
    try:
        yield started
    finally:
        started.close()


def rows(path: str) -> int:
    connection = sqlite3.connect(path)
    try:
        count: int = connection.execute("SELECT count(*) FROM steps").fetchone()[0]
        return count
    finally:
        connection.close()


def test_backup_restore_and_idempotent_registration(server: DevelopmentServer) -> None:
    session, token = server.session("app")
    path = server.path("app", "main.sqlite")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE steps (body TEXT)")
    connection.executemany("INSERT INTO steps VALUES (?)", [("one",), ("two",)])
    connection.commit()
    with Litebackup(session=session, api_key=token, environment=server.environment) as litebackup:
        rule = litebackup.backups.start(sqlite3_support.database_path(connection))
        assert litebackup.backups.start(path).id == rule.id
        with pytest.raises(LitebackupError) as denied:
            litebackup.backups.start(path, maximum_local_databases=4)
        assert denied.value.code == ErrorCode.RULE_EXISTS and denied.value.details["rule"]["id"] == rule.id
        status = litebackup.databases.sync(rule.name)
        assert status.synced_server.version > 0
        versions = litebackup.databases.versions(rule.name)
        assert versions and versions[-1].version == status.synced_server.version
        restored = server.path("restored", "main.sqlite")
        litebackup.databases.restore(rule.name, path=restored, version=versions[-1].version)
        assert rows(restored) == 2
        with pytest.raises(LitebackupError) as existing:
            litebackup.databases.restore(rule.name, path=restored)
        assert existing.value.code == ErrorCode.FILE_EXISTS and rows(restored) == 2
    connection.close()


def test_cross_language_handoff(server: DevelopmentServer) -> None:
    """Python writes and releases, TypeScript acquires, reads, writes, and releases, then Go does the same, then Ruby
    when LITEBACKUP_RUBY names the command that runs it, then PHP when LITEBACKUP_PHP does, then Rust when
    LITEBACKUP_RUST does."""
    name = "agents/42.sqlite"
    python_session, python_token = server.session("python")
    typescript_session, typescript_token = server.session("typescript")
    go_session, go_token = server.session("go")

    python_path = server.path("python", "42.sqlite")
    Path(python_path).touch()
    with (
        Litebackup(session=python_session, api_key=python_token, environment=server.environment) as litebackup,
        litebackup.databases.pool(adapter=sqlite3_support.adapter()) as pool,
        pool.lease(name, path=python_path, durability="cloud", offload=True) as db,
    ):
        db.execute("CREATE TABLE steps (body TEXT)")
        db.executemany("INSERT INTO steps VALUES (?)", [(f"python {index}",) for index in range(3)])
        db.commit()
        db.execute("INSERT INTO steps VALUES ('never committed')")
    with Litebackup(session=python_session, environment=server.environment) as litebackup:
        status = litebackup.databases.get(name)
        assert status.state == "offloaded" and status.synced_cloud.version == status.synced_server.version > 0

    distribution = SDK_ROOT / "typescript" / "dist"
    assert (distribution / "src" / "index.js").exists(), "build the TypeScript SDK first: cd typescript && npm run build"
    typescript_path = server.path("typescript", "42.sqlite")
    environment = {**server.environment, "LITEBACKUP_SESSION": typescript_session, "LITEBACKUP_API_KEY": typescript_token}
    step = subprocess.run(
        ["node", str(Path(__file__).parent / "handoff" / "typescript-step.mjs"), str(distribution), name, typescript_path, "3", "2"],
        env=environment, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert step.returncode == 0, step.stderr + step.stdout

    go_path = server.path("go", "42.sqlite")
    environment = {**server.environment, "LITEBACKUP_SESSION": go_session, "LITEBACKUP_API_KEY": go_token}
    step = subprocess.run(
        ["go", "run", "./litebackup/internal/handoff", name, go_path, "5", "1"],
        cwd=SDK_ROOT, env=environment, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    assert step.returncode == 0, step.stderr + step.stdout
    expected_rows = 6

    # Ruby runs in a container (LITEBACKUP_RUBY, such as sdk/ruby/scripts/ruby-in-docker), with its own daemon there.
    ruby = os.environ.get("LITEBACKUP_RUBY")
    if ruby:
        ruby_session, ruby_token = server.session("ruby")
        ruby_path = server.path("ruby", "42.sqlite")
        environment = {**server.environment, "LITEBACKUP_SESSION": ruby_session, "LITEBACKUP_API_KEY": ruby_token}
        step = subprocess.run(
            [*shlex.split(ruby), str(SDK_ROOT / "ruby" / "test" / "handoff" / "ruby_step.rb"), name, ruby_path, str(expected_rows), "2"],
            env=environment, capture_output=True, text=True, timeout=600, check=False,
        )  # fmt: skip
        assert step.returncode == 0, step.stderr + step.stdout
        expected_rows += 2

    # PHP runs in a container too (LITEBACKUP_PHP, such as sdk/php/scripts/php-in-docker), after Ruby when it ran.
    php = os.environ.get("LITEBACKUP_PHP")
    if php:
        php_session, php_token = server.session("php")
        php_path = server.path("php", "42.sqlite")
        environment = {**server.environment, "LITEBACKUP_SESSION": php_session, "LITEBACKUP_API_KEY": php_token}
        step = subprocess.run(
            [*shlex.split(php), str(SDK_ROOT / "php" / "tests" / "handoff" / "php_step.php"), name, php_path, str(expected_rows), "2"],
            env=environment, capture_output=True, text=True, timeout=600, check=False,
        )  # fmt: skip
        assert step.returncode == 0, step.stderr + step.stdout
        expected_rows += 2

    # Rust runs in a container too (LITEBACKUP_RUST, such as sdk/rust/scripts/rust-in-docker), after PHP when it ran.
    rust = os.environ.get("LITEBACKUP_RUST")
    if rust:
        rust_session, rust_token = server.session("rust")
        rust_path = server.path("rust", "42.sqlite")
        environment = {**server.environment, "LITEBACKUP_SESSION": rust_session, "LITEBACKUP_API_KEY": rust_token}
        step = subprocess.run(
            [*shlex.split(rust), "handoff_step", name, rust_path, str(expected_rows), "2"],
            env=environment, capture_output=True, text=True, timeout=900, check=False,
        )  # fmt: skip
        assert step.returncode == 0, step.stderr + step.stdout
        assert "rust handoff ok" in step.stdout, step.stdout
        expected_rows += 2
    with Litebackup(session=python_session, environment=server.environment) as litebackup:
        # The last step released the database: no machine writes it.
        assert not litebackup.databases.writer(name).active
        back = server.path("python-again", "42.sqlite")
        litebackup.databases.restore(name, path=back)
        assert rows(back) == expected_rows


async def test_async_cancelled_lease_is_released_by_close(server: DevelopmentServer) -> None:
    """A cancelled lease keeps the lock until the pool closes, which rolls back, closes, and releases it."""
    session, token = server.session("async")
    path = server.path("async", "7.sqlite")
    Path(path).touch()
    started = asyncio.Event()

    async with AsyncLitebackup(session=session, api_key=token, environment=server.environment) as litebackup:
        pool = litebackup.databases.pool(adapter=aiosqlite_support.adapter())

        async def agent() -> None:
            async with pool.lease("agents/7.sqlite", path=path, durability="server") as db:
                await db.execute("CREATE TABLE steps (body TEXT)")
                await db.commit()
                await db.execute("INSERT INTO steps VALUES ('uncommitted')")
                started.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(agent())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await litebackup.databases.writer("agents/7.sqlite")).this_machine
        await pool.close()
        writer = await litebackup.databases.writer("agents/7.sqlite")
        assert not writer.this_machine
        status = await litebackup.databases.get("agents/7.sqlite")
        assert status.state == "synced"
    assert rows(path) == 0


def test_close_failure_keeps_the_database_acquired(server: DevelopmentServer) -> None:
    session, token = server.session("close")
    path = server.path("close", "9.sqlite")
    Path(path).touch()

    class LeakyAdapter:
        def open(self, path: str) -> sqlite3.Connection:
            return sqlite3.connect(path, check_same_thread=False)

        def close(self, db: sqlite3.Connection) -> None:
            raise RuntimeError("a pooled connection is still in use")

    with Litebackup(session=session, api_key=token, environment=server.environment) as litebackup:
        pool = litebackup.databases.pool(adapter=LeakyAdapter())
        with pool.lease("agents/9.sqlite", path=path, durability="server") as db:
            db.execute("CREATE TABLE steps (body TEXT)")
            db.commit()
            leaked = db
        with pytest.raises(LeaseCleanupError) as raised:
            pool.close()
        assert raised.value.code == ErrorCode.LEASE_CLEANUP_FAILED and raised.value.step == "close"
        assert raised.value.details == {"step": "close", "name": "agents/9.sqlite", "path": path, "acquired": True}
        assert litebackup.databases.writer("agents/9.sqlite").this_machine
        leaked.close()
        litebackup.databases.release("agents/9.sqlite", durability="server")
        assert not litebackup.databases.writer("agents/9.sqlite").this_machine
