import { basename } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

import { absolutePath, type Litebackup } from "./client.js";
import { milliseconds, type Duration } from "./duration.js";
import type { components } from "./generated/client.js";
import { LitebackupError, type BackupInfo, type Durability } from "./types.js";

/** A better-sqlite3 Database, read by duck typing; the SDK never imports better-sqlite3. */
export interface SQLiteHandle {
  readonly name: string;
  readonly memory: boolean;
  readonly open: boolean;
}

/** A database path, relative or absolute, or a better-sqlite3 handle. */
export type BackupSource = string | SQLiteHandle;

/** Options for backup. */
export interface BackupOptions {
  /** Sets the database name outright; it must be a valid name. */
  name?: string;
  /** Goes before the normalized file name, such as "app/". */
  prefix?: string;
  /** Makes the start request itself wait until the server, or the bucket, holds the first version; ready resolves then. */
  wait?: "none" | Durability;
  /** Bound on that wait. */
  timeout?: Duration;
  /** When positive, offload the least recently changed idle databases past this count once the bucket holds them. */
  maximumLocalDatabases?: number;
  /** Join token for the shared client; defaults to LITEBACKUP_TOKEN. Used only by the package-level backup(). */
  token?: string;
  /** Session for the shared client. Used only by the package-level backup(). */
  session?: string;
}

/** How often wait("cloud") reads a database's status. */
let durabilityPoll = 500;

/** Sets how often wait("cloud") polls, for tests. @internal */
export function setDurabilityPoll(interval: number): void {
  durabilityPoll = interval;
}

/** Reports whether a value looks like a better-sqlite3 Database. */
function isSQLiteHandle(value: unknown): value is SQLiteHandle {
  const candidate = value as Partial<SQLiteHandle> | null;
  return typeof candidate === "object" && candidate !== null && typeof candidate.name === "string" && typeof candidate.memory === "boolean" && typeof candidate.open === "boolean";
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

/** The name the daemon gives a single file: the file name, lowercased with other characters turned into -, after the prefix. */
export function expectedName(path: string, options: Pick<BackupOptions, "name" | "prefix">): string {
  if (options.name) {
    return options.name;
  }
  if (/[*?[]/.test(basename(path))) {
    return "";
  }
  // The daemon ends a prefix with / when it lacks one.
  let prefix = options.prefix ?? "";
  if (prefix && !prefix.endsWith("/")) {
    prefix += "/";
  }
  return prefix + basename(path).toLowerCase().replace(/[^a-z0-9._-]/g, "-");
}

/**
 * A running backup. backup() returns it at once; the start request runs in
 * the background. `ready` resolves with the daemon's answer, or rejects with
 * its error, such as name_in_use. wait() and stop() await `ready` first, so
 * they reject with the same error. Nothing swallows a failed start: when no
 * code awaits `ready`, wait(), or stop(), Node reports the rejection as
 * unhandled.
 */
export class Backup {
  /** The database name: the expected name until ready resolves, then the daemon's. Empty for a glob. */
  name: string;
  /** The absolute path or glob. */
  readonly path: string;
  /** The backup's ID on this machine, once ready resolved. */
  id: string | undefined;
  /** Resolves once the daemon started the backup. */
  readonly ready: Promise<BackupInfo>;
  /** @internal */
  private readonly client: Promise<Litebackup>;

  /** Use backup(); it starts the backup. */
  constructor(client: Promise<Litebackup>, source: BackupSource, options: BackupOptions) {
    this.path = sourcePath(source);
    this.name = expectedName(this.path, options);
    this.client = client;
    const body: components["schemas"]["BackupRequest"] = { path: this.path };
    if (options.name) {
      body.name = options.name;
    }
    if (options.prefix) {
      body.prefix = options.prefix;
    }
    if (options.wait) {
      body.wait = options.wait;
    }
    if (options.timeout !== undefined) {
      body.timeout_millis = milliseconds(options.timeout);
    }
    if (options.maximumLocalDatabases !== undefined) {
      body.maximum_local_databases = options.maximumLocalDatabases;
    }
    this.ready = client.then(async (litebackup) => {
      const info = (await litebackup.call("POST", "/v1/backups/start", body)) as BackupInfo;
      this.id = info.id;
      if (info.name) {
        this.name = info.name;
      } else if (info.databases.length === 1 && this.name) {
        this.name = info.databases[0]!.name;
      }
      return info;
    });
  }

  /**
   * Resolves once every change written before the call is on the server
   * ("server", the default) or in the bucket too ("cloud"). Rejects with the
   * start error when the backup never started.
   */
  async wait(durability: Durability = "server", options: { signal?: AbortSignal } = {}): Promise<void> {
    if (durability !== "server" && durability !== "cloud") {
      throw new LitebackupError("invalid_argument", `wait for server or cloud, not ${JSON.stringify(durability)}`, 0);
    }
    options.signal?.throwIfAborted();
    const started = await this.ready;
    options.signal?.throwIfAborted();
    const litebackup = await this.client;
    const listed = (await litebackup.backups.list(options)).find((backup) => backup.id === started.id);
    const names = (listed ?? started).databases.map((database) => database.name);
    if (names.length === 0 && this.name) {
      names.push(this.name);
    }
    for (const name of names) {
      const synced = await litebackup.sync(name, options);
      if (durability === "cloud") {
        for (;;) {
          options.signal?.throwIfAborted();
          const status = await litebackup.database(name, options);
          if (status.syncedCloud.version >= synced.syncedServer.version) {
            break;
          }
          if (status.state === "error" && status.lastError) {
            throw new LitebackupError("sync_failed", status.lastError, 0);
          }
          await delay(durabilityPoll, undefined, { signal: options.signal });
        }
      }
    }
  }

  /** Stops the backup and releases its write locks. The files and their backups stay. */
  async stop(): Promise<void> {
    const started = await this.ready;
    const litebackup = await this.client;
    await litebackup.backups.stop({ id: started.id });
  }
}
