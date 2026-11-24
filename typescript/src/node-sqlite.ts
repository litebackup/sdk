/**
 * node:sqlite support: the file a DatabaseSync has open, and an adapter for
 * pools.
 *
 *   import { DatabaseSync } from "node:sqlite";
 *   import { databasePath } from "@litebackup/sdk/node-sqlite";
 *   const db = new DatabaseSync("data/app.sqlite");
 *   await litebackup.backups.start(databasePath(db));
 */
import { DatabaseSync, type DatabaseSyncOptions } from "node:sqlite";
import { pathToFileURL } from "node:url";

import { requireExistingFile } from "./existing-file.js";
import { ErrorCode, LitebackupError } from "./types.js";
import type { Adapter } from "./pool.js";

export { databasePath, type DatabasePathOptions } from "./node-sqlite-path.js";

/** Whether node:sqlite refused because the database is already closed. */
function alreadyClosed(error: unknown): boolean {
  return (error as NodeJS.ErrnoException)?.code === "ERR_INVALID_STATE";
}

/**
 * Opens an existing database through a file: URL in mode=rw, or mode=ro with
 * readOnly, so SQLite never creates the file either. A Node whose
 * DatabaseSync takes no URL is refused rather than opened by path, which
 * could create the file.
 */
function openExisting(path: string, databaseOptions: DatabaseSyncOptions | undefined): DatabaseSync {
  const location = pathToFileURL(path);
  location.search = databaseOptions?.readOnly ? "mode=ro" : "mode=rw"; // vocabulary:allow node:sqlite's option name
  try {
    return databaseOptions === undefined ? new DatabaseSync(location) : new DatabaseSync(location, databaseOptions);
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code !== "ERR_INVALID_ARG_TYPE") throw error;
    throw new LitebackupError(
      ErrorCode.invalidArgument,
      `this Node (${process.version}) has a node:sqlite whose DatabaseSync takes no file: URL, which the adapter needs to open existing databases only; use Node 24 or later, or the better-sqlite3 adapter`,
      0,
      { cause: error },
    );
  }
}

/**
 * A pool adapter that opens the path with node:sqlite and the options
 * given. open refuses a missing file with not_found and never creates one.
 * close rolls back a transaction the work left unfinished, never commits
 * it, and closes the database.
 */
export function adapter(databaseOptions?: DatabaseSyncOptions): Adapter<DatabaseSync> {
  return {
    open: (path) => {
      requireExistingFile(path);
      return openExisting(path, databaseOptions);
    },
    close: (db) => {
      // isOpen and isTransaction are missing on older Node 22 releases;
      // without them, try the rollback and the close and tolerate the
      // errors that mean there was nothing to do.
      if ((db.isOpen as boolean | undefined) === false) {
        return;
      }
      try {
        if ((db.isTransaction as boolean | undefined) !== false) {
          try {
            db.exec("ROLLBACK");
          } catch (error) {
            if (!alreadyClosed(error) && !/no transaction is active/.test(String((error as Error)?.message))) {
              throw error;
            }
          }
        }
      } finally {
        try {
          db.close();
        } catch (error) {
          if (!alreadyClosed(error)) {
            throw error;
          }
        }
      }
    },
  };
}
