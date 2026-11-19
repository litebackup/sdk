import { setTimeout as delay } from "node:timers/promises";

import { runBackup, sourcePath, type StartBackupOptions, type BackupOptions, type BackupSource } from "./backup.js";
import { camelize } from "./case.js";
import { milliseconds, type Duration } from "./duration.js";
import { Failover, type FailoverOptions } from "./failover.js";
import type { components } from "./generated/client.js";
import { launchDaemon } from "./launch.js";
import { absolutePath, wireTime, type PathSource } from "./location.js";
import { runManaged, type ManagedOptions, type ManagedScope } from "./managed.js";
import { sessionSocket } from "./paths.js";
import { socketFetch } from "./socket.js";
import {
  LitebackupError,
  compareVersions,
  minimumDaemonVersion,
  responseError,
  sdkVersion,
  type AcquireResult,
  type BackupInfo,
  type Conflict,
  type DatabaseStatus,
  type DatabasePage,
  type Durability,
  type EventList,
  type LitebackupEvent,
  type PrefixReplica,
  type ReleaseResult,
  type ReplicaList,
  type RestoreResult,
  type ShutdownResult,
  type Status,
  type Version,
  type WriteProtection,
  type Writer,
} from "./types.js";

type Schemas = components["schemas"];

/** Header names of the daemon's version negotiation. */
const sdkVersionHeader = "x-litebackup-sdk-version";
const clientVersionHeader = "x-litebackup-client-version";

/** Added to a call's server-side timeout for its client-side deadline, so the daemon's own timeout answer arrives first. */
const deadlineGraceMillis = 30_000;
/** How long one events long-poll waits unless told otherwise. */
const eventWaitMillis = 30_000;
/** Bounds on the backoff between failed event long-polls. */
const firstRetryMillis = 250;
const maximumRetryMillis = 10_000;

/** Options for connecting to a Litebackup session. */
export interface LitebackupOptions {
  /**
   * Join token, TOKEN@HOST, the daemon joins with on its first start;
   * defaults to LITEBACKUP_TOKEN. A daemon that already joined keeps its join.
   */
  token?: string;
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
  to?: PathSource;
  /** Checkpoint on release and VACUUM past the freelist threshold. */
  maintenance?: boolean;
  /** "released" lets Litebackup remove the released copy once the bucket holds it. */
  offload?: "never" | "released";
  /** What other machines see as the writer. */
  machineLabel?: string;
  /** On a lost write lock, keep the copy as conflicted (default) or block local writes until the lock is back. */
  onLost?: "conflict" | "block";
  /** Bound on downloading the head. */
  timeout?: Duration;
}

/** Options for release. */
export interface ReleaseOptions extends SignalOptions {
  /** server returns once the final sync is acknowledged; cloud waits for the bucket too. */
  wait?: Durability;
  /** Remove the local file once the bucket holds the released version. */
  offload?: boolean;
  /** How long to wait for the database to close and for the chosen durability. */
  timeout?: Duration;
}

/** Options for replicate. */
export interface ReplicateOptions extends SignalOptions {
  /** Replica file or file: URL; relative paths resolve against the working directory. Defaults to the path this machine uses. */
  to?: PathSource;
  /** lock (default) makes app writes fail with SQLITE_BUSY; detect rebases after a local write; none trusts apps to open with mode=ro. */
  writeProtection?: WriteProtection;
  /** Bound on downloading the first version. */
  timeout?: Duration;
}

/** Options for replicating a namespace; registration does not wait for downloads. */
export interface ReplicatePrefixOptions extends SignalOptions {
  /** Directory receiving each database at its name relative to the prefix. */
  to: PathSource;
  writeProtection?: WriteProtection;
}

/** Options for restore: a version, a time, or neither for the head. */
export interface RestoreOptions extends SignalOptions {
  /** The new file or file: URL; relative paths resolve against the working directory. Its directory must exist and the file must not. */
  to: PathSource;
  version?: number;
  /** Restore the newest version at or before this time. */
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
  after?: string;
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
  wait?: "none" | Durability;
  timeout?: Duration;
}

/** Options for one socket call. @internal */
export interface CallOptions {
  signal?: AbortSignal;
  /** The call's server-side timeout in milliseconds; its client-side deadline is this plus a grace period. */
  timeoutMillis?: number;
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
    list: async (options: SignalOptions = {}): Promise<BackupInfo[]> =>
      (await this.call<Schemas["BackupList"]>("GET", "/v1/backup-rules/list", undefined, options)).backups,
    stop: this.#stopBackupRule.bind(this),
  };
  /** Databases on this machine and on the server. */
  readonly databases = {
    get: this.#database.bind(this),
    list: this.#listDatabases.bind(this),
    page: this.#databasesPage.bind(this),
    iterate: this.#iterateDatabases.bind(this),
    versions: this.#versions.bind(this),
    iterateVersions: this.#iterateVersions.bind(this),
    restore: this.#restore.bind(this),
    sync: this.#sync.bind(this),
    offload: this.#offload.bind(this),
    writer: this.#writer.bind(this),
    acquire: this.#acquire.bind(this),
    release: this.#release.bind(this),
    managed: this.#managed.bind(this),
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
    stopPrefix: async (prefix: string, to: PathSource, options: SignalOptions = {}): Promise<void> => {
      if (!to) {
        throw new LitebackupError("invalid_argument", "stopping a prefix replica needs a directory", 0);
      }
      await this.call("POST", "/v1/replicas/stop", { prefix, to: absolutePath(to) }, options);
    },
  };
  /** Conflict copies, saved because they diverged from the server: list and delete. */
  readonly conflicts = {
    list: async (options: SignalOptions = {}): Promise<Conflict[]> => (await this.call<Schemas["ConflictList"]>("GET", "/v1/conflicts/list", undefined, options)).conflicts,
    delete: async (id: string, options: SignalOptions = {}): Promise<void> => {
      await this.call("POST", "/v1/conflicts/delete", { id }, options);
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
   * incompatible_version when the daemon is older than minimumDaemonVersion.
   */
  async connect(): Promise<void> {
    this.ensureOpen();
    this.connection ??= this.#open().catch((error) => {
      this.connection = undefined;
      if (error instanceof LitebackupError) throw error;
      throw new LitebackupError("connection_failed", String(error), 0, { cause: error });
    });
    await this.connection;
    this.ensureOpen();
  }

  private ensureOpen(): void {
    if (this.closed) throw new LitebackupError("closed", "the client is closed", 0);
  }

  private environment(): NodeJS.ProcessEnv {
    const env = this.targetEnvironment ?? this.options.environment ?? process.env;
    const token = this.options.token ?? env.LITEBACKUP_TOKEN;
    if (token?.trim().startsWith("litebackup_") && !token.trim().startsWith("litebackup_join_")) {
      throw new LitebackupError("invalid_argument", "Get a join token from Machines → Add machine", 0);
    }
    this.targetEnvironment ??= { ...env };
    return { ...this.targetEnvironment, LITEBACKUP_TOKEN: token };
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
        await launchDaemon({ ...target, environment, binaryPath: this.options.binaryPath });
      }
    }
    await this.request("GET", "/v1/status/show?limit=1");
    if (this.clientVersion && compareVersions(this.clientVersion, minimumDaemonVersion) >= 0) {
      return;
    }
    const reported = this.clientVersion ? `is version ${this.clientVersion}` : "does not report its version";
    const drain = session ? `litebackup drain --session ${session}` : "litebackup drain";
    throw new LitebackupError(
      "incompatible_version",
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
    await this.connect();
    return this.request<T>(method, path, body, options);
  }

  private async request<T>(method: "GET" | "POST", path: string, body?: unknown, options: CallOptions = {}): Promise<any> {
    const signals = [this.lifetime.signal];
    if (options.signal) signals.push(options.signal);
    const deadline = options.timeoutMillis ? AbortSignal.timeout(options.timeoutMillis + deadlineGraceMillis) : undefined;
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
   * once the daemon saved it; discovery and uploads run in the background.
   * Starting the same path again with the same settings returns the saved
   * rule, so call it on every start. Other settings for a saved path fail with
   * exists, and the error's details.rule is the saved rule.
   */
  async #startBackup(source: BackupSource, options: StartBackupOptions = {}): Promise<BackupInfo> {
    if (options.name && options.prefix) {
      throw new LitebackupError("invalid_argument", "choose a name or a prefix", 0);
    }
    const body: Schemas["BackupRequest"] = { path: sourcePath(source) };
    if (options.name) body.name = options.name;
    if (options.prefix) body.prefix = options.prefix;
    if (options.maximumLocalDatabases) body.maximum_local_databases = options.maximumLocalDatabases;
    return this.call("POST", "/v1/backup-rules/start", body, options);
  }

  /** Stop a single-file backup on this machine by its exact remote database name. */
  async #stopBackup(name: string, options: SignalOptions = {}): Promise<{ leasePending: boolean }> {
    return this.call("POST", "/v1/backup-rules/stop", { name }, options);
  }

  /** Stop the whole backup rule, including future matches, using the ID returned by backups.start. */
  async #stopBackupRule(ruleId: string, options: SignalOptions = {}): Promise<{ leasePending: boolean }> {
    return this.call("POST", "/v1/backup-rules/stop", { rule_id: ruleId }, options);
  }

  /** Runs one foreground backup job and resolves once it exits. */
  async #backup(source: BackupSource, options: BackupOptions = {}): Promise<void> {
    this.ensureOpen();
    if (this.options.socketPath !== undefined) throw new LitebackupError("invalid_argument", "foreground backup needs a named session and cannot target socketPath", 0);
    const signal = options.signal ? AbortSignal.any([options.signal, this.lifetime.signal]) : this.lifetime.signal;
    return runBackup(source, { ...options, signal }, this.environment(), this.options.session, this.options.binaryPath);
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
    if (options.to) body.to = absolutePath(options.to);
    if (options.writeProtection) body.write_protection = options.writeProtection;
    const timeoutMillis = optionalMillis(options.timeout);
    if (timeoutMillis) body.timeout_millis = timeoutMillis;
    const result = (await this.call("POST", "/v1/replicas/start", body, { signal: options.signal, timeoutMillis })) as { database?: DatabaseStatus };
    if (!result.database) {
      throw new LitebackupError("invalid_response", "the daemon did not return the replica", 200);
    }
    return result.database;
  }

  /** Registers live readonly replicas of current and future databases under a prefix. An empty prefix follows all databases. */
  async #replicatePrefix(prefix: string, options: ReplicatePrefixOptions): Promise<PrefixReplica> {
    if (!options?.to) {
      throw new LitebackupError("invalid_argument", "replicating a prefix needs a directory", 0);
    }
    const body: Schemas["ReplicaRequest"] = { prefix, to: absolutePath(options.to) };
    if (options.writeProtection) body.write_protection = options.writeProtection;
    const result = (await this.call("POST", "/v1/replicas/start", body, options)) as { prefix?: PrefixReplica };
    if (!result.prefix) {
      throw new LitebackupError("invalid_response", "the daemon did not return the prefix replica", 200);
    }
    return result.prefix;
  }

  /** Writes a version of a database to a new file and checks it. An existing file is never overwritten (exists). */
  async #restore(name: string, options: RestoreOptions): Promise<RestoreResult> {
    if (!options?.to) {
      throw new LitebackupError("invalid_argument", "restore needs a path to write to", 0);
    }
    if (options.version !== undefined && options.time !== undefined) {
      throw new LitebackupError("invalid_argument", "restore takes a version or a time, not both", 0);
    }
    const body: Schemas["RestoreRequest"] = { name, path: absolutePath(options.to) };
    if (options.version !== undefined) body.version = options.version;
    if (options.time !== undefined) body.time = wireTime(options.time);
    return this.call("POST", "/v1/databases/restore", body, options);
  }

  /** The versions of a database the server can restore, oldest first. */
  async #versions(name: string, options: VersionsOptions = {}): Promise<Version[]> {
    const versions: Version[] = [];
    for await (const version of this.#iterateVersions(name, options)) {
      versions.push(version);
    }
    return versions;
  }

  /** Yields the versions of a database the server can restore, oldest first, a page at a time. */
  async *#iterateVersions(name: string, options: VersionsOptions = {}): AsyncGenerator<Version> {
    if (options.limit === 0) return;
    let after = options.after ?? 0;
    let yielded = 0;
    for (;;) {
      const query = new URLSearchParams({ name });
      if (after > 0) query.set("after", String(after));
      if (options.limit !== undefined) query.set("limit", String(options.limit - yielded));
      const page = (await this.call("GET", `/v1/databases/versions?${query}`, undefined, options)) as { versions: Version[]; next?: number };
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
    if (options.to) body.path = absolutePath(options.to);
    if (options.maintenance) body.maintenance = true;
    if (options.offload === "released") body.offload = "released";
    if (options.machineLabel) body.machine_label = options.machineLabel;
    if (options.onLost === "block") body.on_lost = "block";
    const timeoutMillis = optionalMillis(options.timeout);
    if (timeoutMillis) body.timeout_millis = timeoutMillis;
    return this.call("POST", "/v1/databases/acquire", body, { signal: options.signal, timeoutMillis });
  }

  /** Syncs an acquired database a final time, runs its maintenance, and releases its write lock. Close every connection first, or it fails with still_open. */
  async #release(name: string, options: ReleaseOptions = {}): Promise<ReleaseResult> {
    const body: Schemas["ReleaseRequest"] = { name };
    if (options.wait) body.wait = options.wait;
    if (options.offload) body.offload = true;
    const timeoutMillis = optionalMillis(options.timeout);
    if (timeoutMillis) body.timeout_millis = timeoutMillis;
    return this.call("POST", "/v1/databases/release", body, { signal: options.signal, timeoutMillis });
  }

  /**
   * Acquires a database, opens it with the adapter, runs work, closes it, and
   * releases it with the chosen durability; resolves with work's result.
   * Close and release run under their own cleanupTimeout even when the
   * signal aborts. Failed work still closes and releases, without offload. A
   * failed close does not release: the database stays acquired with its file
   * untouched. Several failures are thrown together as an AggregateError.
   */
  async #managed<Connection, Result>(
    name: string,
    options: ManagedOptions<Connection>,
    work: (connection: Connection, scope: ManagedScope) => Promise<Result> | Result,
  ): Promise<Result> {
    return runManaged(this, name, options, work);
  }

  /** Which machine writes a database, when its lease runs out, and whether it ran out. */
  async #writer(name: string, options: SignalOptions = {}): Promise<Writer> {
    return this.call("GET", `/v1/databases/writer?${new URLSearchParams({ name })}`, undefined, options);
  }

  /** The session, its server, and one page of databases. */
  async #status({ after = "", limit = 100, signal }: PageOptions = {}): Promise<Status> {
    return this.call("GET", `/v1/status/show?${new URLSearchParams({ after, limit: String(limit) })}`, undefined, { signal });
  }

  /** Lists all databases. Use databases.iterate() to process one page at a time. */
  async #listDatabases(options: SignalOptions = {}): Promise<DatabaseStatus[]> {
    const databases: DatabaseStatus[] = [];
    for await (const database of this.#iterateDatabases(options)) {
      databases.push(database);
    }
    return databases;
  }

  /** Yields every database on this machine, a page at a time. */
  async *#iterateDatabases(options: PageOptions = {}): AsyncGenerator<DatabaseStatus> {
    let after = options.after ?? "";
    for (;;) {
      const page = await this.#databasesPage({ after, limit: options.limit, signal: options.signal });
      yield* page.databases;
      after = page.next ?? "";
      if (!after) return;
    }
  }

  /** One page of databases. Pass the returned next cursor as after to continue. */
  async #databasesPage({ after = "", limit = 100, signal }: PageOptions = {}): Promise<DatabasePage> {
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

  /** Removes the local file of a released database once the bucket holds its last version; acquire brings it back. */
  async #offload(name: string, options: SignalOptions = {}): Promise<DatabaseStatus> {
    return this.call("POST", "/v1/databases/offload", { name }, options);
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
   * backoff from the same cursor; sdk_unsupported, closed, and cancelled end
   * the watch by throwing.
   */
  async *#watchEvents(options: EventsOptions = {}): AsyncGenerator<LitebackupEvent> {
    let after = options.after ?? 0;
    const wait = optionalMillis(options.wait) ?? eventWaitMillis;
    let retry = firstRetryMillis;
    for (;;) {
      let page: EventList;
      try {
        page = await this.#eventPage({ after, wait, signal: options.signal });
      } catch (error) {
        const failure = error instanceof LitebackupError ? error : new LitebackupError("connection_failed", String(error), 0, { cause: error });
        if (options.signal?.aborted || ["sdk_unsupported", "closed", "cancelled", "invalid_argument"].includes(failure.code)) {
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

  /** Syncs every database a final time, releases every write lock, ends the session, and stops the daemon. close() never does this. */
  async #shutdown(options: ShutdownOptions = {}): Promise<ShutdownResult> {
    const body: Schemas["ShutdownRequest"] = {};
    if (options.wait) body.wait = options.wait;
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
      failover.end();
    }
    this.lifetime.abort();
    this.watches.clear();
  }
}
