"""Django and FastAPI startup, each in its own process, registering backups with a fake daemon."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from collections.abc import Iterator

import pytest

from .contract_daemon import ContractDaemon, Received, Reply

RULE = {
    "id": "rule-1",
    "path": "",
    "name": "main.sqlite",
    "prefix": "",
    "watch": "inotify",
    "maximum_local_databases": 0,
    "failover": False,
}


@pytest.fixture
def daemon() -> Iterator[ContractDaemon]:
    def reply(received: Received) -> Reply:
        body = received.json()
        assert isinstance(body, dict)
        return 200, "application/json", json.dumps({**RULE, "path": body["path"]}).encode()

    started = ContractDaemon(reply)
    yield started
    started.check_sdk_version()
    started.close()


def setup_django(cwd: str, settings: str) -> subprocess.CompletedProcess[str]:
    with open(os.path.join(cwd, "project_settings.py"), "w") as file:
        file.write(textwrap.dedent(settings))
    environment = {**os.environ, "DJANGO_SETTINGS_MODULE": "project_settings", "PYTHONPATH": cwd}
    return subprocess.run(
        [sys.executable, "-c", "import django; django.setup(); print('ready')"],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def django_settings(daemon: ContractDaemon, name: str, litebackup: str) -> str:
    return f"""
        SECRET_KEY = "test"
        INSTALLED_APPS = ["litebackup.integrations.django"]
        DATABASES = {{"default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": {name}}}}}
        LITEBACKUP = {{"socket_path": {daemon.socket_path!r}, **{litebackup}}}
    """


def test_django_registers_on_startup_and_again(cwd: str, daemon: ContractDaemon) -> None:
    settings = django_settings(daemon, "BASE / 'data' / 'app.sqlite'", '{"databases": {"default": {"prefix": "web/"}}}')
    settings = "from pathlib import Path\nBASE = Path(__file__).resolve().parent\n" + textwrap.dedent(settings)
    os.mkdir(os.path.join(cwd, "data"))
    for _start in range(2):
        completed = setup_django(cwd, settings)
        assert completed.returncode == 0, completed.stderr
        assert "ready" in completed.stdout
    starts = [received.json() for received in daemon.received if received.path == "/v1/backup-rules/start"]
    assert starts == [{"path": os.path.join(cwd, "data", "app.sqlite"), "prefix": "web/"}] * 2
    # Before the first migrate, the app created the empty database file SQLite would.
    assert os.path.getsize(os.path.join(cwd, "data", "app.sqlite")) == 0


def test_django_denies_memory_and_skips_when_disabled(cwd: str, daemon: ContractDaemon) -> None:
    completed = setup_django(cwd, django_settings(daemon, "':memory:'", '{"databases": {"default": {}}}'))
    assert completed.returncode != 0
    assert "ImproperlyConfigured" in completed.stderr and "in-memory" in completed.stderr
    completed = setup_django(cwd, django_settings(daemon, "'file:memdb?mode=memory&cache=shared'", '{"databases": {"default": {}}}'))
    assert completed.returncode != 0 and "ImproperlyConfigured" in completed.stderr
    completed = setup_django(cwd, django_settings(daemon, "'app.sqlite'", '{"enabled": False, "databases": {"default": {}}}'))
    assert completed.returncode == 0, completed.stderr
    completed = setup_django(cwd, django_settings(daemon, "'app.sqlite'", '{"databases": {"other": {}}}'))
    assert completed.returncode != 0 and "not in DATABASES" in completed.stderr
    completed = setup_django(cwd, django_settings(daemon, "'missing/app.sqlite'", '{"databases": {"default": {}}}'))
    assert completed.returncode != 0 and "does not exist" in completed.stderr
    assert daemon.received == []


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def test_fastapi_lifespan_registers_then_runs_the_app_lifespan(cwd: str, daemon: ContractDaemon) -> None:
    with open(os.path.join(cwd, "service.py"), "w") as file:
        file.write(
            textwrap.dedent(
                f"""
                from contextlib import asynccontextmanager
                from fastapi import FastAPI, Request
                from litebackup.integrations.fastapi import backup_lifespan

                import os

                @asynccontextmanager
                async def own_lifespan(app):
                    os.makedirs("data", exist_ok=True)
                    open("data/main.sqlite", "a").close()  # the app creates its database before registration
                    yield {{"own": "ran"}}

                app = FastAPI(lifespan=backup_lifespan("data/main.sqlite", socket_path={daemon.socket_path!r}, lifespan=own_lifespan))

                @app.get("/state")
                async def state(request: Request):
                    return {{"own": request.state.own, "client": type(request.state.litebackup).__name__}}
                """
            )
        )
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "service:app", "--port", str(port), "--lifespan", "on"],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=1) as response:
                    state = json.load(response)
                break
            except OSError:
                assert server.poll() is None, server.stdout.read() if server.stdout else ""
                assert time.monotonic() < deadline, "uvicorn never served"
                time.sleep(0.1)
        assert state == {"own": "ran", "client": "AsyncLitebackup"}
        starts = [received.json() for received in daemon.received if received.path == "/v1/backup-rules/start"]
        assert starts == [{"path": os.path.join(cwd, "data", "main.sqlite")}]
    finally:
        server.terminate()
        output, _ = server.communicate(timeout=30)
    assert "Application shutdown complete" in output, output
