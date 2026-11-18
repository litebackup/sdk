import type { Camelize } from "./case.js";
import type { components } from "./generated/client.js";

type Schemas = components["schemas"];

/** What this machine does with a database. */
export type Role = "origin" | "replica";
/** Where a database stands on this machine; waiting means another machine is the origin. */
export type State = "syncing" | "synced" | "waiting" | "offloaded" | "stopped" | "conflicted" | "error";
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
/** A backup or failover running on this machine, as backups.list() returns it. */
export type BackupInfo = Camelize<Schemas["BackupRule"]>;
/** A failover as the daemon reports it. */
export type FailoverInfo = Camelize<Schemas["Failover"]>;
export type PrefixReplica = Camelize<Schemas["PrefixReplica"]>;
export type ReplicaList = Camelize<Schemas["ReplicaList"]>;
export type LitebackupEvent = Camelize<Schemas["Event"]>;
export type EventList = Camelize<Schemas["EventList"]>;
export type ShutdownResult = Camelize<Schemas["ShutdownResult"]>;

/** The SDK version sent with every call. */
export const sdkVersion = "0.4.1";

/** An error the Litebackup daemon answered with, carrying its code. */
export class LitebackupError extends Error {
  /** Such as busy, exists, in_use, not_found, still_open, timeout, or sdk_unsupported. */
  readonly code: string;
  /** The HTTP status of the answer, or 0 for errors the SDK raised itself. */
  readonly status: number;

  /** Creates an error from a daemon {code, message} answer. */
  constructor(code: string, message: string, status: number) {
    super(`${code}: ${message}`);
    this.name = "LitebackupError";
    this.code = code;
    this.status = status;
  }
}
