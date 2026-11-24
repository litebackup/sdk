"""The standard library's sqlite3: the file a connection has open, and a pool adapter.

::

    import sqlite3
    from litebackup.integrations.sqlite3 import database_path

    connection = sqlite3.connect("data/app.sqlite")
    litebackup.backups.start(database_path(connection))
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .._pool import Adapter
from ._sqlite_uri import existing_file_uri, file_from_database_list, require_existing_file


def database_path(connection: sqlite3.Connection, schema: str = "main") -> str:
    """The absolute path of the file a connection has open, or of an attached database.

    Reads ``PRAGMA database_list``, which starts no transaction. Raises
    invalid_argument for in-memory and temporary databases.
    """
    return file_from_database_list(connection.execute("PRAGMA database_list").fetchall(), schema)


class _Sqlite3Adapter:
    def __init__(self, connect_options: dict[str, Any]) -> None:
        self._connect_options = {"check_same_thread": False, **connect_options, "uri": True}

    def open(self, path: str) -> sqlite3.Connection:
        require_existing_file(path)
        connection: sqlite3.Connection = sqlite3.connect(existing_file_uri(path), **self._connect_options)
        return connection

    def close(self, db: sqlite3.Connection) -> None:
        try:
            if db.in_transaction:
                db.rollback()
        finally:
            db.close()


def adapter(**connect_options: Any) -> Adapter[sqlite3.Connection]:
    """A pool adapter: opens an existing file with ``sqlite3.connect(uri, uri=True, **connect_options)``.

    The URI is the file's ``file:`` URI with ``mode=rw``, so a missing file is never
    created: it raises LitebackupError with code not_found. ``check_same_thread``
    is False unless connect_options sets it, because a pool may open, use, and close
    a connection on different threads; use it from one thread at a time. Closing
    rolls back a transaction the block left uncommitted, never commits it, and
    closes the connection. Commit what the block should keep.
    """
    return _Sqlite3Adapter(connect_options)
