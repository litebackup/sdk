"""The asyncio form of agent_state.py, with aiosqlite."""

from __future__ import annotations

import asyncio
import os
import sys

import aiosqlite

from litebackup import AsyncLitebackup, AsyncPool
from litebackup.integrations.aiosqlite import adapter


async def save_agent_step(pool: AsyncPool[aiosqlite.Connection], agent_id: str, step: str) -> None:
    async with pool.lease(
        f"agents/{agent_id}.sqlite",
        path=os.path.join(os.environ.get("AGENT_DIRECTORY", "/var/lib/app/agents"), f"{agent_id}.sqlite"),
        durability="server",
        lock_timeout=30,  # Waits while another machine's run still has the database.
    ) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS steps (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        await db.execute("INSERT INTO steps (body) VALUES (?)", (step,))
        await db.commit()


async def main() -> None:
    # Closing the pool closes the connection and releases the lease, even after a cancelled step.
    async with AsyncLitebackup() as litebackup, litebackup.databases.pool(adapter=adapter()) as agent_pool:
        await save_agent_step(agent_pool, sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    asyncio.run(main())
