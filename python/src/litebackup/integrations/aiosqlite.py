"""aiosqlite: the file a connection has open, and an async managed-database adapter.

Needs the ``litebackup[aiosqlite]`` extra.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from .._managed import AsyncScopeAdapter
from ._sqlite_uri import file_from_database_list


async def database_path(connection: aiosqlite.Connection, schema: str = "main") -> str:
    """The absolute path of the file a connection has open, or of an attached database; see the sqlite3 integration."""
    async with connection.execute("PRAGMA database_list") as cursor:
        rows = await cursor.fetchall()
    return file_from_database_list([tuple(row) for row in rows], schema)


class _AiosqliteAdapter:
    def __init__(self, connect_options: dict[str, Any]) -> None:
        self._connect_options = connect_options

    async def open(self, path: str) -> aiosqlite.Connection:
        return await aiosqlite.connect(path, **self._connect_options)

    async def close(self, connection: aiosqlite.Connection) -> None:
        try:
            if connection.in_transaction:
                await connection.rollback()
        finally:
            await connection.close()


def adapter(**connect_options: Any) -> AsyncScopeAdapter[aiosqlite.Connection]:
    """An async managed-database adapter: opens with ``aiosqlite.connect(path, **connect_options)``,
    and closing rolls back what the block left uncommitted, never commits it, and closes."""
    return _AiosqliteAdapter(connect_options)
