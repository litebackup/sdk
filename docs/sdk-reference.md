# Litebackup SDK reference for TypeScript and Go

Both SDKs use the local client's [socket API](../client-openapi.yaml). Wire
fields use snake_case; TypeScript results use camelCase; Go exposes named
struct fields with JSON tags. Both keep timestamp fields in response objects
as their declared types: TypeScript strings and Go `time.Time` / `*time.Time`.

## Connect

```ts
import { Litebackup } from "@litebackup/sdk";
const client = await Litebackup.connect({
  token: process.env.LITEBACKUP_TOKEN,
  session: "default",
});
```

```go
client, err := litebackup.Connect(ctx, litebackup.Options{
    Token: os.Getenv("LITEBACKUP_TOKEN"), Session: "default",
})
```

`token` is a join token plus server address (`JOIN_TOKEN@HOST`), not a server
API key. It is only used when the local client first joins; see
[client credentials](litebackup-client.md#join-and-register).

Omit token and session to use the environment. TypeScript also supports
`environment`, `launch: false`, and `socketPath`. Go supports `Options.SocketPath` for an existing daemon. A socket override
connects without launching.
Go callers pass a context to operations. TypeScript durations are milliseconds
or strings such as `"5s"` and `"2m"`; Go uses `time.Duration`.

Package-level helpers connect lazily and share a client per token and session.
TypeScript exports `sharedClient()`; Go exposes `Default(ctx)`.

## Operations

Methods below are on a connected client unless identified as package helpers.
`backup`, `replicate`, `replicatePrefix`, `restore`, `versions`,
`acquire`, `release`, `offload`, and `writer` also have package-level helpers
with their corresponding Go names.

| TypeScript | Go | Returns when / result |
| --- | --- | --- |
| `backup(pathOrHandle, options?)` | `Backup(ctx, path, options)` | `Backup` handle immediately in TS; `*BackupRun, error` after registration in Go |
| `run.ready` | `Backup` return | Backup registered; optional start wait can change the boundary |
| `run.wait("server" \| "cloud", { signal }?)` | `run.Wait(ctx, durability)` | Sync before returning; cloud also waits for bucket |
| `run.stop()` | `run.Stop(ctx)` | Stop this backup; no final sync implied |
| `replicate(name, options?)` | `Replicate(ctx, name, options)` | First local version available |
| `replicatePrefix(prefix, { to, writeProtection? })` | `ReplicatePrefix(ctx, prefix, options)` | Namespace registered; files download in background |
| `restore(name, { to, version?, time? })` | `Restore(ctx, name, options)` | New file materialized and checked |
| `versions(name, { after?, limit? }?)` | `Versions(ctx, name, options)` | Retained restorable versions, oldest first; SDK follows pagination |
| `acquire(name, options?)` | `Acquire(ctx, name, options)` | Ownership obtained, local file current |
| `release(name, options?)` | `Release(ctx, name, options)` | Final sync and ownership released |
| `offload(name)` | `Offload(ctx, name)` | Remove released local file after bucket durability |
| `writer(name)` | `Writer(ctx, name)` | Writer machine and lease |
| `status()` | `Status(ctx)` | Session and database status |
| `database(name, { signal }?)` | `Database(ctx, name)` | One local database's status |
| `databases()` | `Databases(ctx)` | Local database list |
| `sync(name, { signal }?)` | `Sync(ctx, name)` | Explicit server acknowledgement |
| `backups.list({ signal }?)` | `Backups(ctx)` | Registered backups |
| `backups.stop({ id } \| { name } \| { path })` | `StopBackup(ctx, StopBackupOptions)` | Stop matching backup |
| `replicas.list()` | `Replicas(ctx)` | Prefix rules and replicas |
| `replicas.stop(name)` | `StopReplica(ctx, name)` | Stop one replica; file stays |
| `replicas.stopPrefix(prefix, to)` | `StopReplicaPrefix(ctx, prefix, to)` | Stop prefix and replicas; files stay |
| `conflicts.list()` / `conflicts.delete(id)` | `Conflicts(ctx)` / `DeleteConflict(ctx, id)` | Inspect / remove divergent saved copies |
| `events({ after?, wait?, signal? }?)` | `Events(ctx, EventsOptions)` | TS async iterator; Go one page with `Next` cursor |
| `eventPage({ after?, waitMillis?, limit?, signal? }?)` | `Events(ctx, EventsOptions)` | One event page |
| `close()` | `Close()` | Close SDK connection and watchers; daemon stays |
| `shutdown({ wait?, timeout? }?)` | `Shutdown(ctx, ShutdownOptions)` | Drain and stop the whole session |

## Release compatibility

Namespace replication and cancellation of in-flight wait requests are not in
the published 0.3.30 SDK. Build this source or use a release containing these
changes. Older SDKs support single-file replicas and only cancel cloud waits
between status requests.

## Backup options

| TypeScript | Go | Meaning |
| --- | --- | --- |
| `name` | `Name` | Exact remote database name |
| `prefix` | `Prefix` | Prefix before normalized basename |
| `wait: "none" \| "server" \| "cloud"` | `Wait: DurabilityNone / DurabilityServer / DurabilityCloud` | Optional initial durability wait; leave unset for app startup |
| `timeout` | `Timeout` | Bounds the initial wait |
| `maximumLocalDatabases` | `MaximumLocalDatabases` | Offload idle files above a positive count once bucket-safe |
| `token`, `session` | Select through `Connect` / `Default` | Shared-client choice for package-level TS helper |

TypeScript accepts a path or a duck-typed `better-sqlite3` handle. The handle
supplies `name`, `memory`, and `open`. In-memory handles are rejected.
Globs apply to the final path segment. See [backup semantics](sqlite-backup.md).

## Acquire and release options

| Acquire TS | Acquire Go | Meaning |
| --- | --- | --- |
| `to` | `To` | Local path; omitted to reuse the known path |
| `maintenance` | `Maintenance` | Checkpoint and optional VACUUM on release |
| `offload: "released"` | `OffloadReleased: true` | Offload after release when bucket-safe |
| `machineLabel` | `MachineLabel` | Label visible to other machines |
| `onLost: "block"` | `BlockOnLost: true` | Block local writes when ownership is lost, rather than default conflict behavior |
| `timeout` | `Timeout` | Bounds downloading the head |

Release options: `wait` (`Wait`) chooses server or cloud durability, `offload`
(`Offload`) requests local removal after bucket safety, and `timeout`
(`Timeout`) bounds waiting for closure and durability. Close all SQLite
connections before release. See [agents](sqlite-agent-state.md).

Replica options: `to`, `writeProtection: "lock" | "detect" | "none"`, and
`timeout` for the first single-file download. Prefix replicas require a
`to` directory and support `writeProtection`; registration does not wait for
the namespace's downloads. See [replicas](sqlite-replication.md).

## Status and errors

`DatabaseStatus` has `role`, `state`, `syncedServer`, `syncedCloud`,
`localChanged`, `acquired`, and diagnostics such as `lastError`.
Roles are `origin` and `replica`. States are `syncing`, `synced`, `waiting`,
`offloaded`, `stopped`, `conflicted`, and `error`. The counters tell you which
version reached the server or bucket. An active origin can stay in `syncing`
while it continues backing up changes.

```ts
import { LitebackupError } from "@litebackup/sdk";

try {
  await client.acquire("agents/42.sqlite", { to: "/var/lib/app/42.sqlite" });
} catch (error) {
  if (error instanceof LitebackupError && error.code === "in_use") {
    console.error("Agent already running; retry later");
  } else {
    throw error;
  }
}
```

Go uses `*litebackup.Error` with `Code`, `Message`, and `Status`, and
`litebackup.IsCode(err, "in_use")`. Context cancellation, transport failures,
and Node abort errors are not necessarily `LitebackupError` values.
See [troubleshooting](sqlite-backup-errors.md) for common codes.

## Release verification

`findRelease(metadata)` / `litebackup.FindRelease` verifies the package
metadata served at `https://litebackup.com/pkg/metadata` with Litebackup's
Ed25519 release keys. Invalid signatures raise `SignatureError` in TypeScript
or return an error in Go. Verification happens before a release is selected.
