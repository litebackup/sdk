/**
 * better-sqlite3 support: the file a handle has open, and an adapter for
 * managed databases.
 *
 *   import Database from "better-sqlite3";
 *   import { databasePath } from "@litebackup/sdk/better-sqlite3";
 *   const db = new Database("data/app.sqlite");
 *   await litebackup.backups.start(databasePath(db));
 *
 * This module never imports better-sqlite3 itself; pass your Database.
 */
import type { ScopeAdapter } from "./managed.js";
import { absolutePath } from "./location.js";
import { LitebackupError } from "./types.js";

/** The parts of a better-sqlite3 Database this module uses. */
export interface BetterSqlite3Database {
  readonly name: string;
  readonly memory: boolean;
  readonly open: boolean;
  readonly inTransaction: boolean;
  pragma(source: string, options?: { simple?: boolean }): unknown;
  exec(source: string): unknown;
  close(): unknown;
}

/** Options for databasePath. */
export interface DatabasePathOptions {
  /** The schema whose file to return: "main" (the default) or an attached database's name. */
  schema?: string;
}

/** One row of PRAGMA database_list. */
interface DatabaseListRow {
  name: string;
  file: string;
}

/**
 * Returns the absolute path of the file a better-sqlite3 handle has open, or
 * of an attached database with `schema`. Throws invalid_argument for
 * in-memory and temporary databases, which have no file to back up.
 */
export function databasePath(db: BetterSqlite3Database, options: DatabasePathOptions = {}): string {
  const schema = options.schema ?? "main";
  if (schema === "main") {
    if (db.memory || db.name === "" || db.name === ":memory:") {
      throw new LitebackupError("invalid_argument", "an in-memory or temporary database has no file to back up; open it from a path", 0);
    }
    return absolutePath(db.name);
  }
  const rows = db.pragma("database_list") as DatabaseListRow[];
  const row = rows.find((candidate) => candidate.name === schema);
  if (!row) {
    throw new LitebackupError("invalid_argument", `no attached database named ${JSON.stringify(schema)}`, 0);
  }
  if (!row.file) {
    throw new LitebackupError("invalid_argument", `the attached database ${JSON.stringify(schema)} is in memory or temporary and has no file to back up`, 0);
  }
  return row.file;
}

/**
 * A managed-database adapter that opens the path with your better-sqlite3
 * Database class. close rolls back a transaction the work left unfinished,
 * never commits it, and closes the handle.
 *
 *   await litebackup.databases.managed("agents/42.sqlite",
 *     { to: path, durability: "cloud", adapter: adapter(Database) },
 *     (db) => db.prepare("INSERT INTO steps (body) VALUES (?)").run(step));
 */
export function adapter<Database extends BetterSqlite3Database, DatabaseOptions = unknown>(
  DatabaseClass: new (path: string, options?: DatabaseOptions) => Database,
  databaseOptions?: DatabaseOptions,
): ScopeAdapter<Database> {
  return {
    open: (path) => new DatabaseClass(path, databaseOptions),
    close: (db) => {
      if (!db.open) {
        return;
      }
      try {
        if (db.inTransaction) {
          db.exec("ROLLBACK");
        }
      } finally {
        db.close();
      }
    },
  };
}
