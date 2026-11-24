"""A FastAPI app on SQLAlchemy whose database is registered for backup before it serves.

Run: uvicorn app:app
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from sqlalchemy import create_engine, text

from litebackup.integrations.fastapi import backup_lifespan
from litebackup.integrations.sqlalchemy import database_path

os.makedirs("data", exist_ok=True)
engine = create_engine(os.environ.get("DATABASE_URL", "sqlite:///data/app.sqlite"))


@asynccontextmanager
async def app_lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS visits (id INTEGER PRIMARY KEY)"))
    yield {}
    engine.dispose()


# Registration runs first and is idempotent across restarts and workers; the app's own lifespan runs inside it.
app = FastAPI(lifespan=backup_lifespan(database_path(engine), lifespan=app_lifespan))


@app.post("/visits")
async def visit(request: Request) -> dict[str, int]:
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO visits DEFAULT VALUES"))
        count = connection.execute(text("SELECT count(*) FROM visits")).scalar_one()
    return {"visits": count}
