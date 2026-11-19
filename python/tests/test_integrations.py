"""Driver integrations against the real sqlite3, aiosqlite, and SQLAlchemy."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from litebackup import LitebackupError
from litebackup.integrations import aiosqlite as aiosqlite_support
from litebackup.integrations import sqlalchemy as sqlalchemy_support
from litebackup.integrations import sqlite3 as sqlite3_support


def invalid(error: pytest.ExceptionInfo[LitebackupError]) -> bool:
    return error.value.code == "invalid_argument"


def test_sqlite3_paths(cwd: str) -> None:
    connection = sqlite3.connect("data.sqlite")
    try:
        assert sqlite3_support.database_path(connection) == os.path.join(cwd, "data.sqlite")
        connection.execute("ATTACH DATABASE 'other.sqlite' AS other")
        connection.execute("ATTACH DATABASE ':memory:' AS scratch")
        assert sqlite3_support.database_path(connection, "other") == os.path.join(cwd, "other.sqlite")
        assert not connection.in_transaction
        for schema in ("scratch", "temp", "missing"):
            with pytest.raises(LitebackupError) as error:
                sqlite3_support.database_path(connection, schema)
            assert invalid(error)
    finally:
        connection.close()
    for name in (":memory:", ""):
        memory = sqlite3.connect(name)
        with pytest.raises(LitebackupError) as error:
            sqlite3_support.database_path(memory)
        assert invalid(error)
        memory.close()
    uri = sqlite3.connect("file:uri%20dir.sqlite?mode=rwc", uri=True)
    assert sqlite3_support.database_path(uri) == os.path.join(cwd, "uri dir.sqlite")
    uri.close()


def test_sqlite3_adapter_rolls_back(cwd: str) -> None:
    adapter = sqlite3_support.adapter()
    connection = adapter.open(os.path.join(cwd, "scope.sqlite"))
    connection.execute("CREATE TABLE steps (id INTEGER)")
    connection.commit()
    connection.execute("INSERT INTO steps VALUES (1)")
    assert connection.in_transaction
    adapter.close(connection)
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    check = sqlite3.connect("scope.sqlite")
    assert check.execute("SELECT count(*) FROM steps").fetchone() == (0,)
    check.close()


async def test_aiosqlite(cwd: str) -> None:
    async with aiosqlite.connect("data.sqlite") as connection:
        assert await aiosqlite_support.database_path(connection) == os.path.join(cwd, "data.sqlite")
        await connection.execute("ATTACH DATABASE 'other.sqlite' AS other")
        assert await aiosqlite_support.database_path(connection, "other") == os.path.join(cwd, "other.sqlite")
    async with aiosqlite.connect(":memory:") as memory:
        with pytest.raises(LitebackupError):
            await aiosqlite_support.database_path(memory)
    adapter = aiosqlite_support.adapter()
    connection = await adapter.open(os.path.join(cwd, "scope.sqlite"))
    await connection.execute("CREATE TABLE steps (id INTEGER)")
    await connection.commit()
    await connection.execute("INSERT INTO steps VALUES (1)")
    assert connection.in_transaction
    await adapter.close(connection)
    async with aiosqlite.connect("scope.sqlite") as check, check.execute("SELECT count(*) FROM steps") as cursor:
        assert await cursor.fetchone() == (0,)


def test_sqlalchemy_resolves_the_file_the_engine_opens(cwd: str) -> None:
    os.mkdir("data dir")
    cases = {
        "sqlite:///plain.sqlite": "plain.sqlite",
        "sqlite+pysqlite:///./data dir/../relative.sqlite": "relative.sqlite",
        f"sqlite:///{cwd}/absolute.sqlite": "absolute.sqlite",
        "sqlite:///file:uri.sqlite?uri=true": "uri.sqlite",
        "sqlite:///file:data%20dir/escaped.sqlite?uri=true&mode=rwc&timeout=5": "data dir/escaped.sqlite",
        "sqlite:///literal%20name.sqlite": "literal%20name.sqlite",
        "sqlite:///notauri.sqlite?timeout=5": "notauri.sqlite",
    }
    for url, file in cases.items():
        expected = os.path.join(cwd, file)
        engine = create_engine(url)
        assert sqlalchemy_support.database_path(engine) == expected, url
        assert sqlalchemy_support.database_path(url) == expected
        assert sqlalchemy_support.database_path(engine.url) == expected
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE marker (id INTEGER)"))
        engine.dispose()
        check = sqlite3.connect(expected)
        assert check.execute("SELECT count(*) FROM sqlite_schema WHERE name = 'marker'").fetchone() == (1,), url
        check.close()
        os.remove(expected)
    async_engine = create_async_engine("sqlite+aiosqlite:///async.sqlite")
    assert sqlalchemy_support.database_path(async_engine) == os.path.join(cwd, "async.sqlite")


def test_sqlalchemy_refuses_memory_and_other_backends() -> None:
    for url in (
        "sqlite://",
        "sqlite:///:memory:",
        "sqlite:///file::memory:?uri=true",
        "sqlite:///file:named?mode=memory&cache=shared&uri=true",
        "sqlite:///file:x.db?vfs=memdb&uri=true",
        "postgresql://localhost/app",
    ):
        with pytest.raises(LitebackupError) as error:
            sqlalchemy_support.database_path(url)
        assert invalid(error), url


def test_paths_accept_path_like(cwd: str) -> None:
    from litebackup._wire import absolute_path

    assert absolute_path(Path("a") / ".." / "b.sqlite") == os.path.join(cwd, "b.sqlite")
    with pytest.raises(LitebackupError):
        absolute_path(b"bytes.sqlite")  # type: ignore[arg-type]
