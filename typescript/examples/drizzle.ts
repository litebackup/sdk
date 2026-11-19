// Back up the database a Drizzle app already opens: register it once at startup.
import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";

import { Litebackup } from "../src/index.js";
import { databasePath } from "../src/drizzle.js";

const db = drizzle({ client: new Database(process.env.DATABASE_PATH ?? "data/app.sqlite") });
const litebackup = new Litebackup();

// Registration is idempotent: every start and every worker may call it.
const rule = await litebackup.backups.start(databasePath(db));
console.log(`backing up ${rule.path} as ${rule.name}`);
