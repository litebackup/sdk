import { existsSync } from "node:fs";
import { mkdir, open } from "node:fs/promises";
import { dirname } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

import type { Litebackup, SignalOptions } from "./client.js";
import { milliseconds, type Duration } from "./duration.js";
import { absolutePath, type PathSource } from "./location.js";
import { timeable } from "./timers.js";
import { DatabaseStates, Durabilities, ErrorCode, JournalModes, LitebackupError, Roles, type BackupRule, type Durability, type LitebackupErrorOptions } from "./types.js";

/** Bounds closing and releasing a leased database unless cleanupTimeout says otherwise. */
export const defaultCleanupTimeout = "10m";

/**
 * How long a database whose backup rule offloads (maximumLocalDatabases)
 * stays open unused before the pool closes it, so the rule can offload it,
 * unless idleClose says otherwise. It is well inside the client's 10-minute
 * idle window.
 */
export const defaultOffloadIdleClose = "2m";

/** How long a lease stays unused before the pool releases it, unless idleRelease says otherwise. */
export const defaultIdleRelease = "1m";

/** How often a lease with cloud durability asks whether the bucket holds its version yet. */
const cloudDurabilityPollMillis = 1000;

/** How often a pool looks for idle databases; tests shorten it. @internal */
export const poolTiming = { idleCheckMillis: 15_000 };

/** The step of a lease release that failed. */
export type LeaseCleanupStep = "close" | "release";

/**
 * A failed release of a leased database, with code lease_cleanup_failed and
 * the failure as its cause. step close means closing the handle failed, so
 * the database was not released: it stays acquired by this machine with its
 * file as it is; close every connection, then call pool.release() again.
 * step release means the release itself failed, as with still_open or an
 * unreachable server; the file is intact, so call pool.release() again
 * before another machine needs the database. details holds step, name,
 * path, and acquired (close) or durability (release).
 */
export class LeaseCleanupError extends LitebackupError {
  /** The step that failed. */
  readonly step: LeaseCleanupStep;

  constructor(step: LeaseCleanupStep, message: string, options: LitebackupErrorOptions = {}) {
    super(ErrorCode.leaseCleanupFailed, message, 0, options);
    this.name = "LeaseCleanupError";
    this.step = step;
  }
}

/**
 * Opens and closes the handle a pool gives to its calls. open must open an
 * existing file and never create one: a missing file is an offloaded copy
 * the pool brings back, never an empty new database, and it fails with
 * not_found. close must roll back a transaction a call left unfinished,
 * never commit it, and throw when a connection may still be open: the pool
 * releases a leased database only after close succeeds. The signal aborts
 * once the call's signal does (open) or the cleanup timeout passes (close).
 * The driver subpath exports provide adapters, such as `adapter()` from
 * `@litebackup/sdk/node-sqlite`.
 */
export interface Adapter<DB> {
  open(path: string, signal?: AbortSignal): Promise<DB> | DB;
  close(db: DB, signal?: AbortSignal): Promise<void> | void;
}

/** Options for databases.pool(). */
export interface PoolOptions<DB> {
  /** Opens and closes each database. Required. */
  adapter: Adapter<DB>;
  /**
   * Close a backup rule's database after this long without a call, to cap
   * open files. Unset keeps it open, except under a rule with
   * maximumLocalDatabases, where it is defaultOffloadIdleClose so the rule
   * can offload it. A rollback-journal database under a rule saved with
   * offloadRollbackJournal closes as soon as no use() call runs, because
   * that rule offloads it on the word that nothing keeps it open.
   */
  idleClose?: Duration;
  /**
   * Release a leased database after this long without a call: the pool
   * closes it and releases it with its durability. Defaults to 1m; 0 keeps
   * the lease until release() or close(). The app coordinates handovers:
   * another machine's lease with lockTimeout succeeds once this one is
   * released by idleRelease, release(), or close().
   */
  idleRelease?: Duration;
  /** One bound on closing and releasing a leased database together, which run even after a signal aborts. Must be positive; defaults to 10m. */
  cleanupTimeout?: Duration;
  /** What other machines see as the writer of leased databases. */
  machineLabel?: string;
  /**
   * Called with each failed release the pool started on its own, because
   * idleRelease ran out, and with each failed close of a rollback-journal
   * database after its last use() call (step close). The database stays
   * leased with its file intact, and the pool tries again the next time it
   * is asked.
   */
  onReleaseError?: (error: LeaseCleanupError) => void;
}

/** Options for pool.lease(). */
export interface LeaseOptions {
  /**
   * Local file or file: URL; the path this machine already uses when unset.
   * When nothing is there, the first call creates an empty file and its
   * directory, so a database neither the server nor this machine has
   * becomes a new one; the server's head replaces an empty file.
   */
  path?: PathSource;
  /** Skip creating path, so a database neither the server nor this machine has fails with not_found. */
  requireExisting?: boolean;
  /**
   * Resolve only once the server, or the bucket with "cloud", holds the
   * version the work committed, without releasing. Unset resolves once the
   * work returns and syncs in the background. The release that ends the
   * lease waits for the same durability, the server when unset.
   */
  durability?: Durability;
  /** Remove the local file when the lease ends, once the release's durability holds the final version, unless the last call failed. */
  offload?: boolean;
  /** How long the first call's acquire keeps trying while another machine holds the write lock; unset fails at once with lock_unavailable. */
  lockTimeout?: Duration;
  /** Bound on downloading the head. */
  acquireTimeout?: Duration;
  /** Checkpoint on release and VACUUM past the freelist threshold. */
  maintenance?: boolean;
  /** Aborts the acquire and the durability wait, and is handed to the work. Aborted by the time the work returns, the call rejects with cancelled and keeps the lease. */
  signal?: AbortSignal;
}

/**
 * The work of a pool call: it receives the open database and a signal that
 * aborts when the caller's does.
 */
export type PoolWork<DB, T> = (db: DB, signal: AbortSignal) => Promise<T> | T;

/** Serializes the calls on one database: opening, bringing back, acquiring, lease calls, and releasing. */
class CallTurn {
  private busy = false;
  private readonly waiting: (() => void)[] = [];

  /** Resolves once this caller's turn starts. */
  async start(): Promise<void> {
    if (!this.busy) {
      this.busy = true;
      return;
    }
    await new Promise<void>((resolve) => this.waiting.push(resolve));
  }

  /** Starts a turn when none is running, and says whether it did. */
  tryStart(): boolean {
    if (this.busy) return false;
    this.busy = true;
    return true;
  }

  /** Ends the turn and starts the next waiting one. */
  end(): void {
    const next = this.waiting.shift();
    if (next) next();
    else this.busy = false;
  }
}

/** One database of a pool. */
interface PoolEntry<DB> {
  name: string;
  turn: CallTurn;
  db?: DB;
  path: string;
  /** Set while db is open. */
  opened: boolean;
  /** Set while this pool holds the write lock through lease(). */
  leased: boolean;
  /** The last lease call's options, which the release uses. */
  leaseOptions: LeaseOptions;
  /** Keeps the release from offloading after a failed call. */
  lastCallFailed: boolean;
  /** How long a rule's database stays open unused; 0 never closes it. */
  idleCloseMillis: number;
  /** Set for a rollback-journal database whose rule offloads such databases: its handle closes once no use() call runs. */
  closeAfterUse: boolean;
  /** Calls that joined and have not left. */
  calls: number;
  /** When the last call left, in epoch milliseconds; 0 before any did. */
  lastUsed: number;
}

/** A duration as whole milliseconds, 0 when unset. */
function optionalMillis(duration: Duration | undefined): number {
  return duration === undefined ? 0 : milliseconds(duration);
}

/** An invalid_argument error the SDK raises itself. */
function invalidArgument(message: string): LitebackupError {
  return new LitebackupError(ErrorCode.invalidArgument, message, 0);
}

/** The message of a thrown value. */
function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Resolves a promise, or rejects once the timeout passes. */
async function withinTimeout<T>(operation: Promise<T> | T, timeoutMillis: number, description: string): Promise<T> {
  if (!timeable(timeoutMillis)) return await operation;
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`${description} did not finish within ${timeoutMillis} ms`)), timeoutMillis);
  });
  try {
    return await Promise.race([Promise.resolve(operation), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

/** Creates an empty file and its directory unless something is already at path. */
async function createEmptyFile(path: string): Promise<void> {
  const failed = (error: unknown) => new LitebackupError(ErrorCode.invalidArgument, `creating ${path} failed: ${String(error)}`, 0, { cause: error });
  try {
    await mkdir(dirname(path), { recursive: true });
  } catch (error) {
    // A file where the directory should be fails here, even as EEXIST.
    throw failed(error);
  }
  try {
    await (await open(path, "wx")).close();
  } catch (error) {
    // Only the database file itself may exist already.
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw failed(error);
  }
}

/** Throws one error alone, or several together as an AggregateError, first one first. */
function throwFailures(failures: unknown[], message: string): void {
  if (failures.length === 1) throw failures[0];
  if (failures.length > 1) throw new AggregateError(failures, message);
}

/**
 * Keeps databases open for calls, the way an app serving many tenants needs
 * them:
 *
 *   const pool = litebackup.databases.pool({ adapter: adapter() });
 *   await pool.use("tenants/42.sqlite", (db) => db.prepare("INSERT INTO notes (body) VALUES (?)").run("hello"));
 *
 * use() is for databases a backup rule on this machine covers: they stay
 * here, open between calls. lease() is for databases that move between
 * machines: the first call acquires the write lock, and the pool keeps it,
 * with the open handle, until idleRelease runs out, release(), or close().
 * Close the pool when done: it releases every lease.
 */
export class Pool<DB> {
  readonly #client: Litebackup;
  readonly #adapter: Adapter<DB>;
  readonly #machineLabel?: string;
  readonly #onReleaseError?: (error: LeaseCleanupError) => void;
  readonly #cleanupMillis: number;
  readonly #idleCloseMillis: number;
  readonly #idleReleaseMillis: number;
  readonly #entries = new Map<string, PoolEntry<DB>>();
  readonly #rules = new Map<string, BackupRule>();
  /** Idle releases and idle closes; close() waits for them. */
  readonly #tasks = new Set<Promise<void>>();
  readonly #idleTimer: NodeJS.Timeout;
  /** use(), lease(), and release() calls that joined and have not left. */
  #runningCalls = 0;
  /** Resolved once no call runs, for a close() that waits. */
  #callsFinished?: () => void;
  #closed = false;

  /** Use litebackup.databases.pool(), which creates one. */
  constructor(client: Litebackup, options: PoolOptions<DB>) {
    if (!client || !options?.adapter || typeof options.adapter.open !== "function" || typeof options.adapter.close !== "function") {
      throw invalidArgument("a pool needs an adapter with open and close");
    }
    this.#cleanupMillis = milliseconds(options.cleanupTimeout ?? defaultCleanupTimeout);
    if (this.#cleanupMillis <= 0) {
      throw invalidArgument("a pool's cleanup timeout must be positive");
    }
    this.#idleCloseMillis = optionalMillis(options.idleClose);
    this.#idleReleaseMillis = milliseconds(options.idleRelease ?? defaultIdleRelease);
    this.#client = client;
    this.#adapter = options.adapter;
    this.#machineLabel = options.machineLabel;
    this.#onReleaseError = options.onReleaseError;
    this.#idleTimer = setInterval(() => this.#closeIdleDatabases(), poolTiming.idleCheckMillis);
    this.#idleTimer.unref();
  }

  /**
   * Runs work with a database a backup rule on this machine covers. The pool
   * opens it on first use and keeps it open; an offloaded copy is brought
   * back first, never recreated empty. Calls on one database run
   * concurrently on the same handle. A name no rule on this machine covers,
   * or an acquired database, is refused with invalid_argument; use lease()
   * for it.
   */
  async use<T>(name: string, work: PoolWork<DB, T>, options: SignalOptions = {}): Promise<T> {
    if (typeof work !== "function") {
      throw invalidArgument("use needs a function to run");
    }
    const entry = this.#join(name);
    try {
      await entry.turn.start();
      let db: DB;
      try {
        if (entry.leased) {
          throw invalidArgument(`${name} is leased in this pool; use lease for it`);
        }
        if (!entry.opened) {
          await this.#openRuleDatabase(entry, options.signal);
        }
        db = entry.db as DB;
      } finally {
        entry.turn.end();
      }
      return await work(db, options.signal ?? new AbortController().signal);
    } finally {
      this.#leave(entry);
      if (entry.closeAfterUse && entry.opened && entry.calls === 0) {
        this.#track(this.#closeAfterUse(entry));
      }
    }
  }

  /**
   * Closes a rollback-journal rule database once no use() call runs and
   * drops its entry, so the rule may offload it; the next use() opens it
   * again. A failed close goes to onReleaseError and keeps the entry for
   * the idle check to retry.
   */
  async #closeAfterUse(entry: PoolEntry<DB>): Promise<void> {
    await entry.turn.start();
    try {
      if (entry.calls > 0 || !entry.opened || entry.leased) return;
      try {
        await this.#closeDatabase(entry.db as DB, entry.name);
      } catch (error) {
        this.#reportReleaseError(
          new LeaseCleanupError(
            "close",
            `closing ${entry.name} after its last use failed: ${errorText(error)}. Its rule may offload it while it is open; the pool tries again at its next idle check`,
            { details: { step: "close", name: entry.name, path: entry.path, acquired: false }, cause: error },
          ),
        );
        return;
      }
      entry.db = undefined;
      entry.opened = false;
      if (entry.calls === 0 && this.#entries.get(entry.name) === entry) {
        this.#entries.delete(entry.name);
      }
    } finally {
      entry.turn.end();
    }
  }

  /** Opens a rule's database, bringing an offloaded copy back first, and tries once more when the copy was offloaded between the two. */
  async #openRuleDatabase(entry: PoolEntry<DB>, signal: AbortSignal | undefined): Promise<void> {
    for (let attempt = 0; ; attempt++) {
      const status = await this.#client.databases.get(entry.name, { signal });
      if (status.role !== Roles.origin || status.acquired || status.failover || !status.backupRuleId) {
        throw invalidArgument(`${entry.name} is not a backup rule's database on this machine; use lease for a database that moves between machines`);
      }
      if (status.state === DatabaseStates.offloaded) {
        await this.#client.databases.acquire(entry.name, { signal });
      }
      let db: DB;
      try {
        db = await this.#adapter.open(status.path, signal);
      } catch (openError) {
        if (attempt === 0 && !existsSync(status.path)) continue;
        throw openError;
      }
      entry.db = db;
      entry.path = status.path;
      entry.opened = true;
      const rollbackJournal = status.journalMode === JournalModes.rollback;
      // The pool reads the rule when it decides something: the idle close, or a rollback-journal database's offload.
      const rule = this.#idleCloseMillis === 0 || rollbackJournal ? await this.#backupRule(status.backupRuleId, signal) : undefined;
      entry.closeAfterUse = rollbackJournal && rule?.offloadRollbackJournal === true;
      entry.idleCloseMillis =
        this.#idleCloseMillis > 0 ? this.#idleCloseMillis : (rule?.maximumLocalDatabases ?? 0) > 0 ? milliseconds(defaultOffloadIdleClose) : 0;
      return;
    }
  }

  /** The backup rule with this ID, from rules.list(), which the pool reads once per rule it does not know yet. */
  async #backupRule(ruleId: string, signal: AbortSignal | undefined): Promise<BackupRule | undefined> {
    if (!this.#rules.has(ruleId)) {
      for (const listed of await this.#client.rules.list({ signal })) {
        this.#rules.set(listed.id, listed);
      }
    }
    return this.#rules.get(ruleId);
  }

  /**
   * Runs work with a database that moves between machines, exclusively:
   * calls on one name run one at a time, and no other machine writes while
   * the pool keeps the lock. The first call acquires the write lock,
   * creating an empty file at path when nothing is there; later calls reuse
   * the lock and the open handle. With durability the call resolves only
   * once the server, or the bucket, holds what the work committed.
   *
   * Each later call first asks the daemon whether this machine still has
   * the database acquired: when it lost the write lock, or its copy is
   * conflicted, the call closes the handle, drops the lease, and rejects
   * with lock_lost without running the work; the next call acquires again.
   * A loss while work runs shows at the next call, or as a failed durability
   * sync.
   *
   * Failed work, or a signal aborted by the time it returns, keeps the lease
   * and rejects; the release then does not offload. A failed open releases
   * at once, without offload. The adapters never commit for the work:
   * commit what it should keep.
   */
  async lease<T>(name: string, options: LeaseOptions, work: PoolWork<DB, T>): Promise<T> {
    options ??= {};
    if (typeof work !== "function") {
      throw invalidArgument("lease needs a function to run");
    }
    if (options.durability !== undefined && options.durability !== Durabilities.server && options.durability !== Durabilities.cloud) {
      throw invalidArgument("a lease's durability must be server or cloud");
    }
    optionalMillis(options.lockTimeout);
    optionalMillis(options.acquireTimeout);
    if (options.signal?.aborted) {
      throw new LitebackupError(ErrorCode.cancelled, "the lease was cancelled before it started", 0, { cause: options.signal.reason });
    }
    const entry = this.#join(name);
    try {
      await entry.turn.start();
      try {
        return await this.#leaseCall(entry, options, work);
      } finally {
        entry.turn.end();
      }
    } finally {
      this.#leave(entry);
    }
  }

  /** One lease call, within the entry's turn. */
  async #leaseCall<T>(entry: PoolEntry<DB>, options: LeaseOptions, work: PoolWork<DB, T>): Promise<T> {
    if (entry.opened && !entry.leased) {
      throw invalidArgument(`${entry.name} is a backup rule's database in this pool; use use for it`);
    }
    if (entry.leased) {
      await this.#requireLeaseLock(entry, options.signal);
      entry.leaseOptions = { ...options };
      if (!entry.opened) {
        // A release failed after closing the handle: open it again for this call.
        entry.db = await this.#adapter.open(entry.path, options.signal);
        entry.opened = true;
      }
    } else {
      entry.leaseOptions = { ...options };
      await this.#acquireLease(entry, options);
    }
    const signal = options.signal ?? new AbortController().signal;
    let workFailed = true;
    let workError: unknown;
    let returned: T | undefined;
    try {
      returned = await work(entry.db as DB, signal);
      workFailed = false;
    } catch (error) {
      workError = error;
    }
    if (!workFailed && options.signal?.aborted) {
      workFailed = true;
      workError = new LitebackupError(ErrorCode.cancelled, `the lease of ${entry.name} was cancelled while its work ran`, 0, { cause: options.signal.reason });
    }
    // The call counts as failed until its work and its durability wait both finished.
    entry.lastCallFailed = true;
    if (workFailed) throw workError;
    if (options.durability) {
      await this.#waitDurable(entry.name, options.durability, options.signal);
    }
    entry.lastCallFailed = false;
    return returned as T;
  }

  /** Rejects with lock_lost, after closing the handle and dropping the lease, when this machine no longer has the database acquired or its copy is conflicted. */
  async #requireLeaseLock(entry: PoolEntry<DB>, signal: AbortSignal | undefined): Promise<void> {
    const status = await this.#client.databases.get(entry.name, { signal });
    if (status.acquired && status.state !== DatabaseStates.conflicted) return;
    await this.#dropLostLease(entry);
    throw new LitebackupError(
      ErrorCode.lockLost,
      `this machine lost the write lock of ${entry.name} (state ${status.state}); the next lease acquires it again`,
      0,
      { details: { name: entry.name, path: entry.path, state: status.state } },
    );
  }

  /** Acquires the write lock and opens the database; a database that opened nothing is released again at once. */
  async #acquireLease(entry: PoolEntry<DB>, options: LeaseOptions): Promise<void> {
    const requestedPath = options.path ? absolutePath(options.path) : "";
    if (requestedPath && !options.requireExisting) {
      await createEmptyFile(requestedPath);
    }
    const acquired = await this.#client.databases.acquire(entry.name, {
      path: options.path,
      maintenance: options.maintenance,
      machineLabel: this.#machineLabel,
      timeout: options.acquireTimeout,
      lockTimeout: options.lockTimeout,
      signal: options.signal,
    });
    if (!acquired.database.acquired) {
      throw invalidArgument(`${entry.name} is a backup rule's database on this machine; use use for it`);
    }
    const path = acquired.database.path || requestedPath;
    let db: DB;
    try {
      db = await this.#adapter.open(path, options.signal);
    } catch (openError) {
      // Nothing is open: release the lock again, without offload.
      entry.path = path;
      entry.leased = true;
      entry.lastCallFailed = true;
      let releaseError: unknown;
      try {
        await this.#releaseLease(entry);
      } catch (error) {
        releaseError = error;
      }
      if (releaseError === undefined) throw openError;
      throw new AggregateError([openError, releaseError], `opening ${entry.name} failed, and releasing it again failed too`);
    }
    entry.db = db;
    entry.path = path;
    entry.opened = true;
    entry.leased = true;
  }

  /** Syncs the database now and resolves once the server, or the bucket with cloud, holds the version the sync reached. */
  async #waitDurable(name: string, durability: Durability, signal: AbortSignal | undefined): Promise<void> {
    let status = await this.#client.databases.sync(name, { signal });
    if (durability !== Durabilities.cloud) return;
    const target = status.syncedServer?.version ?? 0;
    while ((status.syncedCloud?.version ?? 0) < target) {
      try {
        await delay(cloudDurabilityPollMillis, undefined, { signal });
      } catch {
        throw new LitebackupError(ErrorCode.cancelled, "the lease was cancelled while it waited for the bucket", 0, { cause: signal?.reason });
      }
      status = await this.#client.databases.get(name, { signal });
    }
  }

  /**
   * Ends the lease of a database: closes the handle and releases the write
   * lock once the final version reaches the lease's durability, the server
   * when unset, after a running call returns. Rejects with a
   * LeaseCleanupError when closing or releasing fails, and with
   * invalid_argument for a database the pool does not lease.
   */
  async release(name: string): Promise<void> {
    const entry = this.#entries.get(name);
    if (!entry) throw invalidArgument(`${name} is not leased in this pool`);
    this.#countCall(entry);
    try {
      await entry.turn.start();
      try {
        if (!entry.leased) throw invalidArgument(`${name} is not leased in this pool`);
        await this.#releaseLease(entry);
      } finally {
        entry.turn.end();
      }
    } finally {
      this.#leave(entry);
    }
  }

  /** Closes the handle and releases the lock within one cleanup timeout, which runs even after a signal aborted. The caller is in the entry's turn. */
  async #releaseLease(entry: PoolEntry<DB>): Promise<void> {
    const deadline = Date.now() + this.#cleanupMillis;
    if (entry.opened) {
      try {
        await this.#closeDatabase(entry.db as DB, entry.name);
      } catch (error) {
        throw new LeaseCleanupError(
          "close",
          `closing ${entry.name} failed, so it was not released and stays acquired by this machine: ${errorText(error)}. Close every connection to ${entry.path}, then call release(${JSON.stringify(entry.name)}) again`,
          { details: { step: "close", name: entry.name, path: entry.path, acquired: true }, cause: error },
        );
      }
      entry.db = undefined;
      entry.opened = false;
    }
    const durability = entry.leaseOptions.durability ?? Durabilities.server;
    // At least 1 ms, since a release timeout of 0 means the daemon's default.
    const timeout = Math.max(Math.round(deadline - Date.now()), 1);
    try {
      await this.#client.databases.release(entry.name, {
        durability,
        offload: entry.leaseOptions.offload === true && !entry.lastCallFailed,
        timeout,
      });
    } catch (error) {
      throw new LeaseCleanupError(
        "release",
        `releasing ${entry.name} failed: ${errorText(error)}. Its file at ${entry.path} is intact and this machine may still hold its write lock; call release(${JSON.stringify(entry.name)}) again`,
        { details: { step: "release", name: entry.name, path: entry.path, durability }, cause: error },
      );
    }
    entry.leased = false;
    if (this.#entries.get(entry.name) === entry && entry.calls === 0) {
      this.#entries.delete(entry.name);
    }
  }

  /** Closes a database with the adapter, within the cleanup timeout. */
  async #closeDatabase(db: DB, name: string): Promise<void> {
    const signal = timeable(this.#cleanupMillis) ? AbortSignal.timeout(this.#cleanupMillis) : undefined;
    await withinTimeout(this.#adapter.close(db, signal), this.#cleanupMillis, `closing ${name}`);
  }

  /** Closes the handle of a database whose lock was lost, ignoring a failed close, and drops the lease; the copy stays as it is. The caller is in the entry's turn. */
  async #dropLostLease(entry: PoolEntry<DB>): Promise<void> {
    if (entry.opened) {
      await this.#closeDatabase(entry.db as DB, entry.name).catch(() => undefined);
      entry.db = undefined;
      entry.opened = false;
    }
    entry.leased = false;
  }

  /**
   * Refuses new calls with closed, waits for every running use(), lease(),
   * and release() call, then releases every leased database and closes every
   * open one. Rejects with the one failure, or an AggregateError of all of
   * them; a LeaseCleanupError names a database that stays acquired.
   */
  async close(): Promise<void> {
    if (this.#closed) return;
    this.#closed = true;
    clearInterval(this.#idleTimer);
    if (this.#runningCalls > 0) {
      await new Promise<void>((resolve) => {
        this.#callsFinished = resolve;
      });
    }
    while (this.#tasks.size > 0) {
      await Promise.all([...this.#tasks]);
    }
    const failures: unknown[] = [];
    for (const entry of [...this.#entries.values()]) {
      await entry.turn.start();
      try {
        if (entry.leased) {
          await this.#releaseLease(entry);
        } else if (entry.opened) {
          await this.#closeDatabase(entry.db as DB, entry.name);
          entry.db = undefined;
          entry.opened = false;
        }
      } catch (error) {
        failures.push(error);
      } finally {
        entry.turn.end();
      }
    }
    throwFailures(failures, `closing the pool failed for ${failures.length} databases`);
  }

  /** Counts a call on the name's entry, creating it; refuses calls once the pool closes. */
  #join(name: string): PoolEntry<DB> {
    if (!name) throw invalidArgument("a pool call needs a database name");
    if (this.#closed) throw new LitebackupError(ErrorCode.closed, "the pool is closed", 0);
    let entry = this.#entries.get(name);
    if (!entry) {
      entry = { name, turn: new CallTurn(), path: "", opened: false, leased: false, leaseOptions: {}, lastCallFailed: false, idleCloseMillis: 0, closeAfterUse: false, calls: 0, lastUsed: 0 };
      this.#entries.set(name, entry);
    }
    this.#countCall(entry);
    return entry;
  }

  /** Counts a call on an entry and on the pool. */
  #countCall(entry: PoolEntry<DB>): void {
    entry.calls++;
    this.#runningCalls++;
  }

  /** Ends a call on an entry, dropping an entry that holds nothing, and lets a waiting close() go on after the last call. */
  #leave(entry: PoolEntry<DB>): void {
    entry.calls--;
    entry.lastUsed = Date.now();
    if (entry.calls === 0 && !entry.opened && !entry.leased && this.#entries.get(entry.name) === entry) {
      this.#entries.delete(entry.name);
    }
    this.#runningCalls--;
    if (this.#runningCalls === 0 && this.#callsFinished) {
      this.#callsFinished();
      this.#callsFinished = undefined;
    }
  }

  /** Keeps a background task until it ends, so close() can wait for it. Tasks report their own failures. */
  #track(task: Promise<void>): void {
    const tracked = task.finally(() => this.#tasks.delete(tracked));
    this.#tasks.add(tracked);
  }

  /** Closes rule databases past their idle close and releases leases past idleRelease. */
  #closeIdleDatabases(): void {
    const now = Date.now();
    for (const entry of [...this.#entries.values()]) {
      if (entry.calls > 0 || entry.lastUsed === 0 || !entry.turn.tryStart()) continue;
      this.#track(this.#closeIdleEntry(entry, now).finally(() => entry.turn.end()));
    }
  }

  /**
   * Closes or releases one entry when it is still idle. The caller started
   * the entry's turn. A failed close keeps the entry and its handle, so the
   * next check and close() try again.
   */
  async #closeIdleEntry(entry: PoolEntry<DB>, now: number): Promise<void> {
    if (entry.calls > 0) return;
    const idleMillis = now - entry.lastUsed;
    if (entry.leased && this.#idleReleaseMillis > 0 && idleMillis >= this.#idleReleaseMillis) {
      await this.#releaseLease(entry).catch((error: unknown) => this.#reportReleaseError(error));
    } else if (!entry.leased && entry.opened && (entry.closeAfterUse || (entry.idleCloseMillis > 0 && idleMillis >= entry.idleCloseMillis))) {
      try {
        await this.#closeDatabase(entry.db as DB, entry.name);
      } catch {
        return;
      }
      entry.db = undefined;
      entry.opened = false;
      // A call that joined meanwhile opens it again in this entry.
      if (entry.calls === 0 && this.#entries.get(entry.name) === entry) {
        this.#entries.delete(entry.name);
      }
    }
  }

  /** Hands a failed background release or close to onReleaseError on a later tick, outside the entry's turn. */
  #reportReleaseError(error: unknown): void {
    const report = this.#onReleaseError;
    if (report && error instanceof LeaseCleanupError) setImmediate(() => report(error));
  }
}
