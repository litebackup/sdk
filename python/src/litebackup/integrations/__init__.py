"""Driver and framework support, each imported explicitly:

- ``litebackup.integrations.sqlite3``: the file a connection has open, and a pool adapter.
- ``litebackup.integrations.aiosqlite``: the same for aiosqlite, with an async pool adapter (``litebackup[aiosqlite]``).
- ``litebackup.integrations.sqlalchemy``: the file a connection, engine, or URL opens (``litebackup[sqlalchemy]``).
- ``litebackup.integrations.django``: an installable app that registers backups on startup (``litebackup[django]``).
- ``litebackup.integrations.fastapi``: an ASGI lifespan that registers backups on startup.
"""
