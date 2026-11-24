import { readFileSync } from "node:fs";

import { ErrorCode, LitebackupError } from "./types.js";

/**
 * This process's file mode creation mask, which acquire and restore send so a
 * file the daemon creates gets the bits a file this process created would. On
 * Linux it reads the Umask line of /proc/self/status, which leaves the mask
 * alone; elsewhere, as on macOS, process.umask() sets the mask and puts it
 * back at once.
 * @internal
 */
export function currentUmask(): number {
  const mask = statusUmask("/proc/self/status");
  if (mask !== undefined) return mask;
  return process.umask();
}

/** Reads the octal Umask line of a /proc status file, undefined when it has none. @internal */
export function statusUmask(path: string): number | undefined {
  let status: string;
  try {
    status = readFileSync(path, "utf8");
  } catch {
    return undefined;
  }
  for (const line of status.split("\n")) {
    if (!line.startsWith("Umask:")) continue;
    const umaskText = line.slice("Umask:".length).trim();
    if (!/^[0-7]+$/.test(umaskText)) return undefined;
    const mask = Number.parseInt(umaskText, 8);
    return mask <= 0o777 ? mask : undefined;
  }
  return undefined;
}

/** The umask a request sends: the one given, checked, or this process's. @internal */
export function requestUmask(umask: number | undefined): number {
  if (umask === undefined) return currentUmask();
  if (!Number.isInteger(umask) || umask < 0 || umask > 0o777) {
    throw new LitebackupError(ErrorCode.invalidArgument, "umask must be between 0 and 0777", 0);
  }
  return umask;
}
