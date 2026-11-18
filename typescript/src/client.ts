import { isAbsolute, resolve } from "node:path";

import { runBackup, sourcePath, type StartBackupOptions, type BackupOptions, type BackupSource } from "./backup.js";
import { camelize } from "./case.js";
import { milliseconds, type Duration } from "./duration.js";
import { Failover, type FailoverOptions } from "./failover.js";
import type { components } from "./generated/client.js";
import { launchDaemon } from "./launch.js";
import { sessionSocket } from "./paths.js";
import { socketFetch } from "./socket.js";
import {
  LitebackupError,
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

/** Options for connecting to a Litebackup session. */
export interface ConnectOptions {
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
}

/** Options for acquire. */
export interface AcquireOptions {
  /** Local file; relative paths resolve against the working directory. Defaults to the path this machine uses, which brings an offloaded database back. */
  to?: string;
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
export interface ReleaseOptions {
  /** server returns once the final sync is acknowledged; cloud waits for the bucket too. */
  wait?: Durability;
  /** Remove the local file once the bucket holds the released version. */
  offload?: boolean;
  /** How long to wait for the database to close and for the chosen durability. */
  timeout?: Duration;
}

/** Options for replicate. */
export interface ReplicateOptions {
  /** Replica file; relative paths resolve against the working directory. Defaults to the path this machine uses. */
  to?: string;
  /** lock (default) makes app writes fail with SQLITE_BUSY; detect rebases after a local write; none trusts apps to open with mode=ro. */
  writeProtection?: WriteProtection;
  /** Bound on downloading the first version. */
  timeout?: Duration;
}

/** Options for replicating a namespace; registration does not wait for downloads. */
export interface ReplicatePrefixOptions {
  /** Directory receiving each database at its name relative to the prefix. */
  to: string;
  writeProtection?: WriteProtection;
}

/** Options for restore: a version, a time, or neither for the head. */
export interface RestoreOptions {
  /** The new file; relative paths resolve against the working directory. Its directory must exist and the file must not. */
  to: string;
  version?: number;
  /** Restore the newest version at or before this time. */
  time?: Date | string;
}

/** Options for versions. */
export interface VersionsOptions {
  /** Skip versions up to and including this one. */
  after?: number;
  /** Return at most this many; every version when unset. */
  limit?: number;
}

/** Options for events. */
export interface EventsOptions {
  /** Return events with IDs greater than this one; 0 starts at the oldest. */
  after?: number;
  /** How long each long-poll waits for a new event; defaults to 30s. */
  wait?: Duration;
  signal?: AbortSignal;
}

/** Options for shutdown. */
export interface ShutdownOptions {
  wait?: "none" | Durability;
  timeout?: Duration;
}

/** Resolves a path against the working directory; the daemon needs absolute paths. */
export function absolutePath(path: string): string {
  return isAbsolute(path) ? resolve(path) : resolve(process.cwd(), path);
}

/** Talks to the Litebackup daemon of one session over its unix socket. */
export class Litebackup {
  private sessionName: string;
  get session(): string { return this.sessionName; }
  readonly socketPath: string;
  /** The daemon's version, known after the first call. */
  clientVersion = "";

  /** Backups and failovers running on this machine. */
  readonly backups: {
    list(options?: { signal?: AbortSignal }): Promise<BackupInfo[]>;
  };
  /** Prefix replicas and replicas on this machine. */
  readonly replicas: {
    list(): Promise<ReplicaList>;
    stop(name: string): Promise<void>;
    stopPrefix(prefix: string, to: string): Promise<void>;
  };
  /** Conflict copies, saved because they diverged from the server: list and delete. */
  readonly conflicts: {
    list(): Promise<Conflict[]>;
    delete(id: string): Promise<void>;
  };

  /** @internal */
  /** Failovers this client follows; close() stops following them. @internal */
  readonly failovers = new Set<Failover>();
  /** @internal */
  closed = false;

  /** Use Litebackup.connect, which starts the daemon when needed. */
  private constructor(session: string, socketPath: string, private readonly environment: NodeJS.ProcessEnv) {
    this.sessionName = session;
    this.socketPath = socketPath;
    this.backups = {
      list: async (options = {}) => (await this.call<Schemas["BackupList"]>("GET", "/v1/backup-rules/list", undefined, options.signal)).backups,
    };
    this.replicas = {
      list: () => this.call("GET", "/v1/replicas/list"),
      stopPrefix: async (prefix, to) => {
        if (!to) {
          throw new LitebackupError("invalid_argument", "stopping a prefix replica needs a directory", 0);
        }
        await this.call("POST", "/v1/replicas/stop", { prefix, to: absolutePath(to) });
      },
      stop: async (name) => {
        await this.call("POST", "/v1/replicas/stop", { name });
      },
    };
    this.conflicts = {
      list: async () => (await this.call<Schemas["ConflictList"]>("GET", "/v1/conflicts/list")).conflicts,
      delete: async (id) => {
        await this.call("POST", "/v1/conflicts/delete", { id });
      },
    };
  }

  /** Connects to a session, starting its daemon like the CLI does. */
  static async connect(options: ConnectOptions = {}): Promise<Litebackup> {
    const environment = options.environment ?? process.env;
    let session = options.session ?? "";
    let socketPath = options.socketPath;
    if (!socketPath) {
      const resolved = sessionSocket(options.session, environment);
      session = resolved.session;
      socketPath = resolved.socketPath;
      if (options.launch !== false) {
        await launchDaemon({ session, socketPath, environment, token: options.token });
      }
    }
    const litebackup = new Litebackup(session, socketPath, environment);
    const status = await litebackup.status();
    if (!session) litebackup.sessionName = status.session;
    return litebackup;
  }

  /**
   * Sends one call and decodes its JSON answer into camelCase, throwing
   * LitebackupError on failure.
   * @internal
   */
  async call<T>(method: "GET" | "POST", path: string, body?: unknown, signal?: AbortSignal): Promise<any> {
    const response = await socketFetch({
      socketPath: this.socketPath,
      method,
      path,
      headers: { [sdkVersionHeader]: `typescript/${sdkVersion}` },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
    this.clientVersion = response.headers[clientVersionHeader] ?? this.clientVersion;
    if (response.status !== 200) {
      let code = `http_${response.status}`;
      let message = response.body.trim();
      try {
        const decoded = JSON.parse(response.body) as { code?: string; message?: string };
        code = decoded.code ?? code;
        message = decoded.message ?? message;
      } catch {
        // A non-JSON error body keeps its text as the message.
      }
      throw new LitebackupError(code, message, response.status);
    }
    return camelize(JSON.parse(response.body) as T);
  }

  /** Persist a continuous rule. Discovery runs in the daemon. */
  async startBackup(source: BackupSource, options: StartBackupOptions = {}): Promise<BackupInfo> {
    return this.call("POST", "/v1/backup-rules/start", {
      path: sourcePath(source),
      name: options.name,
      prefix: options.prefix,
      maximum_local_databases: options.maximumLocalDatabases,
    });
  }

  /** Stop a single-file backup on this machine by its exact remote database name. */
  stopBackup(name: string): Promise<{ leasePending: boolean }> {
    return this.call("POST", "/v1/backup-rules/stop", { name });
  }

  /** Stop the whole backup rule, including future matches, using the ID returned by startBackup. */
  stopBackupRule(ruleId: string): Promise<{ leasePending: boolean }> {
    return this.call("POST", "/v1/backup-rules/stop", { rule_id: ruleId });
  }

  backup(source: BackupSource, options: BackupOptions = {}): Promise<void> {
    return runBackup(source, { ...options, session: this.session }, this.environment);
  }

  /**
   * Runs a database on several identical machines: the first to take the
   * write lock is the origin, the others keep live readonly replicas at the
   * same path and take over when the origin's lease runs out.
   */
  failover(path: string, options: FailoverOptions = {}): Promise<Failover> {
    return Failover.start(this, path, options);
  }

  /** Keeps a live readonly replica of a database and resolves once its first version is local. */
  async replicate(name: string, options: ReplicateOptions = {}): Promise<DatabaseStatus> {
    const body: Schemas["ReplicaRequest"] = { name };
    if (options.to) {
      body.to = absolutePath(options.to);
    }
    if (options.writeProtection) {
      body.write_protection = options.writeProtection;
    }
    if (options.timeout !== undefined) {
      body.timeout_millis = milliseconds(options.timeout);
    }
    const result = (await this.call("POST", "/v1/replicas/start", body)) as { database?: DatabaseStatus };
    if (!result.database) {
      throw new LitebackupError("invalid_response", "the daemon did not return the replica", 200);
    }
    return result.database;
  }

  /** Registers live readonly replicas of current and future databases under a prefix. An empty prefix follows all databases. */
  async replicatePrefix(prefix: string, options: ReplicatePrefixOptions): Promise<PrefixReplica> {
    if (!options?.to) {
      throw new LitebackupError("invalid_argument", "replicating a prefix needs a directory", 0);
    }
    const body: Schemas["ReplicaRequest"] = { prefix, to: absolutePath(options.to) };
    if (options.writeProtection) {
      body.write_protection = options.writeProtection;
    }
    const result = (await this.call("POST", "/v1/replicas/start", body)) as { prefix?: PrefixReplica };
    if (!result.prefix) {
      throw new LitebackupError("invalid_response", "the daemon did not return the prefix replica", 200);
    }
    return result.prefix;
  }

  /** Writes a version of a database to a new file and checks it. An existing file is never overwritten (exists). */
  restore(name: string, options: RestoreOptions): Promise<RestoreResult> {
    if (!options?.to) {
      throw new LitebackupError("invalid_argument", "restore needs a path to write to", 0);
    }
    if (options.version !== undefined && options.time !== undefined) {
      throw new LitebackupError("invalid_argument", "restore takes a version or a time, not both", 0);
    }
    const body: Schemas["RestoreRequest"] = { name, path: absolutePath(options.to) };
    if (options.version !== undefined) {
      body.version = options.version;
    }
    if (options.time !== undefined) {
      body.time = options.time instanceof Date ? options.time.toISOString() : options.time;
    }
    return this.call("POST", "/v1/databases/restore", body);
  }

  /** The versions of a database the server can restore, oldest first. */
  async versions(name: string, options: VersionsOptions = {}): Promise<Version[]> {
    const versions: Version[] = [];
    let after = options.after ?? 0;
    for (;;) {
      const query = new URLSearchParams({ name });
      if (after > 0) {
        query.set("after", String(after));
      }
      if (options.limit !== undefined) {
        query.set("limit", String(options.limit - versions.length));
      }
      const page = (await this.call("GET", `/v1/databases/versions?${query}`)) as { versions: Version[]; next?: number };
      versions.push(...page.versions);
      const limited = options.limit !== undefined && versions.length >= options.limit;
      if (!page.next || page.next <= after || page.versions.length === 0 || limited) {
        return versions;
      }
      after = page.next;
    }
  }

  /** Takes the write lock on a database and makes the local file the server head, for worker pools that move databases between machines. */
  acquire(name: string, options: AcquireOptions = {}): Promise<AcquireResult> {
    const body: Schemas["AcquireRequest"] = { name };
    if (options.to) {
      body.path = absolutePath(options.to);
    }
    if (options.maintenance !== undefined) {
      body.maintenance = options.maintenance;
    }
    if (options.offload) {
      body.offload = options.offload;
    }
    if (options.machineLabel) {
      body.machine_label = options.machineLabel;
    }
    if (options.onLost) {
      body.on_lost = options.onLost;
    }
    if (options.timeout !== undefined) {
      body.timeout_millis = milliseconds(options.timeout);
    }
    return this.call("POST", "/v1/databases/acquire", body);
  }

  /** Syncs an acquired database a final time, runs its maintenance, and releases its write lock. */
  release(name: string, options: ReleaseOptions = {}): Promise<ReleaseResult> {
    const body: Schemas["ReleaseRequest"] = { name };
    if (options.wait) {
      body.wait = options.wait;
    }
    if (options.offload !== undefined) {
      body.offload = options.offload;
    }
    if (options.timeout !== undefined) {
      body.timeout_millis = milliseconds(options.timeout);
    }
    return this.call("POST", "/v1/databases/release", body);
  }

  /** Which machine writes a database, when its lease runs out, and whether it ran out. */
  writer(name: string): Promise<Writer> {
    return this.call("GET", `/v1/databases/writer?${new URLSearchParams({ name })}`);
  }

  /** The session, its server, and one page of databases. */
  status(after = "", limit = 100): Promise<Status> {
    return this.call("GET", `/v1/status/show?${new URLSearchParams({ after, limit: String(limit) })}`);
  }

  /** Lists all databases. Use databasesPage() to process one page at a time. */
  async databases(): Promise<DatabaseStatus[]> {
    const databases: DatabaseStatus[] = [];
    let after = "";
    do {
      const page = await this.databasesPage(after);
      databases.push(...page.databases);
      after = page.next ?? "";
    } while (after);
    return databases;
  }

  /** Pass the returned next cursor as after to continue. */
  databasesPage(after = "", limit = 100): Promise<DatabasePage> {
    return this.call("GET", `/v1/databases/list?${new URLSearchParams({ after, limit: String(limit) })}`);
  }

  /** One database: its role, state, and the versions the server and the bucket hold. */
  database(name: string, options: { signal?: AbortSignal } = {}): Promise<DatabaseStatus> {
    return this.call("GET", `/v1/databases/status?${new URLSearchParams({ name })}`, undefined, options.signal);
  }

  /** Syncs a database now and resolves once the server acknowledged it. */
  sync(name: string, options: { signal?: AbortSignal } = {}): Promise<DatabaseStatus> {
    return this.call("POST", "/v1/databases/sync", { name }, options.signal);
  }

  /** Removes the local file of a released database once the bucket holds its last version; acquire brings it back. */
  offload(name: string): Promise<DatabaseStatus> {
    return this.call("POST", "/v1/databases/offload", { name });
  }

  /** One page of events after an ID, waiting up to waitMillis for the first. */
  async eventPage(options: { after?: number; waitMillis?: number; limit?: number; signal?: AbortSignal } = {}): Promise<EventList> {
    const query = new URLSearchParams();
    if (options.after) {
      // -1 answers no events and, as next, where the log ends.
      query.set("after", String(options.after));
    }
    if (options.waitMillis) {
      query.set("wait_millis", String(options.waitMillis));
    }
    if (options.limit) {
      query.set("limit", String(options.limit));
    }
    const suffix = query.size > 0 ? `?${query}` : "";
    return this.call("GET", `/v1/events/list${suffix}`, undefined, options.signal);
  }

  /** Yields events, oldest first, by long-polling the daemon until the signal aborts. */
  async *events(options: EventsOptions = {}): AsyncGenerator<LitebackupEvent> {
    let after = options.after ?? 0;
    const waitMillis = milliseconds(options.wait ?? 30_000);
    while (!options.signal?.aborted) {
      const page = await this.eventPage({ after, waitMillis, signal: options.signal });
      for (const event of page.events) {
        yield event;
      }
      after = Math.max(after, page.next);
    }
  }

  /** Syncs every database a final time, releases every write lock, ends the session, and stops the daemon. */
  shutdown(options: ShutdownOptions = {}): Promise<ShutdownResult> {
    const body: Schemas["ShutdownRequest"] = {};
    if (options.wait) {
      body.wait = options.wait;
    }
    if (options.timeout !== undefined) {
      body.timeout_millis = milliseconds(options.timeout);
    }
    return this.call("POST", "/v1/session/shutdown", body);
  }

  /** Stops watching failovers. The daemon keeps running. */
  close(): void {
    this.closed = true;
    for (const failover of this.failovers) {
      failover.end();
    }
    this.failovers.clear();
  }
}
