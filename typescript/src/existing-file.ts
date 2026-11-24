import { statSync } from "node:fs";

import { ErrorCode, LitebackupError } from "./types.js";

/**
 * Refuses a path where no file exists with not_found: pool adapters open
 * existing databases only, because a missing file is an offloaded copy the
 * pool brings back, never an empty new database.
 */
export function requireExistingFile(path: string): void {
  try {
    statSync(path);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") return;
    throw new LitebackupError(ErrorCode.notFound, `${path} does not exist; the adapter opens existing databases only`, 0, { cause: error });
  }
}
