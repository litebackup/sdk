"""SQLAlchemy: the file a SQLite connection, engine, or URL opens, for registering backups.

Needs the ``litebackup[sqlalchemy]`` extra. For registering backups only: it
reads ``PRAGMA database_list`` and changes nothing.

::

    from sqlalchemy import create_engine
    from litebackup import Litebackup
    from litebackup.integrations.sqlalchemy import database_path

    engine = create_engine("sqlite:///data/app.sqlite")
    with Litebackup() as litebackup:
        litebackup.backups.start(database_path(engine))

A ``Connection`` is asked through its own DBAPI connection, inside whatever
transaction it has, without beginning, committing, or rolling back one. An
``Engine`` is asked through a new DBAPI connection made from its URL and
``connect_args``, never one from its pool. An engine with a custom ``creator``,
``async_creator``, or ``pool``, or with a ``do_connect`` event, may hand back a
connection the app is using, so it raises invalid_argument: pass a
``Connection`` from it instead. ``async_database_path`` does the same for an
``AsyncConnection`` or ``AsyncEngine``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.util.concurrency import greenlet_spawn

from ..errors import invalid_argument
from ._sqlite_uri import NO_FILE, file_from_database_list, file_from_uri, plain_file

# Query parameters SQLAlchemy's pysqlite dialects take for themselves; the rest
# go to the SQLite URI when uri=true.
_DRIVER_PARAMETERS = {"uri", "timeout", "isolation_level", "detect_types", "check_same_thread", "cached_statements"}

_CUSTOM_CONNECTIONS = (
    "makes its connections with a custom creator, async_creator, pool, or do_connect event, which may return a "
    "connection the app is using; pass a Connection from it instead"
)


def _creator_code(engine: Engine) -> object:
    return getattr(getattr(engine.pool, "_creator", None), "__code__", None)


def _creator_dialect(engine: Engine) -> object:
    """The dialect the pool's default creator connects with, which is another engine's when the pool was shared."""
    creator = getattr(engine.pool, "_creator", None)
    names = getattr(getattr(creator, "__code__", None), "co_freevars", ())
    cells = getattr(creator, "__closure__", None) or ()
    for name, cell in zip(names, cells, strict=False):
        if name == "dialect":
            return cell.cell_contents
    return None


def _default_creator_code() -> object:
    """The code of the creator create_engine makes when given none, which connects from the URL and connect_args."""
    engine = create_engine("sqlite://")
    try:
        return _creator_code(engine)
    finally:
        engine.dispose()


_DEFAULT_CREATOR_CODE = _default_creator_code()


def _read_database_list(dbapi_connection: Any) -> str:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA database_list")
        entries = cursor.fetchall()
    finally:
        cursor.close()
    return file_from_database_list([tuple(entry) for entry in entries], "main")


def _connection_path(connection: Connection) -> str:
    # The DBAPI cursor, not Connection.execute, which would begin a transaction on the Connection.
    return _read_database_list(connection.connection)


def _engine_path(engine: Engine) -> str:
    _require_sqlite(engine.url)
    if (
        _DEFAULT_CREATOR_CODE is None
        or _creator_code(engine) is not _DEFAULT_CREATOR_CODE
        # A pool taken from another engine connects through that engine's dialect and events.
        or _creator_dialect(engine) is not engine.dialect
        or engine.dialect.dispatch.do_connect
    ):
        raise invalid_argument(f"the engine for {engine.url!r} {_CUSTOM_CONNECTIONS}")
    # SQLAlchemy's own creator: dialect.connect with the URL's arguments and connect_args, outside any pool.
    dbapi_connection = engine.pool._creator()  # type: ignore[call-arg]
    try:
        return _read_database_list(dbapi_connection)
    finally:
        dbapi_connection.close()


def _require_sqlite(url: URL) -> None:
    if url.get_backend_name() != "sqlite":
        raise invalid_argument(f"expected a SQLite URL, got the {url.get_backend_name()} backend")


def database_path(source: Connection | Engine | URL | str) -> str:
    """The absolute path of the file a SQLite connection, engine, URL, or URL string opens.

    For a ``Connection`` or ``Engine`` it asks SQLite through ``PRAGMA database_list``:
    on the connection itself, leaving its transaction as it is, or on a new DBAPI
    connection made from the engine's URL and ``connect_args``. An engine with a
    custom ``creator``, ``pool``, or ``do_connect`` event raises invalid_argument; pass
    a ``Connection`` from it instead. A URL or URL string is read as the pysqlite
    dialects do: a plain database resolves against the working directory, and with
    ``?uri=true`` a ``file:`` database is a SQLite URI whose other query parameters
    apply. For an async connection or engine, use ``async_database_path``. Raises
    invalid_argument for other backends and for in-memory databases.
    """
    if isinstance(source, AsyncConnection | AsyncEngine):
        raise invalid_argument("for an async connection or engine, use await async_database_path(...)")
    if isinstance(source, Connection):
        _require_sqlite(source.engine.url)
        return _connection_path(source)
    if isinstance(source, Engine):
        return _engine_path(source)
    url = make_url(source)
    _require_sqlite(url)
    database = url.database
    if not database:
        raise invalid_argument(NO_FILE)
    query = {key: value if isinstance(value, str) else value[-1] for key, value in url.query.items()}
    if query.get("uri", "").lower() in ("true", "1", "yes") and database.startswith("file:"):
        parameters = [(key, value) for key, value in query.items() if key not in _DRIVER_PARAMETERS]
        return file_from_uri(database, parameters)
    return plain_file(database)


async def async_database_path(source: AsyncConnection | AsyncEngine) -> str:
    """The absolute path of the file a SQLite ``AsyncConnection`` or ``AsyncEngine`` opens.

    As database_path does for a ``Connection`` or ``Engine``: a connection is asked
    inside its transaction, leaving it as it is, and an engine through a new
    connection made from its URL and ``connect_args``. An engine with a custom
    ``async_creator``, ``pool``, or ``do_connect`` event raises invalid_argument;
    pass an ``AsyncConnection`` from it instead.
    """
    if isinstance(source, AsyncConnection):
        _require_sqlite(source.engine.url)
        return await source.run_sync(_connection_path)
    if isinstance(source, AsyncEngine):
        return await greenlet_spawn(_engine_path, source.sync_engine)
    raise invalid_argument("expected an AsyncConnection or AsyncEngine; for others, use database_path")
