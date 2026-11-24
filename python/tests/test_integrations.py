"""Driver integrations against the real sqlite3, aiosqlite, and SQLAlchemy."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

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


def test_sqlite3_adapter_opens_existing_files_only(cwd: str) -> None:
    adapter = sqlite3_support.adapter(timeout=7)
    for name in ("missing.sqlite", "odd ?#%name.sqlite"):
        with pytest.raises(LitebackupError) as missing:
            adapter.open(os.path.join(cwd, name))
        assert missing.value.code == "not_found"
        assert not os.path.exists(name)
    Path("odd ?#%name.sqlite").touch()
    connection = adapter.open(os.path.join(cwd, "odd ?#%name.sqlite"))
    assert sqlite3_support.database_path(connection) == os.path.join(cwd, "odd ?#%name.sqlite")
    # Pools close a connection on another thread than the one that opened it.
    closing = threading.Thread(target=adapter.close, args=(connection,))
    closing.start()
    closing.join()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_sqlite3_adapter_rolls_back(cwd: str) -> None:
    adapter = sqlite3_support.adapter()
    Path("scope.sqlite").touch()
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
    with pytest.raises(LitebackupError) as missing:
        await adapter.open(os.path.join(cwd, "scope.sqlite"))
    assert missing.value.code == "not_found" and not os.path.exists("scope.sqlite")
    Path("scope.sqlite").touch()
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
            connection.execute(text("CREATE TABLE written (id INTEGER)"))
        engine.dispose()
        check = sqlite3.connect(expected)
        assert check.execute("SELECT count(*) FROM sqlite_schema WHERE name = 'written'").fetchone() == (1,), url
        check.close()
        os.remove(expected)


async def test_sqlalchemy_async_engine_and_connection(cwd: str) -> None:
    async_engine = create_async_engine("sqlite+aiosqlite:///async.sqlite")
    try:
        assert await sqlalchemy_support.async_database_path(async_engine) == os.path.join(cwd, "async.sqlite")
        async with async_engine.connect() as connection:
            assert await sqlalchemy_support.async_database_path(connection) == os.path.join(cwd, "async.sqlite")
            assert not connection.in_transaction()
            await connection.execute(text("CREATE TABLE t (x)"))
            await connection.execute(text("INSERT INTO t VALUES (1)"))
            assert await sqlalchemy_support.async_database_path(connection) == os.path.join(cwd, "async.sqlite")
            assert connection.in_transaction()
            assert (await connection.execute(text("SELECT count(*) FROM t"))).scalar() == 1
            await connection.commit()
        for source in (async_engine, connection):
            with pytest.raises(LitebackupError) as error:
                sqlalchemy_support.database_path(source)  # type: ignore[arg-type]
            assert invalid(error)
    finally:
        await async_engine.dispose()


async def test_sqlalchemy_async_creator_is_not_guessed_from_the_url(cwd: str) -> None:
    async_engine = create_async_engine("sqlite+aiosqlite:///named.sqlite", async_creator=lambda: aiosqlite.connect("opened.sqlite"))
    try:
        with pytest.raises(LitebackupError) as error:
            await sqlalchemy_support.async_database_path(async_engine)
        assert invalid(error)
        async with async_engine.connect() as connection:
            assert await sqlalchemy_support.async_database_path(connection) == os.path.join(cwd, "opened.sqlite")
    finally:
        await async_engine.dispose()


def test_sqlalchemy_engine_sharing_another_engines_pool_needs_a_connection(cwd: str) -> None:
    shared = sqlite3.connect("shared.sqlite", check_same_thread=False)
    shared.execute("CREATE TABLE t (x)")
    shared.execute("INSERT INTO t VALUES (1)")  # uncommitted
    owner = create_engine("sqlite:///shared.sqlite")
    event.listen(owner, "do_connect", lambda _dialect, _pool_entry, _cargs, _cparams: shared)
    borrower = create_engine("sqlite:///shared.sqlite", pool=owner.pool)
    try:
        with pytest.raises(LitebackupError) as error:
            sqlalchemy_support.database_path(borrower)
        assert invalid(error)
        assert shared.execute("SELECT count(*) FROM t").fetchone() == (1,)
    finally:
        shared.close()


def test_sqlalchemy_custom_creators_need_a_connection(cwd: str) -> None:
    engine = create_engine("sqlite:///named.sqlite", creator=lambda: sqlite3.connect("opened.sqlite"))
    try:
        with pytest.raises(LitebackupError) as error:
            sqlalchemy_support.database_path(engine)
        assert invalid(error)
        with engine.connect() as connection:
            assert sqlalchemy_support.database_path(connection) == os.path.join(cwd, "opened.sqlite")
    finally:
        engine.dispose()
    hooked = create_engine("sqlite:///named.sqlite")
    event.listen(hooked, "do_connect", lambda _dialect, _pool_entry, _cargs, _cparams: sqlite3.connect("opened.sqlite"))
    try:
        with pytest.raises(LitebackupError) as error:
            sqlalchemy_support.database_path(hooked)
        assert invalid(error)
    finally:
        hooked.dispose()
    memory = create_engine("sqlite:///named.sqlite", creator=lambda: sqlite3.connect(":memory:"))
    try:
        with memory.connect() as connection, pytest.raises(LitebackupError) as error:
            sqlalchemy_support.database_path(connection)
        assert invalid(error)
    finally:
        memory.dispose()


def test_sqlalchemy_shared_creator_connection_is_left_alone(cwd: str) -> None:
    app = sqlite3.connect("app.sqlite", check_same_thread=False)
    app.execute("CREATE TABLE t (x)")
    app.commit()
    app.execute("INSERT INTO t VALUES (1)")
    engine = create_engine("sqlite:///named.sqlite", poolclass=StaticPool, creator=lambda: app)
    try:
        with pytest.raises(LitebackupError) as error:
            sqlalchemy_support.database_path(engine)
        assert invalid(error)
        assert app.in_transaction
        assert app.execute("SELECT count(*) FROM t").fetchone() == (1,)
    finally:
        engine.dispose()
    app.commit()
    check = sqlite3.connect("app.sqlite")
    assert check.execute("SELECT count(*) FROM t").fetchone() == (1,)
    check.close()
    app.close()


def test_sqlalchemy_connection_transaction_state_is_unchanged(cwd: str) -> None:
    engine = create_engine("sqlite:///state.sqlite")
    try:
        with engine.connect() as connection:
            assert sqlalchemy_support.database_path(connection) == os.path.join(cwd, "state.sqlite")
            assert not connection.in_transaction()
            connection.execute(text("CREATE TABLE t (x)"))
            connection.execute(text("INSERT INTO t VALUES (1)"))
            dbapi = connection.connection.dbapi_connection
            assert dbapi is not None and dbapi.in_transaction
            assert sqlalchemy_support.database_path(connection) == os.path.join(cwd, "state.sqlite")
            assert connection.in_transaction() and dbapi.in_transaction
            connection.commit()
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM t")).scalar() == 1
    finally:
        engine.dispose()


def test_sqlalchemy_engine_path_leaves_open_transactions_alone(cwd: str) -> None:
    for poolclass in (StaticPool, None):
        engine = create_engine("sqlite:///shared.sqlite", poolclass=poolclass)
        try:
            with engine.connect() as connection:
                connection.execute(text("CREATE TABLE IF NOT EXISTS t (x)"))
                connection.commit()
                connection.execute(text("INSERT INTO t VALUES (1)"))
                assert sqlalchemy_support.database_path(engine) == os.path.join(cwd, "shared.sqlite")
                assert connection.execute(text("SELECT count(*) FROM t")).scalar() == 1
                connection.commit()
            with engine.connect() as connection:
                assert connection.execute(text("SELECT count(*) FROM t")).scalar() == 1
                connection.execute(text("DELETE FROM t"))
                connection.commit()
        finally:
            engine.dispose()


def test_sqlalchemy_refuses_memory_and_foreign_backends() -> None:
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
