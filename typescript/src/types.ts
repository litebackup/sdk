import type { Camelize } from "./case.js";
import type { components } from "./generated/client.js";
import { specErrorCodes } from "./generated/error_codes.js";

type Schemas = components["schemas"];

/** The roles a database has on this machine, by name; the spec's DatabaseStatus.role. */
export const Roles = { origin: "origin", replica: "replica" } as const satisfies Record<string, Schemas["DatabaseStatus"]["role"]>;
/** What this machine does with a database. */
export type Role = (typeof Roles)[keyof typeof Roles];
/** The states of a database on this machine, by name; the spec's DatabaseStatus.state. */
export const DatabaseStates = {
  pending: "pending",
  syncing: "syncing",
  retrying: "retrying",
  synced: "synced",
  waiting: "waiting",
  offloaded: "offloaded",
  stopped: "stopped",
  conflicted: "conflicted",
  error: "error",
} as const satisfies Record<string, Schemas["DatabaseStatus"]["state"]>;
/** Where a database stands on this machine; waiting means another machine is the origin. */
export type DatabaseState = (typeof DatabaseStates)[keyof typeof DatabaseStates];
/** The journal modes the daemon reports in DatabaseStatus.journalMode, by name; unknown before the first scan. */
export const JournalModes = { WAL: "wal", rollback: "rollback", unknown: "unknown" } as const;
/** The durabilities, by name. */
export const Durabilities = { server: "server", cloud: "cloud" } as const;
/** How far a version must reach: the server's acknowledgement, or the bucket too. */
export type Durability = (typeof Durabilities)[keyof typeof Durabilities];
/** How a replica keeps app writes out. */
export type WriteProtection = "lock" | "detect" | "none";

export type SyncedState = Camelize<Schemas["SyncedState"]>;
export type ArchivedVersion = Camelize<Schemas["ArchivedVersion"]>;
export type DatabaseStatus = Camelize<Schemas["DatabaseStatus"]>;
export type DatabaseStatusList = Camelize<Schemas["DatabaseStatusList"]>;
export type Status = Camelize<Schemas["Status"]>;
export type StopBackupResult = Camelize<Schemas["StopBackupResult"]>;
export type RestorableVersion = Camelize<Schemas["RestorableVersion"]>;
/** One page of a database's versions; pass next as after to continue. */
export type VersionList = Camelize<Schemas["VersionList"]>;
export type RestoreResult = Camelize<Schemas["RestoreResult"]>;
export type RevertResult = Camelize<Schemas["RevertResult"]>;
export type AcquireResult = Camelize<Schemas["AcquireResult"]>;
export type ReleaseResult = Camelize<Schemas["ReleaseResult"]>;
export type Writer = Camelize<Schemas["Writer"]>;
/** A backup or failover rule on this machine, as backups.start() and rules.list() return it. */
export type BackupRule = Camelize<Schemas["BackupRule"]>;
/** A failover as the daemon reports it. */
export type FailoverStatus = Camelize<Schemas["Failover"]>;
export type PrefixReplica = Camelize<Schemas["PrefixReplica"]>;
export type ReplicaList = Camelize<Schemas["ReplicaList"]>;
export type Event = Camelize<Schemas["Event"]>;
export type EventList = Camelize<Schemas["EventList"]>;
/** A database a shutdown could not drain, which keeps its write lock, with the code and message of what stopped it. */
export type DrainFailure = Camelize<Schemas["DrainFailure"]>;
export type ShutdownResult = Camelize<Schemas["ShutdownResult"]>;

/** The SDK version sent with every call. */
export const sdkVersion = "1.6.0";

/** The oldest daemon this SDK talks to; connecting to an older one fails with daemon_unsupported. */
export const minimumDaemonVersion = "1.6.0";

/** Codes the SDK raises itself, with status 0, and the daemon codes worth matching. Other daemon codes are kept verbatim. */
export const ErrorCode = {
  /** An argument failed local validation; nothing was sent. */
  invalidArgument: specErrorCodes.invalidArgument,
  /** No litebackup binary was found to start the daemon or a foreground backup. */
  binaryNotFound: "binary_not_found",
  /** `litebackup launch` ran and failed. */
  launchFailed: specErrorCodes.launchFailed,
  /** The socket refused the connection or closed it mid-call. */
  connectionFailed: "connection_failed",
  /** The daemon is older than minimumDaemonVersion. */
  daemonUnsupported: "daemon_unsupported",
  /** The caller aborted the wait; the daemon may still finish the operation. */
  cancelled: specErrorCodes.cancelled,
  /** The launch refused apiKey, a credential other than the one the session's client took its write locks with: shutdown() with the current credential releases them first. */
  drainRequired: specErrorCodes.drainRequired,
  /** The client was closed. */
  closed: "closed",
  /** The daemon answered 200 with a body the SDK cannot decode. */
  invalidResponse: "invalid_response",
  /** A pool could not close or release a leased database; the error is a LeaseCleanupError. */
  leaseCleanupFailed: "lease_cleanup_failed",
  /** The database's write lock was lost while a lease call ran. */
  lockLost: specErrorCodes.lockLost,
  /** The file a restore or an acquire would write, or a journal file beside it, already exists. */
  fileExists: specErrorCodes.fileExists,
  /** A backup rule on this machine keeps the database and its write lock, so it cannot be released; stop its backup instead. */
  ruleDatabase: specErrorCodes.ruleDatabase,
  /** The path already has a backup rule with other settings; details.rule is the saved rule. */
  ruleExists: specErrorCodes.ruleExists,
  /** Another machine holds the database's write lock. */
  lockUnavailable: specErrorCodes.lockUnavailable,
  notFound: specErrorCodes.notFound,
  /** A process still has the database open, so it cannot be released. */
  stillOpen: specErrorCodes.stillOpen,
  /** The operation's timeout ran out. */
  timeout: specErrorCodes.timeout,
  /** This SDK is older than the daemon accepts. */
  sdkUnsupported: specErrorCodes.sdkUnsupported,
  /** Another job or a continuous backup holds a foreground backup's writer lock. */
  busy: specErrorCodes.busy,
  /** An events cursor is older than the oldest event the daemon keeps; a watch ends with it, and the caller picks a new cursor. */
  cursorExpired: specErrorCodes.cursorExpired,
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
  /** The daemon's code verbatim, such as busy, file_exists, lock_unavailable, not_found, still_open, or timeout, or one of ErrorCode. */
  readonly code: string;
  /** The HTTP status of the answer, or 0 for errors the SDK raised itself. */
  readonly status: number;
  /**
   * The answer's fields besides code and message, under their wire names,
   * such as `rule` for rule_exists from backups.start(). For lease_cleanup_failed:
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
