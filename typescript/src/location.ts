import { isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { LitebackupError } from "./types.js";

/** A local path: a string, relative or absolute, or a file: URL. */
export type PathSource = string | URL;

/** Resolves a path or file: URL against the working directory and cleans it; the daemon needs absolute paths. */
export function absolutePath(source: PathSource): string {
  if (source instanceof URL) {
    if (source.protocol !== "file:") {
      throw new LitebackupError("invalid_argument", `expected a path or a file: URL, got ${source.protocol} URL`, 0);
    }
    return resolve(fileURLToPath(source));
  }
  return isAbsolute(source) ? resolve(source) : resolve(process.cwd(), source);
}

/** Renders a time as the API wants it: UTC with Z, fractional seconds only when they are not zero. */
export function wireTime(time: Date | string): string {
  const date = time instanceof Date ? time : new Date(time);
  if (!Number.isFinite(date.getTime())) {
    throw new LitebackupError("invalid_argument", `expected a valid time, got ${JSON.stringify(String(time))}`, 0);
  }
  return date.toISOString().replace(/\.(\d*?)0*Z$/, (_match, digits: string) => (digits ? `.${digits}Z` : "Z"));
}
