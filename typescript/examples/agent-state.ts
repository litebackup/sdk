// One agent run: take its database, work on it, and hand it back for the next machine.
import { mkdir } from "node:fs/promises";
import { join } from "node:path";

import { Litebackup } from "../src/index.js";
import { adapter } from "../src/node-sqlite.js";

const litebackup = new Litebackup();
const agents = process.env.AGENT_DIRECTORY ?? "/var/lib/app/agents";

export async function runAgent(agentId: string, step: string): Promise<number> {
  await mkdir(agents, { recursive: true });
  return litebackup.databases.managed(
    `agents/${agentId}.sqlite`,
    { to: join(agents, `${agentId}.sqlite`), durability: "cloud", adapter: adapter() },
    (db) => {
      db.exec("CREATE TABLE IF NOT EXISTS steps (id INTEGER PRIMARY KEY, body TEXT NOT NULL)");
      // The adapter rolls back anything not committed when the scope closes.
      db.exec("BEGIN");
      db.prepare("INSERT INTO steps (body) VALUES (?)").run(step);
      db.exec("COMMIT");
      return (db.prepare("SELECT count(*) AS steps FROM steps").get() as { steps: number }).steps;
    },
  );
}

if (process.argv[2] && process.argv[3]) {
  try {
    console.log(await runAgent(process.argv[2], process.argv[3]), "steps");
  } finally {
    litebackup.close();
  }
}
