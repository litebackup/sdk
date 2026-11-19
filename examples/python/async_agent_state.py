"""The asyncio form of agent_state.py, with aiosqlite."""

from __future__ import annotations

import asyncio
import os
import sys

from litebackup import AsyncLitebackup
from litebackup.integrations.aiosqlite import adapter


async def run_agent(litebackup: AsyncLitebackup, agent_id: str, step: str) -> None:
    async with litebackup.databases.managed(
        f"agents/{agent_id}.sqlite",
        to=os.path.join(os.environ.get("AGENT_DIRECTORY", "/var/lib/app/agents"), f"{agent_id}.sqlite"),
        durability="cloud",
        adapter=adapter(),
    ) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS steps (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        await db.execute("INSERT INTO steps (body) VALUES (?)", (step,))
        await db.commit()


async def main() -> None:
    async with AsyncLitebackup() as litebackup:
        # Cancelling the task still closes and releases the database before the cancellation propagates.
        await run_agent(litebackup, sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    asyncio.run(main())
