/**
 * better-sqlite3 support: the file a handle has open, and an adapter for
 * pools.
 *
 *   import Database from "better-sqlite3";
 *   import { databasePath } from "@litebackup/sdk/better-sqlite3";
 *   const db = new Database("data/app.sqlite");
 *   await litebackup.backups.start(databasePath(db));
 *
 * This module never imports better-sqlite3 itself; pass your Database.
 */
import { requireExistingFile } from "./existing-file.js";
import { assertDriverSafePath } from "./location.js";
import type { Adapter } from "./pool.js";
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

/** One listed of PRAGMA database_list. */
interface DatabaseListEntry {
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
  if (!db.open) {
    throw new LitebackupError("invalid_argument", "the database is closed; pass an open handle", 0);
  }
  // SQLite reports the file it opened, even when the working directory has
  // changed since a relative path was opened.
  const databases = db.pragma("database_list") as DatabaseListEntry[];
  const listed = databases.find((candidate) => candidate.name === schema);
  if (schema === "main") {
    if (db.memory || !listed?.file) {
      throw new LitebackupError("invalid_argument", "an in-memory or temporary database has no file to back up; open it from a path", 0);
    }
    return listed.file;
  }
  if (!listed) {
    throw new LitebackupError("invalid_argument", `no attached database named ${JSON.stringify(schema)}`, 0);
  }
  if (!listed.file) {
    throw new LitebackupError("invalid_argument", `the attached database ${JSON.stringify(schema)} is in memory or temporary and has no file to back up`, 0);
  }
  return listed.file;
}

/**
 * A pool adapter that opens the path with your better-sqlite3 Database class
 * and the options given, with fileMustExist set: open refuses a missing file
 * with not_found and never creates one. close rolls back a transaction the
 * work left unfinished, never commits it, and closes the handle.
 *
 *   const pool = litebackup.databases.pool({ adapter: adapter(Database) });
 *   await pool.lease("agents/42.sqlite", { path },
 *     (db) => db.prepare("INSERT INTO steps (body) VALUES (?)").run(step));
 */
export function adapter<Database extends BetterSqlite3Database, DatabaseOptions extends object = object>(
  DatabaseClass: new (path: string, options?: DatabaseOptions) => Database,
  databaseOptions?: DatabaseOptions,
): Adapter<Database> {
  return {
    open: (path) => {
      assertDriverSafePath(path);
      requireExistingFile(path);
      return new DatabaseClass(path, { ...databaseOptions, fileMustExist: true } as DatabaseOptions);
    },
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
