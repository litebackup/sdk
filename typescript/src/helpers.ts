import { type StartBackupOptions, type BackupSource } from "./backup.js";
import { Litebackup, type AcquireOptions, type ReleaseOptions, type ReplicateOptions, type ReplicatePrefixOptions, type RestoreOptions, type VersionsOptions } from "./client.js";
import type { Failover, FailoverOptions } from "./failover.js";
import { tokenEnvironment } from "./launch.js";
import type { AcquireResult, DatabaseStatus, PrefixReplica, ReleaseResult, RestoreResult, Version, Writer } from "./types.js";

/** Picks the shared client of the helpers. */
export interface SharedClientOptions {
  /** Join token; defaults to LITEBACKUP_TOKEN. */
  token?: string;
  /** Session; defaults to LITEBACKUP_SESSION, then "default". */
  session?: string;
}

/** One client per (token, session), connected on first use. */
const shared = new Map<string, Promise<Litebackup>>();

/**
 * Returns the shared client for a token and session, connecting on first
 * use. A failed connection, or a client closed with close(), is replaced on
 * the next call.
 */
export async function sharedClient(options: SharedClientOptions = {}): Promise<Litebackup> {
  const token = options.token ?? process.env[tokenEnvironment] ?? "";
  const session = options.session ?? process.env.LITEBACKUP_SESSION ?? "";
  const key = `${token}\u0000${session}`;
  const existing = shared.get(key);
  if (existing) {
    const client = await existing.catch(() => undefined);
    if (client && !client.closed) {
      return client;
    }
    if (shared.get(key) !== existing) {
      return sharedClient(options);
    }
  }
  const connecting = Litebackup.connect({ token: token || undefined, session: session || undefined });
  shared.set(key, connecting);
  try {
    return await connecting;
  } catch (error) {
    if (shared.get(key) === connecting) {
      shared.delete(key);
    }
    throw error;
  }
}

export async function startBackup(source: BackupSource, options: StartBackupOptions & SharedClientOptions = {}) {
  return (await sharedClient(options)).startBackup(source, options);
}

export async function stopBackup(name: string, options: SharedClientOptions = {}) {
  return (await sharedClient(options)).stopBackup(name);
}

export async function stopBackupRule(ruleId: string, options: SharedClientOptions = {}) {
  return (await sharedClient(options)).stopBackupRule(ruleId);
}

/** Runs a database on several identical machines; see Litebackup.failover. */
export async function failover(path: string, options: FailoverOptions = {}): Promise<Failover> {
  return (await sharedClient(options)).failover(path, options);
}

/** Keeps a live readonly replica of a database; resolves once its first version is local. */
export async function replicate(name: string, options: ReplicateOptions & SharedClientOptions = {}): Promise<DatabaseStatus> {
  return (await sharedClient(options)).replicate(name, options);
}

/** Registers live replicas of a namespace; downloads happen in the background. */
export async function replicatePrefix(prefix: string, options: ReplicatePrefixOptions & SharedClientOptions): Promise<PrefixReplica> {
  return (await sharedClient(options)).replicatePrefix(prefix, withoutClientOptions(options));
}

/** Writes a version of a database to a new file and checks it. */
export async function restore(name: string, options: RestoreOptions & SharedClientOptions): Promise<RestoreResult> {
  return (await sharedClient(options)).restore(name, options);
}

/** The versions of a database the server can restore, oldest first. */
export async function versions(name: string, options: VersionsOptions & SharedClientOptions = {}): Promise<Version[]> {
  return (await sharedClient(options)).versions(name, options);
}

/** Takes a database's write lock and brings it to this machine; see Litebackup.acquire. */
export async function acquire(name: string, options: AcquireOptions & SharedClientOptions = {}): Promise<AcquireResult> {
  return (await sharedClient(options)).acquire(name, withoutClientOptions(options));
}

/** Syncs an acquired database a final time and releases its write lock; see Litebackup.release. */
export async function release(name: string, options: ReleaseOptions & SharedClientOptions = {}): Promise<ReleaseResult> {
  return (await sharedClient(options)).release(name, withoutClientOptions(options));
}

/** Removes the local copy of a released database the bucket holds; see Litebackup.offload. */
export async function offload(name: string, options: SharedClientOptions = {}): Promise<DatabaseStatus> {
  return (await sharedClient(options)).offload(name);
}

/** Which machine writes a database, when its lease runs out, and whether it ran out. */
export async function writer(name: string, options: SharedClientOptions = {}): Promise<Writer> {
  return (await sharedClient(options)).writer(name);
}

/** Drops the shared client's token and session from call options. */
function withoutClientOptions<T extends SharedClientOptions>(options: T): Omit<T, keyof SharedClientOptions> {
  const { token: _token, session: _session, ...rest } = options;
  return rest;
}
