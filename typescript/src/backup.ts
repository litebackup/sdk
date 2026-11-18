import { spawn } from "node:child_process";
import { absolutePath } from "./client.js";
import { litebackupBinary } from "./launch.js";
import { milliseconds, type Duration } from "./duration.js";
import { LitebackupError, type Durability } from "./types.js";

/** A better-sqlite3 Database, read by duck typing; the SDK never imports better-sqlite3. */
export interface SQLiteHandle {
  readonly name: string;
  readonly memory: boolean;
  readonly open: boolean;
}

/** A database path, relative or absolute, or a better-sqlite3 handle. */
export type BackupSource = string | SQLiteHandle;

export interface StartBackupOptions {
  name?: string;
  prefix?: string;
  maximumLocalDatabases?: number;
}
export interface BackupOptions {
  name?: string;
  prefix?: string;
  wait?: Durability;
  timeout?: Duration;
  signal?: AbortSignal;
}

/** Reports whether a value looks like a better-sqlite3 Database. */
function isSQLiteHandle(value: unknown): value is SQLiteHandle {
  const candidate = value as Partial<SQLiteHandle> | null;
  return (
    typeof candidate === "object" &&
    candidate !== null &&
    typeof candidate.name === "string" &&
    typeof candidate.memory === "boolean" &&
    typeof candidate.open === "boolean"
  );
}

/** Turns a backup source into the absolute path the daemon needs. */
export function sourcePath(source: BackupSource): string {
  if (typeof source === "string") {
    if (!source) {
      throw new LitebackupError("invalid_argument", "backup needs a path", 0);
    }
    return absolutePath(source);
  }
  if (!isSQLiteHandle(source)) {
    throw new LitebackupError("invalid_argument", "backup takes a path or a better-sqlite3 database", 0);
  }
  if (source.memory || source.name === "" || source.name === ":memory:") {
    throw new LitebackupError("invalid_argument", "an in-memory database has no file to back up; open it from a path", 0);
  }
  return absolutePath(source.name);
}

/** Run one job and resolve after the foreground process has exited. */
export async function runBackup(source: BackupSource, options: BackupOptions, environment: NodeJS.ProcessEnv, session?: string): Promise<void> {
  const path = sourcePath(source);
  if (options.name !== undefined && options.prefix !== undefined) throw new LitebackupError("invalid_argument", "choose a name or a prefix", 0);
  const args = ["backup", "--stdout-format", "json", "--wait", options.wait ?? "server"];
  if (session) args.push("--session", session);
  if (options.timeout !== undefined) args.push("--timeout", milliseconds(options.timeout) + "ms");
  args.push("--", path);
  if (options.name !== undefined) {
    if (!options.name || options.name.endsWith("/")) throw new LitebackupError("invalid_argument", "expected an exact name", 0);
    args.push(options.name);
  }
  if (options.prefix) args.push(options.prefix.endsWith("/") ? options.prefix : options.prefix + "/");
  const env = { ...environment };
  const signal = options.signal;
  if (signal?.aborted) throw new LitebackupError("cancelled", "backup cancelled", 0);
  let binary: string;
  try {
    binary = litebackupBinary(env);
  } catch (err) {
    throw new LitebackupError("launch_failed", String(err), 0);
  }
  await new Promise<void>((resolve, reject) => {
    const child = spawn(binary, args, { env, stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    child.stdout.resume();
    child.stderr.setEncoding("utf8").on("data", (chunk: string) => {
      stderr = (stderr + chunk).slice(-65536);
    });
    let killTimer: NodeJS.Timeout | undefined;
    const cancel = () => {
      if (child.kill("SIGTERM")) killTimer = setTimeout(() => child.kill("SIGKILL"), 15_000).unref();
    };
    signal?.addEventListener("abort", cancel, { once: true });
    child.once("error", (err) => reject(new LitebackupError("launch_failed", err.message, 0)));
    child.once("close", (code) => {
      signal?.removeEventListener("abort", cancel);
      clearTimeout(killTimer);
      if (code === 0) return resolve();
      if (signal?.aborted) return reject(new LitebackupError("cancelled", "backup cancelled", 0));
      let failure = { code: "backup_failed", message: stderr.trim() || "foreground backup failed" };
      let firstMessage: string | undefined;
      for (const line of stderr.trim().split("\n")) {
        try {
          const parsed = JSON.parse(line);
          if (typeof parsed.code === "string" && typeof parsed.message === "string") {
            firstMessage ??= parsed.message;
            failure = parsed;
          }
        } catch { /* Logs can share stderr with structured errors. */ }
      }
      // The CLI reports file errors before the final job outcome.
      if (firstMessage && firstMessage !== failure.message) {
        failure.message += "\n" + firstMessage;
      }
      reject(new LitebackupError(failure.code, failure.message, 0));
    });
  });
}
