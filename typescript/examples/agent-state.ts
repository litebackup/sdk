// One agent step: lease its database from a pool, save the step, and close
// the pool, which releases the database for the next machine.
import { join } from "node:path";

import { Litebackup } from "../src/index.js";
import { adapter } from "../src/node-sqlite.js";

const litebackup = new Litebackup();
const agents = process.env.AGENT_DIRECTORY ?? "/var/lib/app/agents";
const pool = litebackup.databases.pool({ adapter: adapter() });

export async function saveAgentStep(agentId: string, step: string): Promise<number> {
  // The first lease creates the directory and an empty file when nothing is
  // there yet, and keeps the write lock for later steps on this machine.
  return pool.lease(
    `agents/${agentId}.sqlite`,
    // Wait up to a minute while another machine running this agent has the lock.
    { path: join(agents, `${agentId}.sqlite`), lockTimeout: "1m" },
    (db) => {
      // The adapter rolls back anything not committed when the pool closes it.
      db.exec("BEGIN");
      db.exec("CREATE TABLE IF NOT EXISTS steps (id INTEGER PRIMARY KEY, body TEXT NOT NULL)");
      db.prepare("INSERT INTO steps (body) VALUES (?)").run(step);
      db.exec("COMMIT");
      return (db.prepare("SELECT count(*) AS steps FROM steps").get() as { steps: number }).steps;
    },
  );
}

if (process.argv[2] && process.argv[3]) {
  try {
    console.log(await saveAgentStep(process.argv[2], process.argv[3]), "steps");
  } finally {
    // Closing the pool releases the lease; a failed release is a LeaseCleanupError naming its step.
    await pool.close();
    litebackup.close();
  }
}
