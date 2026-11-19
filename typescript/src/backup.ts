import { spawn } from "node:child_process";
import { absolutePath, type PathSource } from "./location.js";
import { litebackupBinary } from "./launch.js";
import { milliseconds, type Duration } from "./duration.js";
import { LitebackupError, type Durability } from "./types.js";

/**
 * A database path, relative or absolute, a glob, or a file: URL. To back up
 * an open handle, get its path from the driver's subpath export, such as
 * `databasePath(db)` from `@litebackup/sdk/better-sqlite3`.
 */
export type BackupSource = PathSource;

/** Options for backups.start(). */
export interface StartBackupOptions {
  /** The remote name of a single file; defaults to its file name. */
  name?: string;
  /** A name prefix such as tenants/ for a directory or glob; names are the prefix plus the relative path. */
  prefix?: string;
  /** When positive, offload the least recently changed idle databases past this count once the bucket holds them. */
  maximumLocalDatabases?: number;
  signal?: AbortSignal;
}

/** Options for backups.once(). */
export interface BackupOptions {
  name?: string;
  prefix?: string;
  wait?: Durability;
  timeout?: Duration;
  signal?: AbortSignal;
}

/** Turns a backup source into the absolute path the daemon needs. */
export function sourcePath(source: BackupSource): string {
  if (source === "" || (typeof source !== "string" && !(source instanceof URL))) {
    throw new LitebackupError("invalid_argument", "backup needs a path or a file: URL", 0);
  }
  return absolutePath(source);
}

/** Run one job and resolve after the foreground process has exited. */
export async function runBackup(source: BackupSource, options: BackupOptions, environment: NodeJS.ProcessEnv, session?: string, binaryPath?: string): Promise<void> {
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
  if (signal?.aborted) throw new LitebackupError("cancelled", "the backup was cancelled", 0, { cause: signal.reason });
  const binary = litebackupBinary(env, binaryPath);
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
      if (signal?.aborted) return reject(new LitebackupError("cancelled", "the backup was cancelled", 0, { cause: signal.reason }));
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
