import { mkdir, open } from "node:fs/promises";
import { dirname } from "node:path";

import type { Litebackup } from "./client.js";
import { milliseconds, type Duration } from "./duration.js";
import { absolutePath, type PathSource } from "./location.js";
import { LitebackupError, type AcquireResult, type Durability } from "./types.js";

/** Bounds closing and releasing a managed database unless cleanupTimeout says otherwise. */
export const defaultCleanupTimeout = "10m";

/**
 * Opens and closes the connection a managed scope hands to its work. close
 * must roll back a transaction the work left unfinished, never commit it,
 * and throw when a connection may still be open: the scope releases the
 * database only after close succeeds. The driver subpath exports provide
 * adapters, such as `adapter()` from `@litebackup/sdk/node-sqlite`.
 */
export interface ScopeAdapter<Connection> {
  open(path: string): Promise<Connection> | Connection;
  close(connection: Connection): Promise<void> | void;
}

/** Options for databases.managed(). */
export interface ManagedOptions<Connection> {
  /** Local file or file: URL; the path this machine already uses when unset. */
  to?: PathSource;
  /**
   * When `to` is given and nothing is there, create an empty file and its
   * directory first, so a database neither the server nor this machine has
   * becomes a new one; the server's head replaces an empty file. Defaults to true.
   */
  create?: boolean;
  /** How far the final sync must reach before the release resolves. Required. */
  durability: Durability;
  /** Remove the local file once the bucket holds the released version; requested only when the work succeeded. Defaults to false. */
  offload?: boolean;
  /** Bound on closing and releasing, which run even after the signal aborts; defaults to 10m. */
  cleanupTimeout?: Duration;
  /** Opens and closes the connection. Required. */
  adapter: ScopeAdapter<Connection>;
  /** What other machines see as the writer. */
  machineLabel?: string;
  /** Checkpoint on release and VACUUM past the freelist threshold. */
  maintenance?: boolean;
  /** On a lost write lock, keep the copy as conflicted (default) or block local writes. */
  onLost?: "conflict" | "block";
  /** Bound on downloading the head. */
  acquireTimeout?: Duration;
  /** Aborts the acquire and is handed to the work; cleanup still runs. */
  signal?: AbortSignal;
}

/** What a managed scope's work receives besides its connection. */
export interface ManagedScope {
  name: string;
  /** The database file on this machine. */
  path: string;
  /** The scope's signal, aborted when the caller's is; work should stop when it aborts. */
  signal: AbortSignal;
  /** What acquire answered. */
  acquired: AcquireResult;
}

/** Resolves a promise, or rejects once the timeout passes. */
async function withinTimeout<T>(operation: Promise<T> | T, timeoutMillis: number, description: string): Promise<T> {
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
  await mkdir(dirname(path), { recursive: true });
  try {
    await (await open(path, "wx")).close();
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") {
      throw new LitebackupError("invalid_argument", `creating ${path} failed: ${String(error)}`, 0, { cause: error });
    }
  }
}

/** Throws one error alone, or several together as an AggregateError, first one first. */
function throwScopeErrors(errors: unknown[], name: string): never {
  if (errors.length === 1) {
    throw errors[0];
  }
  throw new AggregateError(errors, `the managed database ${name} failed in ${errors.length} ways; the first error ended the work`);
}

/** Runs acquire → open → work → close → release. @internal */
export async function runManaged<Connection, Result>(
  client: Litebackup,
  name: string,
  options: ManagedOptions<Connection>,
  work: (connection: Connection, scope: ManagedScope) => Promise<Result> | Result,
): Promise<Result> {
  if (options?.durability !== "server" && options?.durability !== "cloud") {
    throw new LitebackupError("invalid_argument", "a managed database needs a durability of server or cloud", 0);
  }
  if (!options.adapter || typeof work !== "function") {
    throw new LitebackupError("invalid_argument", "a managed database needs an adapter and a function to run", 0);
  }
  const cleanupMillis = milliseconds(options.cleanupTimeout ?? defaultCleanupTimeout);
  if (cleanupMillis <= 0) {
    throw new LitebackupError("invalid_argument", "the cleanup timeout must be positive", 0);
  }
  if (options.signal?.aborted) {
    throw new LitebackupError("cancelled", "the managed database was cancelled before it was acquired", 0, { cause: options.signal.reason });
  }
  if (options.to && options.create !== false) {
    await createEmptyFile(absolutePath(options.to));
  }
  const acquired = await client.databases.acquire(name, {
    to: options.to,
    machineLabel: options.machineLabel,
    maintenance: options.maintenance,
    onLost: options.onLost,
    timeout: options.acquireTimeout,
    signal: options.signal,
  });
  const path = acquired.database.path || (options.to ? absolutePath(options.to) : "");

  const release = async (offload: boolean): Promise<LitebackupError | undefined> => {
    try {
      await client.databases.release(name, { wait: options.durability, offload, timeout: cleanupMillis });
      return undefined;
    } catch (error) {
      return new LitebackupError(
        "scope_cleanup_failed",
        `releasing ${name} failed: ${error instanceof Error ? error.message : String(error)}. Its file at ${path} is intact and this machine may still hold its write lock; call databases.release(${JSON.stringify(name)}) again`,
        0,
        { details: { step: "release", name, path, durability: options.durability }, cause: error },
      );
    }
  };

  let connection: Connection;
  try {
    connection = await options.adapter.open(path);
  } catch (error) {
    const releaseError = await release(false);
    throwScopeErrors(releaseError ? [error, releaseError] : [error], name);
  }

  const signal = options.signal ?? new AbortController().signal;
  let workFailed = false;
  let workError: unknown;
  let result: Result | undefined;
  try {
    result = await work(connection, { name, path, signal, acquired });
  } catch (error) {
    workFailed = true;
    workError = error;
  }
  const raised: unknown[] = workFailed ? [workError] : [];

  try {
    await withinTimeout(options.adapter.close(connection), cleanupMillis, `closing ${name}`);
  } catch (error) {
    raised.push(
      new LitebackupError(
        "scope_cleanup_failed",
        `closing ${name} failed, so it was not released and stays acquired by this machine: ${error instanceof Error ? error.message : String(error)}. Close every connection to ${path}, then call databases.release(${JSON.stringify(name)})`,
        0,
        { details: { step: "close", name, path, acquired: true }, cause: error },
      ),
    );
    throwScopeErrors(raised, name);
  }

  const releaseError = await release(!workFailed && options.offload === true);
  if (releaseError) raised.push(releaseError);
  if (raised.length > 0) throwScopeErrors(raised, name);
  return result as Result;
}
