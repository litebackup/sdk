"""One agent run: lease its database, save a step, and hand it back for the next machine.

Run: python agent_state.py AGENT-ID STEP
"""

from __future__ import annotations

import os
import sqlite3
import sys

from litebackup import Litebackup, Pool
from litebackup.integrations.sqlite3 import adapter

AGENTS = os.environ.get("AGENT_DIRECTORY", "/var/lib/app/agents")


def save_agent_step(pool: Pool[sqlite3.Connection], agent_id: str, step: str) -> int:
    with pool.lease(
        f"agents/{agent_id}.sqlite",
        path=os.path.join(AGENTS, f"{agent_id}.sqlite"),
        durability="server",
        lock_timeout=30,  # Waits while another machine's run still has the database.
    ) as db:
        db.execute("CREATE TABLE IF NOT EXISTS steps (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("INSERT INTO steps (body) VALUES (?)", (step,))
        db.commit()  # The adapter rolls back anything not committed when the pool closes the connection.
        steps: int = db.execute("SELECT count(*) FROM steps").fetchone()[0]
        return steps


if __name__ == "__main__":
    # Closing the pool releases the lease, so the next machine can acquire the database.
    with Litebackup() as client, client.databases.pool(adapter=adapter()) as agent_pool:
        print(save_agent_step(agent_pool, sys.argv[1], sys.argv[2]), "steps")
