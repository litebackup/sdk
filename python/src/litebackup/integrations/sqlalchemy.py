"""SQLAlchemy: the file a SQLite engine or URL opens, for registering backups.

Needs the ``litebackup[sqlalchemy]`` extra. Registration only: it never touches
the engine's pool.

::

    from sqlalchemy import create_engine
    from litebackup.integrations.sqlalchemy import database_path

    engine = create_engine("sqlite:///data/app.sqlite")
    litebackup.backups.start(database_path(engine))
"""

from __future__ import annotations

from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from ..errors import invalid_argument
from ._sqlite_uri import NO_FILE, file_from_uri, plain_file

# Query parameters SQLAlchemy's pysqlite dialects take for themselves; the rest
# go to the SQLite URI when uri=true.
_DRIVER_PARAMETERS = {"uri", "timeout", "isolation_level", "detect_types", "check_same_thread", "cached_statements"}


def database_path(source: Engine | AsyncEngine | URL | str) -> str:
    """The absolute path of the file a SQLite engine, async engine, URL, or URL string opens.

    Follows the pysqlite dialects: a plain database resolves against the working
    directory, and with ``?uri=true`` a ``file:`` database is a SQLite URI whose other
    query parameters apply. Raises invalid_argument for other backends and for
    in-memory databases.
    """
    url = make_url(source) if isinstance(source, str | URL) else source.url
    if url.get_backend_name() != "sqlite":
        raise invalid_argument(f"expected a SQLite URL, got the {url.get_backend_name()} backend")
    database = url.database
    if not database:
        raise invalid_argument(NO_FILE)
    query = {key: value if isinstance(value, str) else value[-1] for key, value in url.query.items()}
    if query.get("uri", "").lower() in ("true", "1", "yes") and database.startswith("file:"):
        parameters = [(key, value) for key, value in query.items() if key not in _DRIVER_PARAMETERS]
        return file_from_uri(database, parameters)
    return plain_file(database)
