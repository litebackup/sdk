import { performance } from "node:perf_hooks";
import { setTimeout as delay } from "node:timers/promises";

import type { Litebackup } from "./client.js";
import { absolutePath, type PathSource } from "./location.js";
import { milliseconds, type Duration } from "./duration.js";
import type { components } from "./generated/client.js";
import { ErrorCode, LitebackupError, type DatabaseStatus, type EventList, type FailoverStatus, type Event, type Role, type Writer } from "./types.js";

/** Options for failover. */
export interface FailoverOptions {
  /** The database name; defaults to the file name. */
  name?: string;
  /** Bound on the first download of a replica. */
  timeout?: Duration;
  /**
   * Replace a file at the path that differs from the server head with the
   * head, discarding its writes the head lacks; without it such a file is
   * refused with conflict and left untouched.
   */
  discardLocal?: boolean;
  signal?: AbortSignal;
}

/**
 * A failover's role on this machine: origin, replica, or unknown while the
 * daemon does not answer, from a draining event until the next role change
 * or a role read past the drain,
 * and once the failover stopped. The SDK sets unknown; the daemon never
 * reports it as a role.
 */
export type FailoverRole = Role | "unknown";
/** The role a failover reports while the daemon does not answer or drains. */
const roleUnknown: FailoverRole = "unknown";

/** Whether an event type sets a failover's role: role_changed, and draining and refused, which make it unknown. */
function changesRole(type: Event["type"]): boolean {
  return type === "role_changed" || type === "draining" || type === "denied";
}

/**
 * How long a failover's long-poll waits, and the longest it keeps reporting a
 * role the daemon has not confirmed: 15 s, the default client heartbeat
 * interval and a quarter of the default 60 s session lease another machine
 * waits out before it takes over. The wait is shorter than the limit, so a
 * daemon that answers confirms the role in time. Tests shorten both.
 * @internal
 */
export const failoverTiming = { eventWaitMillis: 10_000, roleConfirmationLimitMillis: 15_000 };
/** How many events one page reads. */
const eventPage = 1000;
/** The backoff between failed reads, doubling from first to maximum. */
const firstRetryMillis = 250;
const maximumRetryMillis = 10_000;

/** The events a Failover reports, with their listener arguments. */
export interface FailoverEvents {
  /** This machine became the origin. */
  origin: [];
  /** This machine became a replica. */
  replica: [];
  /** The daemon stopped answering, so this machine's role is unknown; once it answers again, the role is read and reported. */
  unknown: [];
  /** The daemon refused this machine's file and the failover stays conflicted there, leaving the file as it is: a file that differs from the server head when this machine starts as the origin, or one that holds writes the new origin never saw when this machine lost the origin role. The database's state is conflicted, and the role is unknown, except for a refusal older than the last role read after the daemon answered again, which leaves the role that read gave. The argument is the daemon's message. */
  denied: [message: string];
  /** Reading the daemon's events failed; watching retries. An expired cursor (cursor_expired) is reported too, and following resumes at the log's end after re-reading the role. Only reported when a listener is registered. */
  error: [error: Error];
}

/** What a failover's follower carries from one read to the next. */
interface FollowProgress {
  /** The newest event ID handed to the failover. */
  cursor: number;
  /** The backoff after the next failed read. */
  retryMillis: number;
  /** When the daemon last confirmed the role, on performance.now()'s monotonic clock: the start, a long-poll, or a role read answered. */
  confirmed: number;
  /** Set when the role is read before the next long-poll: after an expired cursor, and after the role became unknown. */
  roleReadPending: boolean;
  /** Set when the log's end is read before that role read. */
  logEndPending: boolean;
  /** The log's end read before the last role read: role_changed events up to it are older than that read and are skipped. */
  roleReadLogEnd: number;
}

/**
 * A database run on several identical machines. Each replica asks for the
 * write lock about once a second, so one takes over once the origin's lease
 * runs out. `role` follows the daemon's role_changed events. Listeners hear
 * the role the start answered once, on the next turn of the event loop, so
 * register them right after start resolves; then they hear every change,
 * including `unknown` when the daemon stops answering. While a Failover runs,
 * its long-poll keeps the Node process alive; stop() or the client's close()
 * ends it.
 */
export class Failover {
  readonly name: string;
  /** The database file on this machine. */
  readonly path: string;
  /** This machine's role now; unknown while the daemon does not answer and once the failover stopped. */
  role: FailoverRole;
  /** What the daemon answered when the failover started. */
  readonly status: FailoverStatus;
  private readonly client: Litebackup;
  private readonly listeners = new Map<keyof FailoverEvents, Set<Function>>();
  private readonly controller = new AbortController();
  private readonly eventWaitMillis = failoverTiming.eventWaitMillis;
  private readonly roleConfirmationLimitMillis = failoverTiming.roleConfirmationLimitMillis;
  private stopped = false;

  /** Use failover(); it starts the failover. */
  private constructor(client: Litebackup, status: FailoverStatus) {
    this.client = client;
    this.status = status;
    this.name = status.name;
    this.path = status.path;
    this.role = status.role;
  }

  /** @internal */
  static async start(client: Litebackup, path: PathSource, options: FailoverOptions): Promise<Failover> {
    if (!path) {
      throw new LitebackupError("invalid_argument", "failover needs a path", 0);
    }
    const body: components["schemas"]["FailoverRequest"] = { path: absolutePath(path) };
    if (options.name) {
      body.name = options.name;
    }
    const timeoutMillis = options.timeout === undefined ? 0 : milliseconds(options.timeout);
    if (timeoutMillis) {
      body.timeout_millis = timeoutMillis;
    }
    if (options.discardLocal) {
      body.discard_local = true;
    }
    if (client.closed) {
      throw new LitebackupError("closed", "the client is closed", 0);
    }
    // Events after the log's end as of now are this failover's, including
    // those the start request itself records.
    const end = await client.events.page({ after: -1, signal: options.signal });
    const status = (await client.call("POST", "/v1/failovers/start", body, { signal: options.signal, timeoutMillis })) as FailoverStatus;
    const failover = new Failover(client, status);
    if (client.closed) {
      failover.stopFollowing();
      throw new LitebackupError("closed", "the client is closed", 0);
    }
    client.watches.add(failover);
    void failover.follow(end.next);
    return failover;
  }

  /** Whether this machine writes the database now; false while the role is unknown. */
  get isOrigin(): boolean {
    return this.role === "origin";
  }

  /** Listens for role changes, refusals, and watch errors. */
  on<Name extends keyof FailoverEvents>(event: Name, listener: (...args: FailoverEvents[Name]) => void): this {
    let registered = this.listeners.get(event);
    if (!registered) {
      registered = new Set();
      this.listeners.set(event, registered);
    }
    registered.add(listener);
    return this;
  }

  /** Removes a listener. */
  off<Name extends keyof FailoverEvents>(event: Name, listener: (...args: FailoverEvents[Name]) => void): this {
    this.listeners.get(event)?.delete(listener);
    return this;
  }

  /** Which machine writes the database, when its lease runs out, and whether it ran out. */
  writer(): Promise<Writer> {
    return this.client.databases.writer(this.name);
  }

  /**
   * Stops the failover and releases its write lock. The file stays, and the
   * role is unknown from then on. Listeners hear nothing once stop is called.
   */
  async stop(): Promise<void> {
    this.stopFollowing();
    this.client.watches.delete(this);
    await this.client.backups.stop(this.name);
  }

  /** Stops following events; listeners hear nothing more, and nothing follows the role any more, so it is unknown. @internal */
  stopFollowing(): void {
    this.stopped = true;
    this.role = roleUnknown;
    this.controller.abort();
  }

  /** Saves a role and tells listeners when it differs; returns whether it changed. @internal */
  changeRole(role: FailoverRole | undefined): boolean {
    if (!role || role === this.role || this.stopped) {
      return false;
    }
    this.role = role;
    this.notify(role);
    return true;
  }

  /**
   * Applies one event about this failover's database. An event older than the
   * last role read changes no role, because that read holds it; a refusal
   * among them is still reported.
   * @internal
   */
  applyEvent(event: Event, olderThanRoleRead = false): void {
    if (olderThanRoleRead && event.type !== "denied") {
      return;
    }
    if (event.type === "role_changed") {
      this.changeRole((event.role ?? event.message) as Role);
    } else if (event.type === "draining") {
      // The daemon drains: it stops being the origin before it releases the
      // lock, so the app stops writing while no machine may take over yet.
      this.changeRole(roleUnknown);
    } else if (event.type === "denied" && !this.stopped) {
      // The daemon refused this machine's file and the failover stays
      // conflicted there: this machine neither writes nor follows the database.
      if (!olderThanRoleRead) this.changeRole(roleUnknown);
      if (!this.stopped) this.notify("denied", event.message);
    }
  }

  /**
   * Long-polls the daemon's events after cursor until stopped, handing this
   * failover's events to it. It first reports the role the start answered,
   * on the next turn of the event loop.
   *
   * An expired cursor does not end following: it moves to the log's end,
   * then the role is read again before the next long-poll, so a role change
   * in events the daemon pruned is still reported. The cursor is taken first
   * and kept while the role read is retried, so no event after it is lost.
   *
   * A read the daemon did not answer (connection_failed, timeout,
   * daemon_unsupported: any error with status 0) makes the role unknown at
   * once, and so does no answer within the confirmation limit of the last
   * confirmation, or failed reads the daemon answered for that long. Once
   * the daemon answers again, the follower reads the log's end, then the
   * role, then the log's end again and the events up to it: when one
   * changed the role, the role read may predate it, so it reads the role
   * again. It keeps its cursor: refusals meanwhile are still
   * reported, and role changes up to the end it looked at are skipped because
   * the role read holds them. A closed client makes the role unknown without
   * a listener hearing it.
   */
  private async follow(cursor: number): Promise<void> {
    const signal = this.controller.signal;
    try {
      await delay(0, undefined, { signal });
    } catch {
      return;
    }
    this.notify(this.role);
    const progress: FollowProgress = {
      cursor,
      retryMillis: firstRetryMillis,
      confirmed: performance.now(),
      roleReadPending: false,
      logEndPending: false,
      roleReadLogEnd: 0,
    };
    while (!signal.aborted) {
      try {
        await this.readDaemon(progress);
        progress.retryMillis = firstRetryMillis;
      } catch (error) {
        if (signal.aborted) {
          return;
        }
        const closed = error instanceof LitebackupError && error.code === ErrorCode.closed;
        const ending = closed || (error instanceof LitebackupError && error.code === ErrorCode.sdkUnsupported);
        const unanswered = !(error instanceof LitebackupError) || error.status === 0;
        if (closed) {
          // The client is closing and stops following next: no role listener hears it.
          this.role = roleUnknown;
        } else if (ending || unanswered) {
          this.loseRole(progress);
        }
        this.reportError(error);
        if (ending) {
          return;
        }
        let waitMillis = progress.retryMillis;
        if (this.role !== roleUnknown) {
          waitMillis = Math.max(0, Math.ceil(Math.min(waitMillis, progress.confirmed + this.roleConfirmationLimitMillis - performance.now())));
        }
        try {
          await delay(waitMillis, undefined, { signal });
        } catch {
          return;
        }
        progress.retryMillis = Math.min(progress.retryMillis * 2, maximumRetryMillis);
      }
    }
  }

  /** Makes the follower's next read: the log's end or the role when a role read is pending, otherwise one long-poll. */
  private async readDaemon(progress: FollowProgress): Promise<void> {
    this.loseUnconfirmedRole(progress);
    if (progress.roleReadPending) {
      if (progress.logEndPending) {
        progress.roleReadLogEnd = (await this.deadlineRead(progress, (signal) => this.client.events.page({ after: -1, signal }))).next;
        progress.logEndPending = false;
      }
      const database: DatabaseStatus = await this.deadlineRead(progress, (signal) => this.client.databases.get(this.name, { signal }));
      if (await this.roleSuperseded(progress)) {
        // The role read may predate a role change the log now holds: read the role again, then look past the new log end.
        return;
      }
      progress.confirmed = performance.now();
      progress.roleReadPending = false;
      if (!this.controller.signal.aborted) {
        // A drain says the role is unknown, as its draining event does, and
        // so does a refused file, as its denied event does.
        this.changeRole(database.draining || database.state === "conflicted" ? roleUnknown : database.role);
      }
      return;
    }
    let page: EventList;
    try {
      page = await this.deadlineRead(progress, (signal) =>
        this.client.events.page({ after: progress.cursor, wait: this.eventWaitMillis, limit: eventPage, signal }),
      );
    } catch (error) {
      if (this.controller.signal.aborted || !(error instanceof LitebackupError) || error.code !== ErrorCode.cursorExpired) {
        throw error;
      }
      this.reportError(error);
      const end = await this.deadlineRead(progress, (signal) => this.client.events.page({ after: -1, signal }));
      progress.cursor = end.next;
      progress.roleReadLogEnd = end.next;
      progress.roleReadPending = true;
      progress.logEndPending = false;
      return;
    }
    progress.confirmed = performance.now();
    let roleEvents = 0;
    for (const event of page.events) {
      progress.cursor = Math.max(progress.cursor, event.id);
      const olderThanRoleRead = changesRole(event.type) && event.id <= progress.roleReadLogEnd;
      if (event.name === this.name && !this.controller.signal.aborted) {
        this.applyEvent(event, olderThanRoleRead);
        if (changesRole(event.type) && !olderThanRoleRead) roleEvents++;
      }
    }
    progress.cursor = Math.max(progress.cursor, page.next);
    if (roleEvents === 0 && this.role === roleUnknown) {
      // A role unknown since a draining event is read again after every
      // long-poll that brought no role change: a drain that failed says
      // nothing more, and its status shows the role once it stopped.
      progress.roleReadPending = true;
      progress.logEndPending = true;
    }
  }

  /**
   * Reads the log's end as of now, then the events from roleReadLogEnd
   * through it, without waiting, and reports whether one changed this
   * database's role, so a role read that raced it is not trusted.
   * roleReadLogEnd moves to that end; events after it came after the role
   * read. When pruning passed roleReadLogEnd, it reports true with
   * logEndPending set, so the follower starts again from a fresh log end.
   */
  private async roleSuperseded(progress: FollowProgress): Promise<boolean> {
    const end = (await this.deadlineRead(progress, (signal) => this.client.events.page({ after: -1, signal }))).next;
    let superseded = false;
    while (progress.roleReadLogEnd < end) {
      let page: EventList;
      try {
        page = await this.deadlineRead(progress, (signal) =>
          this.client.events.page({ after: progress.roleReadLogEnd, limit: eventPage, signal }),
        );
      } catch (error) {
        if (error instanceof LitebackupError && error.code === ErrorCode.cursorExpired) {
          progress.logEndPending = true;
          return true;
        }
        throw error;
      }
      for (const event of page.events) {
        if (event.id > end) break;
        if (event.name === this.name && changesRole(event.type)) {
          superseded = true;
        }
      }
      const last = page.events.at(-1);
      if (!last) break;
      // Never past end: a role change after it came after the role read, and the long-poll applies it.
      progress.roleReadLogEnd = Math.max(progress.roleReadLogEnd, Math.min(last.id, end));
    }
    progress.roleReadLogEnd = Math.max(progress.roleReadLogEnd, end);
    return superseded;
  }

  /**
   * Runs one read with a deadline of the confirmation limit after the last
   * confirmation, or after now once the role is unknown; a read the deadline
   * ended is timeout.
   */
  private async deadlineRead<Answer>(progress: FollowProgress, read: (signal: AbortSignal) => Promise<Answer>): Promise<Answer> {
    const start = this.role === roleUnknown ? performance.now() : progress.confirmed;
    const deadline = AbortSignal.timeout(Math.max(0, Math.ceil(start + this.roleConfirmationLimitMillis - performance.now())));
    try {
      return await read(AbortSignal.any([this.controller.signal, deadline]));
    } catch (error) {
      if (deadline.aborted && !this.controller.signal.aborted) {
        const message = `the daemon did not answer within ${this.roleConfirmationLimitMillis} ms, so the failover's role is unknown`;
        throw new LitebackupError(ErrorCode.timeout, message, 0, { cause: error });
      }
      throw error;
    }
  }

  /** Makes the role unknown once the daemon has not confirmed it for the confirmation limit. */
  private loseUnconfirmedRole(progress: FollowProgress): void {
    if (this.role !== roleUnknown && performance.now() - progress.confirmed >= this.roleConfirmationLimitMillis) {
      this.loseRole(progress);
    }
  }

  /** Makes the role unknown and, when that changed it, has the follower read the log's end and the role once the daemon answers again. */
  private loseRole(progress: FollowProgress): void {
    if (this.changeRole(roleUnknown)) {
      progress.roleReadPending = true;
      progress.logEndPending = true;
    }
  }

  /** Hands a failed read to the error listeners, when there are any. */
  private reportError(error: unknown): void {
    if ((this.listeners.get("error")?.size ?? 0) > 0) {
      this.notify("error", error instanceof Error ? error : new Error(String(error)));
    }
  }

  /** Calls every listener of an event, in the order they were added. */
  private notify<Name extends keyof FailoverEvents>(event: Name, ...args: FailoverEvents[Name]): void {
    for (const listener of [...(this.listeners.get(event) ?? [])]) {
      // A listener may stop the failover; the rest then hear nothing.
      if (this.stopped) return;
      (listener as (...args: FailoverEvents[Name]) => void)(...args);
    }
  }
}
