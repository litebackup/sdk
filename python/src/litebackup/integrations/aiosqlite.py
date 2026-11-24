"""aiosqlite: the file a connection has open, and an async pool adapter.

Needs the ``litebackup[aiosqlite]`` extra.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from .._pool import AsyncAdapter
from ._sqlite_uri import existing_file_uri, file_from_database_list, require_existing_file


async def database_path(connection: aiosqlite.Connection, schema: str = "main") -> str:
    """The absolute path of the file a connection has open, or of an attached database; see the sqlite3 integration."""
    async with connection.execute("PRAGMA database_list") as cursor:
        entries = await cursor.fetchall()
    return file_from_database_list([tuple(entry) for entry in entries], schema)


class _AiosqliteAdapter:
    def __init__(self, connect_options: dict[str, Any]) -> None:
        self._connect_options = {**connect_options, "uri": True}

    async def open(self, path: str) -> aiosqlite.Connection:
        require_existing_file(path)
        return await aiosqlite.connect(existing_file_uri(path), **self._connect_options)

    async def close(self, db: aiosqlite.Connection) -> None:
        try:
            if db.in_transaction:
                await db.rollback()
        finally:
            await db.close()


def adapter(**connect_options: Any) -> AsyncAdapter[aiosqlite.Connection]:
    """An async pool adapter: opens an existing file with ``aiosqlite.connect(uri, uri=True, **connect_options)``.

    As the sqlite3 adapter does, it opens the file's ``file:`` URI with ``mode=rw`` and
    raises not_found for a missing file; closing rolls back what the block left
    uncommitted, never commits it, and closes.
    """
    return _AiosqliteAdapter(connect_options)
