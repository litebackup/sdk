"""One agent run: take its database, record a step, and hand it back for the next machine.

Run: python agent_state.py AGENT-ID STEP
"""

from __future__ import annotations

import os
import sys

from litebackup import Litebackup
from litebackup.integrations.sqlite3 import adapter

AGENTS = os.environ.get("AGENT_DIRECTORY", "/var/lib/app/agents")


def run_agent(litebackup: Litebackup, agent_id: str, step: str) -> int:
    os.makedirs(AGENTS, exist_ok=True)
    with litebackup.databases.managed(
        f"agents/{agent_id}.sqlite",
        to=os.path.join(AGENTS, f"{agent_id}.sqlite"),
        durability="cloud",
        adapter=adapter(),
    ) as db:
        db.execute("CREATE TABLE IF NOT EXISTS steps (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("INSERT INTO steps (body) VALUES (?)", (step,))
        db.commit()  # The adapter rolls back anything not committed when the scope closes.
        steps: int = db.execute("SELECT count(*) FROM steps").fetchone()[0]
        return steps


if __name__ == "__main__":
    with Litebackup() as client:
        print(run_agent(client, sys.argv[1], sys.argv[2]), "steps")
