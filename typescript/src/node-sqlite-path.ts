/**
 * The file a node:sqlite DatabaseSync has open. Kept apart from the adapter,
 * which loads node:sqlite, so the Drizzle subpath can use it without loading
 * node:sqlite.
 */
import type { DatabaseSync } from "node:sqlite";

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
