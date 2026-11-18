import { LitebackupError } from "./types.js";

/** Milliseconds as a number, or a duration such as "500ms", "60s", "2m", or "1h". */
export type Duration = number | string;

const units: Record<string, number> = { ms: 1, s: 1000, m: 60_000, h: 3_600_000 };

/** Converts a Duration to whole milliseconds. */
export function milliseconds(duration: Duration): number {
  if (typeof duration === "number") {
    if (!Number.isFinite(duration) || duration < 0) {
      throw new LitebackupError("invalid_argument", `a duration must be a non-negative number of milliseconds, got ${duration}`, 0);
    }
    return Math.round(duration);
  }
  const match = /^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)\s*$/.exec(duration);
  if (!match) {
    throw new LitebackupError("invalid_argument", `durations look like 500ms, 60s, 2m, or 1h, got ${JSON.stringify(duration)}`, 0);
  }
  return Math.round(Number(match[1]) * units[match[2]!]!);
}
