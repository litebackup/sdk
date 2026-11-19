import type { Camelize } from "./case.js";
import type { components } from "./generated/client.js";

type Schemas = components["schemas"];

/** What this machine does with a database. */
export type Role = "origin" | "replica";
/** Where a database stands on this machine; waiting means another machine is the origin. */
export type State = "syncing" | "retrying" | "synced" | "waiting" | "offloaded" | "stopped" | "conflicted" | "error";
/** How far a version must reach: the server's acknowledgement, or the bucket too. */
export type Durability = "server" | "cloud";
/** How a replica keeps app writes out. */
export type WriteProtection = "lock" | "detect" | "none";

export type SyncedState = Camelize<Schemas["SyncedState"]>;
export type DatabaseStatus = Camelize<Schemas["DatabaseStatus"]>;
export type DatabasePage = Camelize<Schemas["DatabaseStatusList"]>;
export type Status = Camelize<Schemas["Status"]>;
export type Version = Camelize<Schemas["RestorableVersion"]>;
export type RestoreResult = Camelize<Schemas["RestoreResult"]>;
export type Conflict = Camelize<Schemas["Conflict"]>;
export type AcquireResult = Camelize<Schemas["AcquireResult"]>;
export type ReleaseResult = Camelize<Schemas["ReleaseResult"]>;
export type Writer = Camelize<Schemas["Writer"]>;
/** A backup or failover rule on this machine, as backups.start() and rules.list() return it. */
export type BackupInfo = Camelize<Schemas["BackupRule"]>;
/** A failover as the daemon reports it. */
export type FailoverInfo = Camelize<Schemas["Failover"]>;
export type PrefixReplica = Camelize<Schemas["PrefixReplica"]>;
export type ReplicaList = Camelize<Schemas["ReplicaList"]>;
export type LitebackupEvent = Camelize<Schemas["Event"]>;
export type EventList = Camelize<Schemas["EventList"]>;
export type ShutdownResult = Camelize<Schemas["ShutdownResult"]>;

/** The SDK version sent with every call. */
export const sdkVersion = "1.0.0";

/** The oldest daemon this SDK talks to; connecting to an older one fails with incompatible_version. */
export const minimumDaemonVersion = "1.0.0";

/** Codes the SDK raises itself, with status 0, and the daemon codes worth matching. Other daemon codes are kept verbatim. */
export const ErrorCode = {
  /** An argument failed local validation; nothing was sent. */
  invalidArgument: "invalid_argument",
  /** No litebackup binary was found to start the daemon. */
  binaryNotFound: "binary_not_found",
  /** `litebackup launch` ran and failed. */
  launchFailed: "launch_failed",
  /** The socket refused the connection or closed it mid-call. */
  connectionFailed: "connection_failed",
  /** The daemon is older than minimumDaemonVersion. */
  incompatibleVersion: "incompatible_version",
  /** The caller aborted the wait; the daemon may still finish the operation. */
  cancelled: "cancelled",
  /** The client was closed. */
  closed: "closed",
  /** The daemon answered 200 with a body the SDK cannot decode. */
  invalidResponse: "invalid_response",
  /** A managed scope could not close or release its database. */
  scopeCleanupFailed: "scope_cleanup_failed",
  /** A restore target exists, or the path already has a backup rule with other settings. */
  exists: "exists",
  /** Another machine writes the database. */
  inUse: "in_use",
  notFound: "not_found",
  /** A process still has the database open, so it cannot be released. */
  stillOpen: "still_open",
  /** The operation's timeout ran out. */
  timeout: "timeout",
  /** This SDK is older than the daemon accepts. */
  sdkUnsupported: "sdk_unsupported",
  /** Another job or a continuous backup holds a foreground backup's writer lock. */
  busy: "busy",
} as const;

/** Options for a LitebackupError beyond its code, message, and status. */
export interface LitebackupErrorOptions {
  /** The answer's fields besides code and message, under their wire names. */
  details?: Record<string, unknown>;
  /** The underlying error. */
  cause?: unknown;
}

/** Every failure the SDK throws: the daemon's answer, or an error the SDK raised itself with status 0. */
export class LitebackupError extends Error {
  /** The daemon's code verbatim, such as busy, exists, in_use, not_found, still_open, or timeout, or one of ErrorCode. */
  readonly code: string;
  /** The HTTP status of the answer, or 0 for errors the SDK raised itself. */
  readonly status: number;
  /**
   * The answer's fields besides code and message, under their wire names,
   * such as `rule` for exists from backups.start(). For scope_cleanup_failed:
   * step, name, path, and acquired or durability.
   */
  readonly details: Record<string, unknown>;

  constructor(code: string, message: string, status: number, options: LitebackupErrorOptions = {}) {
    super(`${code}: ${message}`, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = "LitebackupError";
    this.code = code;
    this.status = status;
    this.details = options.details ?? {};
  }
}

/** Decodes a non-200 answer: a JSON object's code, message, and other fields, or http_<status> with the body's text. */
export function responseError(status: number, body: string): LitebackupError {
  const fallback = new LitebackupError(`http_${status}`, body.trim(), status);
  let decoded: unknown;
  try {
    decoded = JSON.parse(body);
  } catch {
    return fallback;
  }
  if (decoded === null || typeof decoded !== "object" || Array.isArray(decoded)) {
    return fallback;
  }
  const { code, message, ...details } = decoded as Record<string, unknown>;
  if (typeof code !== "string" || !code) {
    return typeof message === "string" ? new LitebackupError(`http_${status}`, message, status) : fallback;
  }
  return new LitebackupError(code, typeof message === "string" ? message : "", status, { details });
}

/** Orders dotted numeric versions, ignoring any -suffix; unparsable parts count as zero. */
export function compareVersions(left: string, right: string): number {
  const parts = (version: string) => version.split("-", 1)[0]!.split(".").map((part) => Number.parseInt(part, 10) || 0);
  const leftParts = parts(left);
  const rightParts = parts(right);
  for (let index = 0; index < Math.max(leftParts.length, rightParts.length); index++) {
    const difference = (leftParts[index] ?? 0) - (rightParts[index] ?? 0);
    if (difference !== 0) {
      return difference;
    }
  }
  return 0;
}
