import { spawn } from "node:child_process";
import { absolutePath, type PathSource } from "./location.js";
import { litebackupBinary, missingFileCode, missingPath, startFailureMessage } from "./launch.js";
import { deadlineGraceMillis, milliseconds, type Duration } from "./duration.js";
import { ErrorCode, LitebackupError, type Durability } from "./types.js";
import { timeable } from "./timers.js";

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
  /** When positive, offload the least recently changed idle databases past this count once offloadDurability holds them. */
  maximumLocalDatabases?: number;
  /** What maximumLocalDatabases waits for: server (default) once the server holds a copy's last version, cloud once the bucket holds it too. */
  offloadDurability?: Durability;
  /** Lets maximumLocalDatabases offload rollback-journal databases too. Set it only when nothing keeps them open while idle: no lock shows an idle rollback-journal connection. WAL databases are offloaded either way. */
  offloadRollbackJournal?: boolean;
  signal?: AbortSignal;
}

/** Options for backups.once(). */
export interface BackupOptions {
  name?: string;
  prefix?: string;
  durability?: Durability;
  /** Passed to the CLI; a job still running 30 seconds past it is stopped and rejects with timeout. */
  timeout?: Duration;
  signal?: AbortSignal;
}

/** How long a stopped job has between SIGTERM and SIGKILL. */
const killAfterMillis = 15_000;
/** How long after the CLI exits its output may stay open, in a process it started, before the backup stops reading it. */
const drainMillis = 1_000;
/** How much of the start and of the end of the CLI's error output is kept. */
const stderrPart = 32_768;
/** How many of the CLI's file diagnostics a failed backup's message lists. */
const maximumFileDiagnostics = 20;

/** One structured error line of the CLI: a file's diagnostic or the job's final report. */
interface ReportedFailure {
  code: string;
  message: string;
}

/**
 * Reads the CLI's error output line by line and keeps its structured error
 * lines wherever they fall, however much log output lies between them: the
 * first file diagnostics and the last line, the job's final report.
 */
class FailureLines {
  private pending = "";
  readonly diagnostics: ReportedFailure[] = [];
  last: ReportedFailure | undefined;

  add(chunk: string): void {
    const lines = (this.pending + chunk).split("\n");
    // A line longer than stderrPart is log output; only its end is kept, and it does not parse.
    this.pending = lines.pop()!.slice(-stderrPart);
    for (const line of lines) this.parse(line);
  }

  end(): void {
    this.parse(this.pending);
    this.pending = "";
  }

  private parse(line: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      return; // Logs share stderr with structured errors.
    }
    if (typeof parsed !== "object" || parsed === null) return;
    const { code, message } = parsed as Record<string, unknown>;
    if (typeof code !== "string" || typeof message !== "string") return;
    this.last = { code, message };
    // A file reported twice is listed once, so the limit counts distinct files.
    if (this.diagnostics.length < maximumFileDiagnostics && !this.diagnostics.some((diagnostic) => diagnostic.message === message)) {
      this.diagnostics.push(this.last);
    }
  }

  /** The final report, followed by each file diagnostic the CLI printed before it. */
  failure(): ReportedFailure | undefined {
    if (!this.last) return undefined;
    const listed = new Set([this.last.message]);
    let message = this.last.message;
    for (const diagnostic of this.diagnostics) {
      if (listed.has(diagnostic.message)) continue;
      listed.add(diagnostic.message);
      message += "\n" + diagnostic.message;
    }
    return { code: this.last.code, message };
  }
}

/** @internal Lets tests shorten the deadline grace and the stop delay. */
export interface BackupTiming {
  graceMillis: number;
  killAfterMillis: number;
  drainMillis?: number;
}

/** Turns a backup source into the absolute path the daemon needs. */
export function sourcePath(source: BackupSource): string {
  if (source === "" || (typeof source !== "string" && !(source instanceof URL))) {
    throw new LitebackupError("invalid_argument", "backup needs a path or a file: URL", 0);
  }
  return absolutePath(source);
}

/** Run one job and resolve after the foreground process has exited. */
export async function foregroundBackup(
  source: BackupSource,
  options: BackupOptions,
  environment: NodeJS.ProcessEnv,
  session?: string,
  binaryPath?: string,
  timing: BackupTiming = { graceMillis: deadlineGraceMillis, killAfterMillis },
): Promise<void> {
  const path = sourcePath(source);
  if (options.name !== undefined && options.prefix !== undefined) throw new LitebackupError("invalid_argument", "choose a name or a prefix", 0);
  const args = ["backup", "--stdout-format", "json", "--durability", options.durability ?? "server"];
  if (session) args.push("--session", session);
  const timeoutMillis = options.timeout === undefined ? undefined : milliseconds(options.timeout);
  if (timeoutMillis !== undefined) args.push("--timeout", timeoutMillis + "ms");
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
    const startChild = () => spawn(binary, args, { env, stdio: ["ignore", "pipe", "pipe"] });
    let child: ReturnType<typeof startChild>;
    try {
      child = startChild();
    } catch (error) {
      // Some start failures, ENOTDIR among them, throw rather than emit "error".
      reject(startFailure(binary, error as NodeJS.ErrnoException));
      return;
    }
    // The start of the error output holds the first file diagnostic, and its end the final report.
    let head = "";
    let tail = "";
    const failureLines = new FailureLines();
    child.stdout.resume();
    child.stderr.setEncoding("utf8").on("data", (chunk: string) => {
      failureLines.add(chunk);
      const room = stderrPart - head.length;
      head += chunk.slice(0, room);
      tail = (tail + chunk.slice(Math.max(room, 0))).slice(-stderrPart);
    });
    let killTimer: NodeJS.Timeout | undefined;
    let stopping = false;
    const stop = () => {
      if (stopping) return;
      stopping = true;
      if (child.kill("SIGTERM")) killTimer = setTimeout(() => child.kill("SIGKILL"), timing.killAfterMillis).unref();
    };
    // The CLI enforces the timeout itself; this deadline only stops a CLI that never answers.
    let timedOut = false;
    const deadlineMillis = timeoutMillis === undefined ? undefined : timeoutMillis + timing.graceMillis;
    const deadline = !timeable(deadlineMillis) ? undefined : setTimeout(() => {
      // A CLI that already exited answered in time; its output is only draining.
      if (child.exitCode !== null || child.signalCode !== null) return;
      timedOut = true;
      stop();
    }, deadlineMillis);
    signal?.addEventListener("abort", stop, { once: true });
    // The CLI's output may stay open in a process it started; once the CLI itself
    // exited, stop reading (at once when it was stopped) so the backup settles.
    let drainTimer: NodeJS.Timeout | undefined;
    child.once("exit", () => {
      const finish = () => {
        child.stdout.destroy();
        child.stderr.destroy();
      };
      if (stopping) finish();
      else drainTimer = setTimeout(finish, timing.drainMillis ?? drainMillis).unref();
    });
    child.once("error", (err: NodeJS.ErrnoException) => {
      clearTimeout(deadline);
      reject(startFailure(binary, err));
    });
    child.once("close", (code) => {
      const stderr = head.length === stderrPart && tail ? `${head}\n${tail}` : head + tail;
      signal?.removeEventListener("abort", stop);
      clearTimeout(deadline);
      clearTimeout(killTimer);
      clearTimeout(drainTimer);
      // A job stopped at the deadline failed, even if it exited cleanly on SIGTERM.
      if (timedOut) return reject(new LitebackupError("timeout", "the foreground backup did not finish within its timeout", 0));
      if (code === 0) return resolve();
      if (signal?.aborted) return reject(new LitebackupError("cancelled", "the backup was cancelled", 0, { cause: signal.reason }));
      failureLines.end();
      // The CLI reports each file's error, the writer of a busy one included, before the final job report.
      const failure = failureLines.failure() ?? { code: "backup_failed", message: stderr.trim() || "foreground backup failed" };
      reject(new LitebackupError(failure.code, failure.message, 0));
    });
  });
}

/**
 * binary_not_found when the binary is gone, launch_failed when it cannot run.
 * ENOENT with the file present means its script interpreter is missing.
 */
export function startFailure(binary: string, err: NodeJS.ErrnoException): LitebackupError {
  const missing = missingFileCode(err.code) && missingPath(binary);
  return new LitebackupError(missing ? ErrorCode.binaryNotFound : ErrorCode.launchFailed, startFailureMessage(binary, err), 0, { cause: err });
}
