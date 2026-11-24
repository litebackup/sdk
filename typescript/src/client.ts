import { setTimeout as delay } from "node:timers/promises";

import { foregroundBackup, sourcePath, type StartBackupOptions, type BackupOptions, type BackupSource } from "./backup.js";
import { camelize } from "./case.js";
import { deadlineGraceMillis, milliseconds, type Duration } from "./duration.js";
import { Failover, type FailoverOptions } from "./failover.js";
import type { components } from "./generated/client.js";
import { apiKeyEnvironment, credentialType, launchDaemon } from "./launch.js";
import { absolutePath, wireTime, type PathSource } from "./location.js";
import { Pool, type PoolOptions } from "./pool.js";
import { sessionSocket } from "./paths.js";
import { socketFetch } from "./socket.js";
import { timeable } from "./timers.js";
import { requestUmask } from "./umask.js";
import {
  ErrorCode,
  LitebackupError,
  compareVersions,
  minimumDaemonVersion,
  responseError,
  sdkVersion,
  type AcquireResult,
  type BackupRule,
  type DatabaseStatus,
  type DatabaseStatusList,
  type Durability,
  type EventList,
  type Event,
  type PrefixReplica,
  type ReleaseResult,
  type ReplicaList,
  type RestoreResult,
  type RevertResult,
  type ShutdownResult,
  type Status,
  type RestorableVersion,
  type StopBackupResult,
  type VersionList,
  type WriteProtection,
  type Writer,
} from "./types.js";

type Schemas = components["schemas"];

/** Header names of the daemon's version negotiation. */
const sdkVersionHeader = "litebackup-sdk-version";
const clientVersionHeader = "litebackup-client-version";

/** How long one events long-poll waits unless told otherwise. */
const eventWaitMillis = 30_000;
/** Bounds on the backoff between failed event long-polls. */
const firstRetryMillis = 250;
const maximumRetryMillis = 10_000;
/** Codes that end an event watch instead of being retried. */
const watchEndingCodes: ReadonlySet<string> = new Set([ErrorCode.sdkUnsupported, ErrorCode.closed, ErrorCode.cancelled, ErrorCode.invalidArgument, ErrorCode.cursorExpired]);
/** How long the connect probe waits for the daemon's answer. */
const probeTimeoutMillis = 30_000;
/** How long a caller waits for the connection, which may include starting the daemon. */
const connectTimeoutMillis = 65_000;

/** Options for connecting to a Litebackup session. */
export interface LitebackupOptions {
  /**
   * The credential the daemon uses when it starts: an API key, or a join
   * token it exchanges once for a key of this machine, then @ and the
   * server's address; defaults to LITEBACKUP_API_KEY. The daemon keeps the
   * key it saved unless this holds a different credential, which the launch
   * refuses with ErrorCode.drainRequired while the client holds write locks;
   * a daemon already running keeps its key until it restarts. Foreground
   * backups use it too.
   */
  apiKey?: string;
  /** Session name; defaults to LITEBACKUP_SESSION, then "default". */
  session?: string;
  /** Environment for resolving paths and starting the daemon; defaults to process.env. */
  environment?: NodeJS.ProcessEnv;
  /** Start the session daemon when it is not running; defaults to true. */
  launch?: boolean;
  /** Connect to this socket and never start a daemon. */
  socketPath?: string;
  /** The litebackup binary that starts the daemon; see litebackupBinary() for the search when unset. */
  binaryPath?: string;
}

/** An abort signal for one call. Aborting stops the local wait only; the daemon may still finish the operation. */
export interface SignalOptions {
  signal?: AbortSignal;
}

/** Options for acquire. */
export interface AcquireOptions extends SignalOptions {
  /** Local file or file: URL; relative paths resolve against the working directory. Defaults to the path this machine uses, which brings an offloaded database back. */
  path?: PathSource;
  /** Checkpoint on release and VACUUM past the freelist threshold. */
  maintenance?: boolean;
  /** Lets Litebackup remove the released copy once offloadDurability holds it. */
  offloadOnRelease?: boolean;
  /** What an offload of the released copy waits for: server (default) once the server holds its last version, cloud once the bucket holds it too. */
  offloadDurability?: Durability;
  /** What other machines see as the writer. */
  machineLabel?: string;
  /** Bound on downloading the head. */
  timeout?: Duration;
  /** How long the daemon keeps trying while another machine holds the write lock; unset or 0 fails at once with lock_unavailable. The call may take timeout plus lockTimeout. */
  lockTimeout?: Duration;
  /** The file mode creation mask a file the acquire creates gets: 0666 less it, as a file the caller created would. A copy the head replaces keeps its bits. Defaults to this process's umask. */
  umask?: number;
  /** Replace a local file that diverged from the server head with the head, discarding its writes the head lacks. Without it such a file is refused with conflict and left untouched. */
  discardLocal?: boolean;
}

/** Options for release. */
export interface ReleaseOptions extends SignalOptions {
  /** server returns once the final sync is acknowledged; cloud waits for the bucket too. */
  durability?: Durability;
  /** Remove the local file once durability holds the released version: the server with server, the bucket with cloud. */
  offload?: boolean;
  /** How long to wait for the database to close and for the chosen durability. */
  timeout?: Duration;
}

/** Checks an offload durability; undefined and "server", the daemon's default, stay off the wire. */
function offloadDurabilityChoice(durability: Durability | undefined): Durability | undefined {
  if (durability !== undefined && durability !== "server" && durability !== "cloud") {
    throw new LitebackupError(ErrorCode.invalidArgument, "offload durability must be server or cloud", 0);
  }
  return durability === "cloud" ? durability : undefined;
}

/** Options for offload. */
export interface OffloadOptions extends SignalOptions {
  /** server (default) removes the file once the server holds its last version; cloud waits for the bucket too. */
  durability?: Durability;
}

/** Options for replicate. */
export interface ReplicateOptions extends SignalOptions {
  /** Replica file or file: URL; relative paths resolve against the working directory. Defaults to the path this machine uses. */
  path?: PathSource;
  /** lock (default) makes app writes fail with SQLITE_BUSY; detect rebases after a local write; none trusts apps to open with mode=ro. */
  writeProtection?: WriteProtection;
  /** Bound on downloading the first version. */
  timeout?: Duration;
}

/** Options for replicating a namespace; registration does not wait for downloads. */
export interface ReplicatePrefixOptions extends SignalOptions {
  /** Directory, or file: URL, that receives each database at its name relative to the prefix. */
  directory: PathSource;
  writeProtection?: WriteProtection;
}

/** Options for restore: a version, a time, or neither for the head. */
export interface RestoreOptions extends SignalOptions {
  /** The new file or file: URL; relative paths resolve against the working directory. Its directory must exist and the file must not. */
  path: PathSource;
  version?: number;
  /** Restore the newest version at or before this time. */
  time?: Date | string;
  /** The file mode creation mask the new file is created under: it gets 0666 less it, as a file the caller created would. Defaults to this process's umask. */
  umask?: number;
}

/** Options for revert: a version or a time, one of them. */
export interface RevertOptions extends SignalOptions {
  /** The version whose content becomes the new head. */
  version?: number;
  /** Revert to the newest version at or before this time. */
  time?: Date | string;
}

/** Options for versions. */
export interface VersionsOptions extends SignalOptions {
  /** Skip versions up to and including this one. */
  after?: number;
  /** Return at most this many; every version when unset. */
  limit?: number;
}

/** A cursor and page size for status and database pages. */
export interface PageOptions extends SignalOptions {
  /** Return databases whose names sort after this one; pass `next` to continue. */
  after?: string;
  /** Return at most this many databases, from 1 to 1000; 100 by default. */
  limit?: number;
}

/** Options for one page of events. */
export interface EventPageOptions extends SignalOptions {
  /** Return events with IDs greater than this one; -1 returns none and, as next, where the log ends. */
  after?: number;
  /** How long to wait for the first new event. */
  wait?: Duration;
  limit?: number;
}

/** Options for events.watch(). */
export interface EventsOptions extends SignalOptions {
  /** Start after this event ID; -1 starts at the end of the log, 0 at the oldest. */
  after?: number;
  /** How long each long-poll waits for a new event; defaults to 30s. */
  wait?: Duration;
  /** Called with each failed long-poll before it is retried with backoff. */
  onError?: (error: LitebackupError) => void;
}

/** Options for shutdown. */
export interface ShutdownOptions extends SignalOptions {
  durability?: "none" | Durability;
  timeout?: Duration;
}

/** Options for one socket call. @internal */
export interface CallOptions {
  signal?: AbortSignal;
  /** The call's server-side timeout in milliseconds; its client-side deadline is this plus a grace period. */
  timeoutMillis?: number;
  /** A client-side deadline in milliseconds for a call without a server-side timeout. */
  deadlineMillis?: number;
}

/** Refuses a page limit the daemon does not take: a whole number from 1 to 1000. */
function checkPageLimit(limit: number): void {
  if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
    throw new LitebackupError("invalid_argument", "the limit must be a whole number from 1 to 1000", 0);
  }
}

/** Resolves or rejects with the promise, or rejects with the signal's reason once it aborts. */
async function untilAborted<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  let onAbort: (() => void) | undefined;
  const aborted = new Promise<never>((_resolve, reject) => {
    onAbort = () => reject(signal.reason);
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  });
  try {
    return await Promise.race([promise, aborted]);
  } finally {
    signal.removeEventListener("abort", onAbort!);
  }
}

/** A duration as whole milliseconds, or undefined when unset or zero, so the daemon's default applies. */
function optionalMillis(duration: Duration | undefined): number | undefined {
  if (duration === undefined) {
    return undefined;
  }
  return milliseconds(duration) || undefined;
}

/** Talks to the Litebackup daemon of one session over its unix socket. */
export class Litebackup {
  private targetEnvironment?: NodeJS.ProcessEnv;
  private readonly options: LitebackupOptions;
  private connection?: Promise<void>;
  private connected = false;
  private socketPath = "";
  private readonly lifetime = new AbortController();
  /** The daemon's version, from its last answer. */
  clientVersion = "";

  /** Continuous backups and one-shot backup jobs. */
  readonly backups = {
    start: this.#startBackup.bind(this),
    once: this.#backup.bind(this),
    stop: this.#stopBackup.bind(this),
  };
  /** Saved backup and failover rules. */
  readonly rules = {
    list: async (options: SignalOptions = {}): Promise<BackupRule[]> =>
      (await this.call<Schemas["BackupList"]>("GET", "/v1/backup-rules/list", undefined, options)).rules,
    stop: this.#stopBackupRule.bind(this),
  };
  /** Databases on this machine and on the server. */
  readonly databases = {
    get: this.#database.bind(this),
    list: this.#listDatabases.bind(this),
    page: this.#databasesPage.bind(this),
    all: this.#allDatabases.bind(this),
    versions: this.#versions.bind(this),
    versionsPage: this.#versionsPage.bind(this),
    allVersions: this.#allVersions.bind(this),
    restore: this.#restore.bind(this),
    revert: this.#revert.bind(this),
    sync: this.#sync.bind(this),
    offload: this.#offload.bind(this),
    writer: this.#writer.bind(this),
    acquire: this.#acquire.bind(this),
    release: this.#release.bind(this),
    pool: this.#pool.bind(this),
  };
  /** The daemon's event log. */
  readonly events = { page: this.#eventPage.bind(this), watch: this.#watchEvents.bind(this) };
  /** The session and its daemon. */
  readonly session = { status: this.#status.bind(this), shutdown: this.#shutdown.bind(this) };
  /** Databases run on several identical machines. */
  readonly failovers = { start: this.#failover.bind(this) };
  /** Prefix replicas and replicas on this machine. */
  readonly replicas = {
    start: this.#replicate.bind(this),
    startPrefix: this.#replicatePrefix.bind(this),
    list: (options: SignalOptions = {}): Promise<ReplicaList> => this.call("GET", "/v1/replicas/list", undefined, options),
    stop: async (name: string, options: SignalOptions = {}): Promise<void> => {
      await this.call("POST", "/v1/replicas/stop", { name }, options);
    },
    stopPrefix: async (prefix: string, directory: PathSource, options: SignalOptions = {}): Promise<void> => {
      if (!directory) {
        throw new LitebackupError("invalid_argument", "stopping a prefix replica needs a directory", 0);
      }
      await this.call("POST", "/v1/replicas/stop", { prefix, directory: absolutePath(directory) }, options);
    },
  };
  /** Failovers this client follows; close() stops following them. @internal */
  readonly watches = new Set<Failover>();
  /** @internal */
  closed = false;

  constructor(options: LitebackupOptions = {}) {
    this.options = { ...options, environment: options.environment && { ...options.environment } };
  }

  /**
   * Connects to the session, starting its daemon like the CLI does. Calls
   * connect on first use, so calling it is optional. Fails with
   * daemon_unsupported when the daemon is older than minimumDaemonVersion.
   * Concurrent callers share one connection attempt; each stops waiting when
   * its own signal aborts.
   */
  async connect(options: SignalOptions = {}): Promise<void> {
    this.ensureOpen();
    if (options.signal?.aborted) {
      throw new LitebackupError("cancelled", "connecting was cancelled", 0, { cause: options.signal.reason });
    }
    if (this.connected) return;
    this.connection ??= this.#open().then(
      () => {
        this.connected = true;
      },
      (error) => {
        this.connection = undefined;
        if (error instanceof LitebackupError) throw error;
        throw new LitebackupError("connection_failed", String(error), 0, { cause: error });
      },
    );
    const deadline = AbortSignal.timeout(connectTimeoutMillis);
    const signals = [this.lifetime.signal, deadline];
    if (options.signal) signals.push(options.signal);
    try {
      await untilAborted(this.connection, AbortSignal.any(signals));
    } catch (error) {
      this.ensureOpen();
      if (options.signal?.aborted) {
        throw new LitebackupError("cancelled", "connecting was cancelled", 0, { cause: options.signal.reason });
      }
      if (deadline.aborted) {
        throw new LitebackupError("timeout", `connecting to the daemon did not finish within ${connectTimeoutMillis} ms`, 0, { cause: error });
      }
      throw error;
    }
    this.ensureOpen();
  }

  private ensureOpen(): void {
    if (this.closed) throw new LitebackupError("closed", "the client is closed", 0);
  }

  private environment(): NodeJS.ProcessEnv {
    const env = this.targetEnvironment ?? this.options.environment ?? process.env;
    const apiKey = this.options.apiKey ?? env[apiKeyEnvironment];
    credentialType(apiKey);
    this.targetEnvironment ??= { ...env };
    return { ...this.targetEnvironment, [apiKeyEnvironment]: apiKey };
  }

  async #open(): Promise<void> {
    const environment = this.environment();
    let session = this.options.session;
    if (this.options.socketPath !== undefined) {
      if (!this.options.socketPath) throw new LitebackupError("invalid_argument", "socketPath needs a path", 0);
      this.socketPath = this.options.socketPath;
    } else {
      const target = sessionSocket(this.options.session, environment);
      this.socketPath = target.socketPath;
      session = target.session;
      if (this.options.launch !== false) {
        await launchDaemon({ ...target, environment, binaryPath: this.options.binaryPath, signal: this.lifetime.signal });
      }
    }
    await this.request("GET", "/v1/status/show?limit=1", undefined, { deadlineMillis: probeTimeoutMillis });
    if (this.clientVersion && compareVersions(this.clientVersion, minimumDaemonVersion) >= 0) {
      return;
    }
    const reported = this.clientVersion ? `is version ${this.clientVersion}` : "does not report its version";
    const drain = session ? `litebackup drain --session ${session}` : "litebackup drain";
    throw new LitebackupError(
      ErrorCode.daemonUnsupported,
      `the litebackup daemon ${reported}, and this SDK needs ${minimumDaemonVersion} or later. Stop it with \`${drain}\` so the next call starts the new binary, or restart the service if \`litebackup install service\` runs it`,
      0,
    );
  }

  /**
   * Sends one call and decodes its JSON answer into camelCase, throwing
   * LitebackupError on failure.
   * @internal
   */
  async call<T>(method: "GET" | "POST", path: string, body?: unknown, options: CallOptions = {}): Promise<any> {
    if (options.signal?.aborted) {
      throw new LitebackupError("cancelled", "the call was cancelled before it was sent", 0, { cause: options.signal.reason });
    }
    await this.connect({ signal: options.signal });
    return this.request<T>(method, path, body, options);
  }

  private async request<T>(method: "GET" | "POST", path: string, body?: unknown, options: CallOptions = {}): Promise<any> {
    const signals = [this.lifetime.signal];
    if (options.signal) signals.push(options.signal);
    const deadlineMillis = options.timeoutMillis ? options.timeoutMillis + deadlineGraceMillis : options.deadlineMillis;
    const deadline = timeable(deadlineMillis) ? AbortSignal.timeout(deadlineMillis) : undefined;
    if (deadline) signals.push(deadline);
    const response = await socketFetch({
      socketPath: this.socketPath,
      method,
      path,
      headers: { [sdkVersionHeader]: `typescript/${sdkVersion}` },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.any(signals),
    }).catch((error: unknown) => {
      this.ensureOpen();
      if (options.signal?.aborted) {
        throw new LitebackupError("cancelled", "the call was cancelled; the daemon may still finish the operation", 0, { cause: options.signal.reason });
      }
      if (deadline?.aborted) {
        throw new LitebackupError("timeout", "the daemon did not answer within the call's timeout; it may still finish the operation", 0, { cause: error });
      }
      throw new LitebackupError("connection_failed", String(error), 0, { cause: error });
    });
    this.clientVersion = response.headers[clientVersionHeader] ?? "";
    if (response.status !== 200) {
      throw responseError(response.status, response.body);
    }
    try {
      return camelize(JSON.parse(response.body) as T);
    } catch (error) {
      throw new LitebackupError("invalid_response", "the daemon returned invalid JSON", response.status, { cause: error });
    }
  }

  /**
   * Saves a continuous backup rule for a file, directory, or glob and resolves
   * once the daemon saved it; listing and uploads run in the background.
   * Starting the same path again with the same settings returns the saved
   * rule, so call it on every start. Other settings for a saved path fail with
   * rule_exists, and the error's details.rule is the saved rule.
   */
  async #startBackup(source: BackupSource, options: StartBackupOptions = {}): Promise<BackupRule> {
    if (options.name && options.prefix) {
      throw new LitebackupError("invalid_argument", "choose a name or a prefix", 0);
    }
    const body: Schemas["BackupRequest"] = { path: sourcePath(source) };
    if (options.name) body.name = options.name;
    if (options.prefix) body.prefix = options.prefix;
    if (options.maximumLocalDatabases) body.maximum_local_databases = options.maximumLocalDatabases;
    const offloadDurability = offloadDurabilityChoice(options.offloadDurability);
    if (offloadDurability) body.offload_durability = offloadDurability;
    if (options.offloadRollbackJournal) body.offload_rollback_journal = true;
    return this.call("POST", "/v1/backup-rules/start", body, options);
  }

  /** Stop a single-file backup on this machine by its exact remote database name. */
  async #stopBackup(name: string, options: SignalOptions = {}): Promise<StopBackupResult> {
    if (!name) throw new LitebackupError("invalid_argument", "stopping a backup needs its database name", 0);
    return this.call("POST", "/v1/backup-rules/stop", { name }, options);
  }

  /** Stop the whole backup rule, including future matches, using the ID returned by backups.start. */
  async #stopBackupRule(ruleId: string, options: SignalOptions = {}): Promise<StopBackupResult> {
    if (!ruleId) throw new LitebackupError("invalid_argument", "stopping a backup rule needs its ID", 0);
    return this.call("POST", "/v1/backup-rules/stop", { rule_id: ruleId }, options);
  }

  /** Runs one foreground backup job and resolves once it exits. */
  async #backup(source: BackupSource, options: BackupOptions = {}): Promise<void> {
    this.ensureOpen();
    if (this.options.socketPath !== undefined) throw new LitebackupError("invalid_argument", "foreground backup needs a named session and cannot target socketPath", 0);
    const signal = options.signal ? AbortSignal.any([options.signal, this.lifetime.signal]) : this.lifetime.signal;
    try {
      await foregroundBackup(source, { ...options, signal }, this.environment(), this.options.session, this.options.binaryPath);
    } catch (error) {
      // The lifetime signal aborted because the client closed: report closed, not cancelled.
      if (error instanceof LitebackupError && error.code === "cancelled") this.ensureOpen();
      throw error;
    }
  }

  /**
   * Runs a database on several identical machines: the first to take the
   * write lock is the origin, the others keep live readonly replicas at the
   * same path and take over when the origin's lease runs out.
   */
  async #failover(path: PathSource, options: FailoverOptions = {}): Promise<Failover> {
    return Failover.start(this, path, options);
  }

  /** Keeps a live readonly replica of a database and resolves once its first version is local. */
  async #replicate(name: string, options: ReplicateOptions = {}): Promise<DatabaseStatus> {
    const body: Schemas["ReplicaRequest"] = { name };
    if (options.path) body.path = absolutePath(options.path);
    if (options.writeProtection) body.write_protection = options.writeProtection;
    const timeoutMillis = optionalMillis(options.timeout);
    if (timeoutMillis) body.timeout_millis = timeoutMillis;
    const started = (await this.call("POST", "/v1/replicas/start", body, { signal: options.signal, timeoutMillis })) as { database?: DatabaseStatus };
    if (!started.database) {
      throw new LitebackupError("invalid_response", "the daemon did not return the replica", 200);
    }
    return started.database;
  }

  /** Registers live readonly replicas of current and future databases under a prefix. An empty prefix follows all databases. */
  async #replicatePrefix(prefix: string, options: ReplicatePrefixOptions): Promise<PrefixReplica> {
    if (!options?.directory) {
      throw new LitebackupError("invalid_argument", "replicating a prefix needs a directory", 0);
    }
    const body: Schemas["ReplicaRequest"] = { prefix, directory: absolutePath(options.directory) };
    if (options.writeProtection) body.write_protection = options.writeProtection;
    const started = (await this.call("POST", "/v1/replicas/start", body, options)) as { prefixReplica?: PrefixReplica };
    if (!started.prefixReplica) {
      throw new LitebackupError("invalid_response", "the daemon did not return the prefix replica", 200);
    }
    return started.prefixReplica;
  }

  /** Writes a version of a database to a new file and checks it. An existing file is never overwritten (file_exists). */
  async #restore(name: string, options: RestoreOptions): Promise<RestoreResult> {
    if (!options?.path) {
      throw new LitebackupError("invalid_argument", "restore needs a path to write to", 0);
    }
    if (options.version !== undefined && options.time !== undefined) {
      throw new LitebackupError("invalid_argument", "restore takes a version or a time, not both", 0);
    }
    const body: Schemas["RestoreRequest"] = { name, path: absolutePath(options.path) };
    if (options.version !== undefined) body.version = options.version;
    if (options.time !== undefined) body.time = wireTime(options.time);
    body.umask = requestUmask(options.umask);
    return this.call("POST", "/v1/databases/restore", body, options);
  }

  /**
   * Rolls a database back under its own name: the content of a past version becomes a new head, one past the current head, and every later version stays restorable.
   * The database must be one this machine acquired or backs up, and its file closed (still_open otherwise); a replica or a failover standby is refused (invalid_argument), and a file holding writes the server lacks is refused with conflict and left untouched.
   * The daemon takes the write lock when it does not hold it and gives it back once the new head is synced; when that sync fails for any reason but a lost lock or a refused file, which are final, it keeps the lock and syncs again on its own, so release an acquired database afterwards.
   */
  async #revert(name: string, options: RevertOptions): Promise<RevertResult> {
    if ((options?.version === undefined) === (options?.time === undefined)) {
      throw new LitebackupError(ErrorCode.invalidArgument, "revert takes a version or a time, one of them", 0);
    }
    if (options.version !== undefined && (!Number.isInteger(options.version) || options.version < 1)) {
      throw new LitebackupError(ErrorCode.invalidArgument, "revert needs a positive whole version", 0);
    }
    const body: Schemas["RevertRequest"] = { name };
    if (options.version !== undefined) body.version = options.version;
    if (options.time !== undefined) body.time = wireTime(options.time);
    return this.call("POST", "/v1/databases/revert", body, options);
  }

  /** The versions of a database the server can restore, oldest first. */
  async #versions(name: string, options: VersionsOptions = {}): Promise<RestorableVersion[]> {
    const versions: RestorableVersion[] = [];
    for await (const version of this.#allVersions(name, options)) {
      versions.push(version);
    }
    return versions;
  }

  /** One page of the versions of a database the server can restore, oldest first. Pass the returned next as after to continue. */
  async #versionsPage(name: string, options: VersionsOptions = {}): Promise<VersionList> {
    const query = new URLSearchParams({ name });
    if (options.after !== undefined && options.after > 0) query.set("after", String(options.after));
    if (options.limit !== undefined) query.set("limit", String(options.limit));
    return this.call("GET", `/v1/databases/versions?${query}`, undefined, options);
  }

  /** Yields the versions of a database the server can restore, oldest first, a page at a time. */
  async *#allVersions(name: string, options: VersionsOptions = {}): AsyncGenerator<RestorableVersion> {
    if (options.limit === 0) return;
    let after = options.after ?? 0;
    let yielded = 0;
    for (;;) {
      const limit = options.limit !== undefined ? options.limit - yielded : undefined;
      const page = await this.#versionsPage(name, { ...options, after, limit });
      for (const version of page.versions) {
        yield version;
        yielded++;
        if (options.limit !== undefined && yielded >= options.limit) return;
      }
      if (!page.next || page.next <= after || page.versions.length === 0) return;
      after = page.next;
    }
  }

  /** Takes the write lock on a database and makes the local file the server head, for worker pools that move databases between machines. */
  async #acquire(name: string, options: AcquireOptions = {}): Promise<AcquireResult> {
    const body: Schemas["AcquireRequest"] = { name };
    if (options.path) body.path = absolutePath(options.path);
    if (options.maintenance) body.maintenance = true;
    if (options.discardLocal) body.discard_local = true;
    if (options.offloadOnRelease) body.offload_on_release = true;
    const offloadDurability = offloadDurabilityChoice(options.offloadDurability);
    if (offloadDurability) body.offload_durability = offloadDurability;
    if (options.machineLabel) body.machine_label = options.machineLabel;
    body.umask = requestUmask(options.umask);
    const timeoutMillis = optionalMillis(options.timeout);
    if (timeoutMillis) body.timeout_millis = timeoutMillis;
    const lockTimeoutMillis = optionalMillis(options.lockTimeout);
    if (lockTimeoutMillis) body.lock_timeout_millis = lockTimeoutMillis;
    // No timeout leaves the call unbounded, as the daemon applies its own.
    const callTimeoutMillis = timeoutMillis ? timeoutMillis + (lockTimeoutMillis ?? 0) : undefined;
    return this.call("POST", "/v1/databases/acquire", body, { signal: options.signal, timeoutMillis: callTimeoutMillis });
  }

  /** Syncs an acquired database a final time, runs its maintenance, and releases its write lock. Close every connection first, or it fails with still_open. */
  async #release(name: string, options: ReleaseOptions = {}): Promise<ReleaseResult> {
    const body: Schemas["ReleaseRequest"] = { name };
    if (options.durability) body.durability = options.durability;
    if (options.offload) body.offload = true;
    const timeoutMillis = optionalMillis(options.timeout);
    if (timeoutMillis) body.timeout_millis = timeoutMillis;
    return this.call("POST", "/v1/databases/release", body, { signal: options.signal, timeoutMillis });
  }

  /**
   * Creates a pool that opens databases with the adapter and keeps them open
   * for calls: use() for a backup rule's databases, lease() for databases
   * that move between machines, keeping the write lock between calls.
   * Close the pool when done; it releases every lease.
   */
  #pool<DB>(options: PoolOptions<DB>): Pool<DB> {
    this.ensureOpen();
    return new Pool(this, options);
  }

  /** Which machine writes a database, when its lease runs out, and whether it ran out. */
  async #writer(name: string, options: SignalOptions = {}): Promise<Writer> {
    return this.call("GET", `/v1/databases/writer?${new URLSearchParams({ name })}`, undefined, options);
  }

  /** The session, its server, and one page of databases. */
  async #status({ after = "", limit = 100, signal }: PageOptions = {}): Promise<Status> {
    checkPageLimit(limit);
    return this.call("GET", `/v1/status/show?${new URLSearchParams({ after, limit: String(limit) })}`, undefined, { signal });
  }

  /** Lists all databases. Use databases.all() to process one page at a time. */
  async #listDatabases(options: SignalOptions = {}): Promise<DatabaseStatus[]> {
    const databases: DatabaseStatus[] = [];
    for await (const database of this.#allDatabases(options)) {
      databases.push(database);
    }
    return databases;
  }

  /** Yields every database on this machine, a page at a time. */
  async *#allDatabases(options: PageOptions = {}): AsyncGenerator<DatabaseStatus> {
    let after = options.after ?? "";
    for (;;) {
      const page = await this.#databasesPage({ after, limit: options.limit, signal: options.signal });
      yield* page.databases;
      after = page.next ?? "";
      if (!after) return;
    }
  }

  /** One page of databases. Pass the returned next cursor as after to continue. */
  async #databasesPage({ after = "", limit = 100, signal }: PageOptions = {}): Promise<DatabaseStatusList> {
    checkPageLimit(limit);
    return this.call("GET", `/v1/databases/list?${new URLSearchParams({ after, limit: String(limit) })}`, undefined, { signal });
  }

  /** One database: its role, state, and the versions the server and the bucket hold. */
  async #database(name: string, options: SignalOptions = {}): Promise<DatabaseStatus> {
    return this.call("GET", `/v1/databases/status?${new URLSearchParams({ name })}`, undefined, options);
  }

  /** Syncs a database now and resolves once the server acknowledged it. */
  async #sync(name: string, options: SignalOptions = {}): Promise<DatabaseStatus> {
    return this.call("POST", "/v1/databases/sync", { name }, options);
  }

  /** Removes the local file of a released database once the server holds its last version, or the bucket with durability cloud; acquire brings it back. */
  async #offload(name: string, options: OffloadOptions = {}): Promise<DatabaseStatus> {
    const body: Schemas["OffloadRequest"] = { name };
    offloadDurabilityChoice(options.durability);
    if (options.durability) body.durability = options.durability;
    return this.call("POST", "/v1/databases/offload", body, options);
  }

  /** One page of events after an ID, waiting up to wait for the first. */
  async #eventPage(options: EventPageOptions = {}): Promise<EventList> {
    const query = new URLSearchParams();
    if (options.after) {
      // -1 answers no events and, as next, where the log ends.
      query.set("after", String(options.after));
    }
    const waitMillis = optionalMillis(options.wait);
    if (waitMillis) query.set("wait_millis", String(waitMillis));
    if (options.limit) query.set("limit", String(options.limit));
    const suffix = query.size > 0 ? `?${query}` : "";
    return this.call("GET", `/v1/events/list${suffix}`, undefined, { signal: options.signal, timeoutMillis: waitMillis });
  }

  /**
   * Yields events, oldest first, by long-polling the daemon until the caller
   * stops iterating or the signal aborts. A failed long-poll is retried with
   * backoff from the same cursor; sdk_unsupported, closed, cancelled,
   * invalid_argument, and cursor_expired (the cursor is older than the oldest
   * event kept; choose a new one) end the watch by throwing.
   */
  async *#watchEvents(options: EventsOptions = {}): AsyncGenerator<Event> {
    let after = options.after ?? 0;
    const wait = optionalMillis(options.wait) ?? eventWaitMillis;
    let retry = firstRetryMillis;
    for (;;) {
      let page: EventList;
      try {
        page = await this.#eventPage({ after, wait, signal: options.signal });
      } catch (error) {
        const failure = error instanceof LitebackupError ? error : new LitebackupError("connection_failed", String(error), 0, { cause: error });
        if (options.signal?.aborted || watchEndingCodes.has(failure.code)) {
          throw failure;
        }
        options.onError?.(failure);
        try {
          await delay(retry, undefined, { signal: options.signal ? AbortSignal.any([options.signal, this.lifetime.signal]) : this.lifetime.signal });
        } catch {
          this.ensureOpen();
          throw new LitebackupError("cancelled", "the watch was cancelled", 0, { cause: options.signal?.reason });
        }
        retry = Math.min(retry * 2, maximumRetryMillis);
        continue;
      }
      retry = firstRetryMillis;
      for (const event of page.events) {
        after = Math.max(after, event.id);
        yield event;
      }
      after = Math.max(after, page.next);
    }
  }

  /** Syncs every database a final time, releases every write lock, ends the session, and stops the daemon. A backup another machine writes holds no lock and stays waiting. Each database it could not sync or release is in failed, with its code, and keeps any write lock it holds; the session and the daemon then stay. An empty failed is the only complete shutdown. close() never does this. */
  async #shutdown(options: ShutdownOptions = {}): Promise<ShutdownResult> {
    const body: Schemas["ShutdownRequest"] = {};
    if (options.durability) body.durability = options.durability;
    const timeoutMillis = optionalMillis(options.timeout);
    if (timeoutMillis) body.timeout_millis = timeoutMillis;
    return this.call("POST", "/v1/session/shutdown", body, { signal: options.signal, timeoutMillis });
  }

  /**
   * Stops watching failovers and events and ends calls in flight. The daemon
   * keeps running, and so do its failovers and acquired databases. Calls
   * after close fail with closed.
   */
  close(): void {
    this.closed = true;
    for (const failover of this.watches) {
      failover.stopFollowing();
    }
    this.lifetime.abort();
    this.watches.clear();
  }
}
