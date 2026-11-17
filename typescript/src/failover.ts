import { setTimeout as delay } from "node:timers/promises";

import { absolutePath, type Litebackup } from "./client.js";
import { milliseconds, type Duration } from "./duration.js";
import type { components } from "./generated/client.js";
import { LitebackupError, type FailoverInfo, type LitebackupEvent, type Role, type Writer } from "./types.js";

/** Options for failover. */
export interface FailoverOptions {
  /** The database name; defaults to the normalized file name. */
  name?: string;
  /** Bound on the first download of a replica. */
  timeout?: Duration;
  /** Join token for the shared client; defaults to LITEBACKUP_TOKEN. Used only by the package-level failover(). */
  token?: string;
  /** Session for the shared client. Used only by the package-level failover(). */
  session?: string;
}

/** How long one events long-poll waits. */
const eventWaitMillis = 30_000;
/** How many events one page reads. */
const eventPage = 1000;

/** The events a Failover reports, with their listener arguments. */
export interface FailoverEvents {
  /** This machine became the origin. */
  origin: [];
  /** This machine became a replica. */
  replica: [];
  /** Writes the server never got were saved to this conflict copy, as when this machine lost the origin role; `litebackup conflict list` shows it. */
  conflictSaved: [path: string];
  /** Reading the daemon's events failed; watching retries. Only reported when a listener is registered. */
  error: [error: Error];
}

/**
 * A database run on several identical machines. Each replica asks for the
 * write lock about once a second, so one takes over once the origin's lease
 * runs out. `role` follows the daemon's role_changed events, and listeners
 * hear about every change. While a Failover runs, its long-poll keeps the
 * Node process alive; stop() or the client's close() ends it.
 */
export class Failover {
  readonly name: string;
  /** The database file on this machine. */
  readonly path: string;
  /** This machine's role now. */
  role: Role;
  /** What the daemon answered when the failover started. */
  readonly info: FailoverInfo;
  private readonly client: Litebackup;
  private readonly listeners = new Map<keyof FailoverEvents, Set<Function>>();
  private readonly controller = new AbortController();
  private stopped = false;

  /** Use failover(); it starts the failover. */
  private constructor(client: Litebackup, info: FailoverInfo) {
    this.client = client;
    this.info = info;
    this.name = info.name;
    this.path = info.path;
    this.role = info.role;
  }

  /** @internal */
  static async start(client: Litebackup, path: string, options: FailoverOptions): Promise<Failover> {
    if (!path) {
      throw new LitebackupError("invalid_argument", "failover needs a path", 0);
    }
    const body: components["schemas"]["FailoverRequest"] = { path: absolutePath(path) };
    if (options.name) {
      body.name = options.name;
    }
    if (options.timeout !== undefined) {
      body.timeout_millis = milliseconds(options.timeout);
    }
    if (client.closed) {
      throw new LitebackupError("closed", "the client is closed", 0);
    }
    // Events after the log's end as of now are this failover's, including
    // those the start request itself records.
    const end = await client.eventPage({ after: -1 });
    const info = (await client.call("POST", "/v1/failovers/start", body)) as FailoverInfo;
    const failover = new Failover(client, info);
    if (client.closed) {
      failover.end();
      throw new LitebackupError("closed", "the client is closed", 0);
    }
    client.failovers.add(failover);
    void failover.follow(end.next);
    return failover;
  }

  /** Whether this machine writes the database now. */
  get isOrigin(): boolean {
    return this.role === "origin";
  }

  /** Listens for role changes, saved conflict copies, and watch errors. */
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
    return this.client.writer(this.name);
  }

  /** Stops the failover and releases its write lock. The file stays. */
  async stop(): Promise<void> {
    try {
      await this.client.backups.stop({ name: this.name });
    } finally {
      this.end();
      this.client.failovers.delete(this);
    }
  }

  /** Stops following events; listeners hear nothing more. @internal */
  end(): void {
    this.stopped = true;
    this.controller.abort();
  }

  /** @internal */
  changeRole(role: Role | undefined): void {
    if (!role || role === this.role || this.stopped) {
      return;
    }
    this.role = role;
    this.notify(role);
  }

  /** @internal */
  handle(event: LitebackupEvent): void {
    if (event.type === "role_changed") {
      this.changeRole((event.role ?? event.message) as Role);
    } else if (event.type === "conflict_saved" && !this.stopped) {
      this.notify("conflictSaved", event.path || event.message);
    }
  }

  /** Long-polls the daemon's events after cursor until stopped, handing this failover's events to it. */
  private async follow(cursor: number): Promise<void> {
    const signal = this.controller.signal;
    let retry = 250;
    while (!signal.aborted) {
      try {
        const page = await this.client.eventPage({ after: cursor, waitMillis: eventWaitMillis, limit: eventPage, signal });
        retry = 250;
        for (const event of page.events) {
          cursor = Math.max(cursor, event.id);
          if (event.name === this.name && !signal.aborted) {
            this.handle(event);
          }
        }
        cursor = Math.max(cursor, page.next);
      } catch (error) {
        if (signal.aborted) {
          return;
        }
        if ((this.listeners.get("error")?.size ?? 0) > 0) {
          this.notify("error", error instanceof Error ? error : new Error(String(error)));
        }
        if (error instanceof LitebackupError && error.code === "sdk_unsupported") {
          return;
        }
        try {
          await delay(retry, undefined, { signal });
        } catch {
          return;
        }
        retry = Math.min(retry * 2, 10_000);
      }
    }
  }

  /** Calls every listener of an event, in the order they were added. */
  private notify<Name extends keyof FailoverEvents>(event: Name, ...args: FailoverEvents[Name]): void {
    for (const listener of [...(this.listeners.get(event) ?? [])]) {
      (listener as (...args: FailoverEvents[Name]) => void)(...args);
    }
  }
}
