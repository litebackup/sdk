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
client, err := litebackup.Connect(ctx, &litebackup.ConnectArgs{
    Token: os.Getenv("LITEBACKUP_TOKEN"), Session: "default",
})
```

`token` is a join token plus server address (`JOIN_TOKEN@HOST`), not a server
API key. It is only used when the local client first joins; see
[client credentials](litebackup-client.md#join-and-register).

Omit token and session to use the environment. TypeScript also supports
`environment`, `launch: false`, and `socketPath`. Go supports `ConnectArgs.SocketPath` for an existing daemon. A socket override
connects without launching.
Go callers pass a context and a pointer to the operation’s `Args` struct. Pass `nil` to use defaults. Restore and prefix replication require a destination. TypeScript durations are milliseconds
or strings such as `"5s"` and `"2m"`; Go uses `time.Duration`.

Foreground backup runs the CLI directly. Socket helpers connect on first use.
TypeScript's `sharedClient()` shares connections by token and session. Go's
`Default(ctx)` keeps one shared client for the process.

## Operations

Both SDKs provide package-level helpers for `startBackup`, `stopBackup`,
`stopBackupRule`, `backup`, `failover`, `replicate`, `replicatePrefix`,
`restore`, and `versions`. Go capitalizes each helper name. For `acquire`,
`release`, `offload`, and `writer`, TypeScript also exports package-level
helpers. In Go, call these methods on a connected client.

| TypeScript | Go | Returns when / result |
| --- | --- | --- |
| `await startBackup(pathOrHandle, options?)` | `StartBackup(ctx, path, *StartBackupArgs)` | Durable local rule acknowledgement |
| `await stopBackup(name)` | `StopBackup(ctx, name)` | Stop a single-file backup on this machine by remote name |
| `await stopBackupRule(rule.id)` | `StopBackupRule(ctx, rule.ID)` | Stop a whole backup rule, including future matches |
| `await backup(pathOrHandle, options?)` | `Backup(ctx, path, *BackupArgs)` | Foreground job acknowledged and locks released |
| `replicate(name, options?)` | `Replicate(ctx, name, *ReplicateArgs)` | First local version available |
| `replicatePrefix(prefix, { to, writeProtection? })` | `ReplicatePrefix(ctx, prefix, *ReplicatePrefixArgs)` | Namespace registered; files download in background |
| `restore(name, { to, version?, time? })` | `Restore(ctx, name, *RestoreArgs)` | New file materialized and checked |
| `versions(name, { after?, limit? }?)` | `Versions(ctx, name, *VersionsArgs)` | Retained restorable versions, oldest first; SDK follows pagination |
| `acquire(name, options?)` | `Acquire(ctx, name, *AcquireArgs)` | Ownership obtained, local file current |
| `release(name, options?)` | `Release(ctx, name, *ReleaseArgs)` | Final sync and ownership released |
| `offload(name)` | `Offload(ctx, name)` | Remove released local file after bucket durability |
| `writer(name)` | `Writer(ctx, name)` | Writer machine and lease |
| `status(after?, limit?)` | `StatusPage(ctx, after, limit)` | Session and database status |
| `database(name, { signal }?)` | `Database(ctx, name)` | One local database's status |
| `databasesPage(after, limit)` | `DatabasesPage(ctx, after, limit)` | Returns databases and `next`. Pass that cursor as `after` until it is absent. |
| `sync(name, { signal }?)` | `Sync(ctx, name)` | Explicit server acknowledgement |
| `backups.list({ signal }?)` | `Backups(ctx)` | Registered backups |
| `replicas.list()` | `Replicas(ctx)` | Prefix rules and replicas |
| `replicas.stop(name)` | `StopReplica(ctx, name)` | Stop one replica; file stays |
| `replicas.stopPrefix(prefix, to)` | `StopReplicaPrefix(ctx, prefix, to)` | Stop prefix and replicas; files stay |
| `conflicts.list()` / `conflicts.delete(id)` | `Conflicts(ctx)` / `DeleteConflict(ctx, id)` | Inspect / remove divergent saved copies |
| `events({ after?, wait?, signal? }?)` | `Events(ctx, *EventsArgs)` | TS async iterator; Go one page with `Next` cursor |
| `eventPage({ after?, waitMillis?, limit?, signal? }?)` | `Events(ctx, *EventsArgs)` | One event page |
| `close()` | `Close()` | Close SDK connection and watchers; daemon stays |
| `shutdown({ wait?, timeout? }?)` | `Shutdown(ctx, *ShutdownArgs)` | Drain and stop the whole session |

## Release compatibility

Use Litebackup CLI and SDK 0.4.0 or newer for the explicit backup commands. See the [breaking change in the release notes](../CHANGELOG.md). Releases through 0.3.32 use the earlier continuous `backup` behavior.

## Backup options

| TypeScript | Go | Meaning |
| --- | --- | --- |
| `name` | `Name` | Exact remote database name |
| `prefix` | `Prefix` | Prefix before relative path |
| `wait: "server" \| "cloud"` | `Wait: DurabilityServer / DurabilityCloud` | Foreground durability, default server |
| `timeout` | `Timeout` | Bounds the foreground job |
| `maximumLocalDatabases` | `MaximumLocalDatabases` | Offload idle files above a positive count once bucket-safe |
| `token`, `session` | Select through `Connect` / `Default` | Shared-client choice for package-level TS helper |

TypeScript accepts a path or a duck-typed `better-sqlite3` handle. The handle
supplies `name`, `memory`, and `open`. In-memory handles are rejected.
Recursive globs use **. See [backup semantics](sqlite-backup.md).

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
