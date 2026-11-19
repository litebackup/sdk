/**
 * node:sqlite support: the file a DatabaseSync has open, and an adapter for
 * managed databases.
 *
 *   import { DatabaseSync } from "node:sqlite";
 *   import { databasePath } from "@litebackup/sdk/node-sqlite";
 *   const db = new DatabaseSync("data/app.sqlite");
 *   await litebackup.backups.start(databasePath(db));
 */
import { DatabaseSync, type DatabaseSyncOptions } from "node:sqlite";

import type { ScopeAdapter } from "./managed.js";
import { LitebackupError } from "./types.js";

/** Options for databasePath. */
export interface DatabasePathOptions {
  /** The schema whose file to return: "main" (the default) or an attached database's name. */
  schema?: string;
}

/**
 * Returns the absolute path of the file a DatabaseSync has open, or of an
 * attached database with `schema`, from `db.location()`. Throws
 * invalid_argument for in-memory and temporary databases.
 */
export function databasePath(db: DatabaseSync, options: DatabasePathOptions = {}): string {
  if (typeof db.location !== "function") {
    throw new LitebackupError("invalid_argument", "this Node's node:sqlite has no DatabaseSync.location(); upgrade Node or pass the path you opened", 0);
  }
  const schema = options.schema ?? "main";
  const location = db.location(schema);
  if (!location) {
    throw new LitebackupError("invalid_argument", `the database ${JSON.stringify(schema)} is in memory, temporary, or not attached, and has no file to back up`, 0);
  }
  return location;
}

/**
 * A managed-database adapter that opens the path with node:sqlite. close
 * rolls back a transaction the work left unfinished, never commits it, and
 * closes the database.
 */
export function adapter(databaseOptions?: DatabaseSyncOptions): ScopeAdapter<DatabaseSync> {
  return {
    open: (path) => (databaseOptions === undefined ? new DatabaseSync(path) : new DatabaseSync(path, databaseOptions)),
    close: (db) => {
      if (!db.isOpen) {
        return;
      }
      try {
        if (db.isTransaction) {
          db.exec("ROLLBACK");
        }
      } finally {
        db.close();
      }
    },
  };
}
