"""The standard library's sqlite3: the file a connection has open, and a managed-database adapter.

::

    import sqlite3
    from litebackup.integrations.sqlite3 import database_path

    connection = sqlite3.connect("data/app.sqlite")
    litebackup.backups.start(database_path(connection))
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .._managed import ScopeAdapter
from ._sqlite_uri import file_from_database_list


def database_path(connection: sqlite3.Connection, schema: str = "main") -> str:
    """The absolute path of the file a connection has open, or of an attached database.

    Reads ``PRAGMA database_list``, which starts no transaction. Raises
    invalid_argument for in-memory and temporary databases.
    """
    return file_from_database_list(connection.execute("PRAGMA database_list").fetchall(), schema)


class _Sqlite3Adapter:
    def __init__(self, connect_options: dict[str, Any]) -> None:
        self._connect_options = connect_options

    def open(self, path: str) -> sqlite3.Connection:
        connection: sqlite3.Connection = sqlite3.connect(path, **self._connect_options)
        return connection

    def close(self, connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()


def adapter(**connect_options: Any) -> ScopeAdapter[sqlite3.Connection]:
    """A managed-database adapter: opens with ``sqlite3.connect(path, **connect_options)``.

    Closing rolls back a transaction the block left uncommitted, never commits it,
    and closes the connection. Commit what the block should keep.
    """
    return _Sqlite3Adapter(connect_options)
