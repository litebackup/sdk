"""An ASGI lifespan that registers continuous backups on startup, for FastAPI, Starlette, or any ASGI app.

::

    from fastapi import FastAPI
    from litebackup.integrations.fastapi import backup_lifespan

    app = FastAPI(lifespan=backup_lifespan("/var/lib/app/main.sqlite"))

Pass ``lifespan=`` to wrap an existing lifespan; it runs inside, after
registration. The client is in the lifespan state as ``litebackup`` and is
closed on exit, which never shuts down the daemon. Nothing here imports FastAPI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, TypeVar

from .._wire import PathSource
from ..client import AsyncLitebackup
from ..errors import invalid_argument

App = TypeVar("App")
Lifespan = Callable[[App], AbstractAsyncContextManager[Mapping[str, Any] | None]]


def backup_lifespan(
    *sources: PathSource,
    name: str | None = None,
    prefix: str | None = None,
    maximum_local_databases: int | None = None,
    lifespan: Lifespan[App] | None = None,
    token: str | None = None,
    session: str | None = None,
    socket_path: str | None = None,
    binary_path: str | None = None,
) -> Callable[[App], AbstractAsyncContextManager[dict[str, Any]]]:
    """A lifespan that registers each source with ``backups.start()`` before the app serves.

    Registration is idempotent, so every start of every worker may run it. ``name``
    applies to a single source; ``prefix`` and ``maximum_local_databases`` apply to each.
    """
    if not sources:
        raise invalid_argument("backup_lifespan needs at least one database path")
    if name is not None and len(sources) != 1:
        raise invalid_argument("a name applies to one source; use a prefix for several")

    @asynccontextmanager
    async def registering_lifespan(app: App) -> AsyncIterator[dict[str, Any]]:
        async with AsyncLitebackup(token=token, session=session, socket_path=socket_path, binary_path=binary_path) as litebackup:
            for source in sources:
                await litebackup.backups.start(source, name=name, prefix=prefix, maximum_local_databases=maximum_local_databases)
            state: dict[str, Any] = {"litebackup": litebackup}
            if lifespan is None:
                yield state
                return
            async with lifespan(app) as inner_state:
                yield {**state, **(inner_state or {})}

    return registering_lifespan
