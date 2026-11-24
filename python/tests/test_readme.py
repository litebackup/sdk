"""Every Python block in README.md runs on its own in an empty directory, against a fake daemon."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

import litebackup

from .contract_daemon import ContractDaemon, Received, Reply

README = Path(__file__).resolve().parents[1] / "README.md"
BLOCKS = re.findall(r"^```python\n(.*?)^```$", README.read_text(), re.MULTILINE | re.DOTALL)


def reply(received: Received) -> Reply:
    body: Any = received.json()
    if received.path == "/v1/backup-rules/start":
        answer: Any = {"id": "rule", "path": body["path"], "name": "app.sqlite", "prefix": "", "watch": "inotify", "failover": False}
    elif received.path == "/v1/databases/list":
        answer = {"databases": []}
    elif received.path == "/v1/databases/acquire":
        database = {"name": body["name"], "path": body["path"], "role": "origin", "state": "synced", "acquired": True}
        answer = {"database": database, "decision": "created"}
    elif received.path == "/v1/databases/sync":
        synced = {"version": 1}
        answer = {"name": body["name"], "path": "/x", "role": "origin", "state": "synced", "synced_server": synced, "synced_cloud": synced}
    elif received.path == "/v1/databases/release":
        database = {"name": body["name"], "path": "/x", "role": "origin", "state": "synced"}
        answer = {"database": database, "checkpointed": False, "vacuumed": False, "offloaded": False}
    else:
        return 404, "application/json", json.dumps({"code": "not_found", "message": received.path}).encode()
    return 200, "application/json", json.dumps(answer).encode()


EXTRAS = ("aiosqlite", "sqlalchemy", "django")


def test_readme_names_the_extra_each_block_needs() -> None:
    """A block importing an integration with an extra is preceded, in its section, by that extra's install."""
    text = README.read_text()
    for match in re.finditer(r"^```python\n(.*?)^```$", text, re.MULTILINE | re.DOTALL):
        section = text[: match.start()].rsplit("\n#", 1)[-1]
        for extra in EXTRAS:
            if f"litebackup.integrations.{extra}" in match.group(1):
                assert f"litebackup[{extra}]" in section, f"a block imports litebackup.integrations.{extra} without naming its extra"


def test_readme_has_python_blocks() -> None:
    assert len(BLOCKS) >= 4


@pytest.mark.parametrize("index", range(len(BLOCKS)))
def test_readme_block_runs_alone(index: int, cwd: str, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = ContractDaemon(reply)
    sync_client, async_client = litebackup.Litebackup, litebackup.AsyncLitebackup
    monkeypatch.setattr(litebackup, "Litebackup", lambda **options: sync_client(socket_path=daemon.socket_path, **options))
    monkeypatch.setattr(litebackup, "AsyncLitebackup", lambda **options: async_client(socket_path=daemon.socket_path, **options))
    try:
        exec(compile(BLOCKS[index], f"README.md block {index + 1}", "exec"), {"__name__": "__main__"})
        assert daemon.received, "the block made no requests"
    finally:
        daemon.close()
    daemon.check_sdk_version()
