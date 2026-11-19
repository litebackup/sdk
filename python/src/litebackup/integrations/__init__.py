"""Driver and framework support, each imported explicitly:

- ``litebackup.integrations.sqlite3``: the file a connection has open, and a managed-database adapter.
- ``litebackup.integrations.aiosqlite``: the same for aiosqlite (``litebackup[aiosqlite]``).
- ``litebackup.integrations.sqlalchemy``: the file an engine or URL opens (``litebackup[sqlalchemy]``).
- ``litebackup.integrations.django``: an installable app that registers backups on startup (``litebackup[django]``).
- ``litebackup.integrations.fastapi``: an ASGI lifespan that registers backups on startup.
"""
