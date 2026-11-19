/**
 * Drizzle support: the file a Drizzle SQLite database has open, from its
 * documented `$client`.
 *
 *   import { drizzle } from "drizzle-orm/better-sqlite3";
 *   import { databasePath } from "@litebackup/sdk/drizzle";
 *   const db = drizzle("data/app.sqlite");
 *   await litebackup.backups.start(databasePath(db));
 *
 * The client must be a better-sqlite3 Database or a node:sqlite DatabaseSync.
 */
import { createRequire } from "node:module";
import { DatabaseSync } from "node:sqlite";

import { databasePath as betterSqlite3Path, type BetterSqlite3Database, type DatabasePathOptions } from "./better-sqlite3.js";
import { databasePath as nodeSqlitePath } from "./node-sqlite.js";
import { LitebackupError } from "./types.js";

export type { DatabasePathOptions } from "./better-sqlite3.js";

/** A Drizzle database: anything with the `$client` Drizzle documents. */
export interface DrizzleDatabase {
  readonly $client: unknown;
}

/** better-sqlite3's Database class as the app resolves it, or undefined when it is not installed. */
function betterSqlite3Class(): (abstract new (...args: never[]) => unknown) | undefined {
  try {
    return createRequire(import.meta.url)("better-sqlite3") as abstract new (...args: never[]) => unknown;
  } catch {
    return undefined;
  }
}

/**
 * Returns the absolute path of the file a Drizzle SQLite database has open,
 * or of an attached database with `schema`. Throws invalid_argument for
 * in-memory databases and for clients other than better-sqlite3 and node:sqlite.
 */
export function databasePath(db: DrizzleDatabase, options: DatabasePathOptions = {}): string {
  const client = db?.$client;
  if (client instanceof DatabaseSync) {
    return nodeSqlitePath(client, options);
  }
  const BetterSqlite3 = betterSqlite3Class();
  if (BetterSqlite3 && client instanceof BetterSqlite3) {
    return betterSqlite3Path(client as BetterSqlite3Database, options);
  }
  throw new LitebackupError(
    "invalid_argument",
    "databasePath takes a Drizzle database whose $client is a better-sqlite3 Database or a node:sqlite DatabaseSync; for other drivers, pass the file path you opened",
    0,
  );
}
