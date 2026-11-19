# Litebackup SDK reference for TypeScript, Python, and Go

The three SDKs use the local client's [socket API](../client-openapi.yaml) and
behave the same way; [the SDK contract](sdk-contract.md) is the shared
behavior, checked by the same fixtures in each SDK's tests. Wire fields are
snake_case: TypeScript results use camelCase, Python keeps the wire names on
frozen dataclasses, and Go has generated structs with JSON tags. Times are
strings in TypeScript, `datetime` in Python, and `time.Time` in Go, where an
absent optional time is the zero time.

## Connect

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup({ session: "default" });
```

```python
from litebackup import AsyncLitebackup, Litebackup

with Litebackup(session="default") as litebackup:
    ...


async def main() -> None:
    async with AsyncLitebackup(session="default") as litebackup:
        ...
```

```go
client, err := litebackup.Connect(ctx, &litebackup.ConnectArgs{Session: "default"})
```

| Option | TypeScript | Python | Go | Meaning |
| --- | --- | --- | --- | --- |
| Join token | `token` | `token` | `Token` | `JOIN_TOKEN@HOST`, used only when the daemon first joins; `LITEBACKUP_TOKEN` by default |
| Session | `session` | `session` | `Session` | `LITEBACKUP_SESSION`, then `default` |
| Socket | `socketPath` | `socket_path` | `SocketPath` | Connect to this socket and never start a daemon |
| Binary | `binaryPath` | `binary_path` | `BinaryPath` | The `litebackup` binary that starts the daemon |
| Launch | `launch: false` | `launch=False` | not applicable | Never start the daemon |
| Environment | `environment` | `environment` | the process environment | Used to resolve sessions and start the daemon |

TypeScript and Python clients connect on first use; `connect()` checks at
startup. Connecting starts the session's daemon with `litebackup launch` when
nothing listens on its socket. The binary is the explicit one, then
`LITEBACKUP_PATH`, then the one the npm package or wheel bundles, then
`litebackup` on `PATH`, then `~/.local/bin/litebackup`. Go bundles none.

Connecting fails with `incompatible_version` when the daemon is older than
1.0.0, and the message says how to replace it: `litebackup drain --session
NAME` stops it after a final sync, and the next call starts the new binary.
`close()` closes connections and stops watches; it never stops the daemon,
its failovers, or its acquired databases.

Go callers pass a context and a pointer to the operation's `Args` struct, or
`nil` for defaults. TypeScript durations are milliseconds or strings such as
`"5s"` and `"2m"`, Python durations are seconds or a `timedelta`, and Go uses
`time.Duration`. Relative paths resolve against the working directory;
TypeScript also takes `file:` URLs and Python any `os.PathLike`.

## Operations

| TypeScript `litebackup.` | Python `litebackup.` | Go `client.` | Returns when / result |
| --- | --- | --- | --- |
| `backups.start(path, options?)` | `backups.start(path, ...)` | `StartBackup(ctx, path, args)` | The rule is saved. Idempotent; other settings fail with `exists` |
| `backups.stop(name)` | `backups.stop(name)` | `StopBackup(ctx, name)` | A single-file backup stopped |
| `rules.list()` / `rules.stop(id)` | `rules.list()` / `rules.stop(id)` | `Backups(ctx)` / `StopBackupRule(ctx, id)` | Saved rules; a whole rule stopped |
| `backups.once(path, options?)` | `backups.once(path, ...)` | `Backup(ctx, path, args)` | A foreground job is acknowledged at its `wait` |
| `replicas.start(name, options?)` | `replicas.start(name, ...)` | `Replicate(ctx, name, args)` | The first local version is available |
| `replicas.startPrefix(prefix, { to })` | `replicas.start_prefix(prefix, to=)` | `ReplicatePrefix(ctx, prefix, args)` | The namespace is registered; files download in the background |
| `replicas.list()`, `stop(name)`, `stopPrefix(prefix, to)` | `replicas.list()`, `stop(name)`, `stop_prefix(prefix, to=)` | `Replicas`, `StopReplica`, `StopReplicaPrefix` | Replicas; files stay when stopped |
| `databases.restore(name, { to, version?, time? })` | `databases.restore(name, to=, version=, time=)` | `Restore(ctx, name, args)` | A new file is written and checked; an existing file is never replaced |
| `databases.versions(name, options?)` | `databases.versions(name, ...)` | `Versions(ctx, name, args)` | Restorable versions, oldest first |
| `databases.iterateVersions(name, options?)` | `databases.iter_versions(name, ...)` | `AllVersions(ctx, name, args)` | The same, a page at a time |
| `databases.acquire(name, options?)` | `databases.acquire(name, ...)` | `Acquire(ctx, name, args)` | The write lock is held and the local file is the head |
| `databases.release(name, options?)` | `databases.release(name, ...)` | `Release(ctx, name, args)` | The final sync reached `wait` and the lock is released |
| `databases.managed(name, options, work)` | `with databases.managed(name, ...) as db` | `Managed(ctx, client, name, args, work)` | Acquire, open, work, close, release; see below |
| `databases.offload(name)` | `databases.offload(name)` | `Offload(ctx, name)` | A released file is removed once the bucket holds it |
| `databases.writer(name)` | `databases.writer(name)` | `Writer(ctx, name)` | The writer's machine and lease |
| `databases.get(name)` | `databases.get(name)` | `Database(ctx, name)` | One database's status |
| `databases.sync(name)` | `databases.sync(name)` | `Sync(ctx, name)` | The server acknowledged a sync |
| `databases.list()` / `iterate()` / `page()` | `databases.list()` / `iter_all()` / `page()` | `Databases` / `AllDatabases` / `DatabasesPage` | Every database on this machine |
| `failovers.start(path, options?)` | `failovers.start(path, ...)` | `Failover(ctx, path, args)` | This machine is the origin or holds a replica |
| `conflicts.list()` / `delete(id)` | `conflicts.list()` / `delete(id)` | `Conflicts` / `DeleteConflict` | Conflict copies |
| `events.watch(options?)` | `events.watch(...)` | `WatchEvents(ctx, args)` | Events as they happen; failed long-polls are retried |
| `events.page(options?)` | `events.page(...)` | `Events(ctx, args)` | One page of events |
| `session.status(options?)` | `session.status(...)` | `Status(ctx)` / `StatusPage` | The session, its server, and a page of databases |
| `session.shutdown(options?)` | `session.shutdown(...)` | `Shutdown(ctx, args)` | Every database final-synced and the daemon stopped |

Every TypeScript operation takes `signal`, and Go takes `ctx`. Cancelling stops
the local wait only: the daemon may still finish the operation, and the error
is `cancelled`. In async Python, cancelling the task raises
`asyncio.CancelledError` as usual. Calls with a `timeout` wait that long plus
30 seconds for the daemon's answer; others wait until it answers.

## Options

| Backup TS | Python | Go | Meaning |
| --- | --- | --- | --- |
| `name` | `name` | `Name` | Exact remote name of a single file |
| `prefix` | `prefix` | `Prefix` | Prefix before each relative path |
| `maximumLocalDatabases` | `maximum_local_databases` | `MaximumLocalDatabases` | Continuous rules: offload idle files past this count once the bucket holds them |
| `wait` | `wait` | `Wait` | `backups.once`: `server` (default) or `cloud` |
| `timeout` | `timeout` | `Timeout` | `backups.once`: bounds the job |

| Acquire TS | Python | Go | Meaning |
| --- | --- | --- | --- |
| `to` | `to` | `To` | Local file; the known path when omitted |
| `maintenance` | `maintenance` | `Maintenance` | Checkpoint and VACUUM past the freelist threshold on release |
| `offload: "released"` | `offload="released"` | `OffloadReleased` | Offload after release once the bucket holds it |
| `machineLabel` | `machine_label` | `MachineLabel` | What other machines see as the writer |
| `onLost: "block"` | `on_lost="block"` | `BlockOnLost` | Block local writes after a lost lock instead of saving a conflict copy |
| `timeout` | `timeout` | `Timeout` | Bounds downloading the head |

Release takes `wait` (`server` or `cloud`), `offload`, and `timeout`, which
bounds waiting for every connection to close and for `wait`. Close all SQLite
connections first, or release fails with `still_open`.

Managed scopes take the acquire options plus `create` (true by default:
create an empty file at `to` when nothing is there; Go spells it
`RequireExisting` for false), `durability` (required, the
release's `wait`), `offload` (only after the work succeeded), `cleanupTimeout`
/ `cleanup_timeout` / `CleanupTimeout` (10 minutes by default), and an
`adapter`. See [integrations](sqlite-integrations.md) for adapters and the
failure rules.

Options equal to the daemon's defaults are not sent, so the SDKs send identical
requests. Replica options are `to`, `writeProtection` (`lock`, `detect`, or
`none`), and `timeout` for the first download.

## Status and errors

`DatabaseStatus` has `role` (`origin` or `replica`), `state`, the versions the
server and bucket hold (`syncedServer`, `syncedCloud`), `localChanged`,
`acquired`, and diagnostics such as `lastError`. States are `syncing`,
`retrying`, `synced`, `waiting`, `offloaded`, `stopped`, `conflicted`, and
`error`. A `retrying` origin's `lastError` says why and `nextRetryTime` when
the next attempt runs. `firstUnsyncedChangeTime` is when the first change the
server lacks was seen; it is absent when the server has every change seen and
may be earlier than the real first change, never later.

Every failure is one error type with `code`, `message`, `status` (the HTTP
status, or 0 for errors the SDK raised), and `details`: `LitebackupError` in
TypeScript and Python, `*litebackup.Error` in Go (`IsCode(err, code)`).
Codes the daemon answers with are kept verbatim; `details` holds the answer's
other fields under their wire names, such as `details.rule` for `exists`.

```ts
import { Litebackup, LitebackupError } from "@litebackup/sdk";

const litebackup = new Litebackup();

try {
  await litebackup.databases.acquire("agents/42.sqlite", { to: "/var/lib/app/42.sqlite" });
} catch (error) {
  if (error instanceof LitebackupError && error.code === "in_use") {
    console.error("Agent already running; retry later");
  } else {
    throw error;
  }
}
```

| Code | Raised by | Meaning |
| --- | --- | --- |
| `invalid_argument` | SDK or daemon | An argument failed validation; the SDK sends nothing |
| `binary_not_found` | SDK | No `litebackup` binary; the message has the install command |
| `launch_failed` | SDK | `litebackup launch` failed |
| `connection_failed` | SDK | The socket refused or dropped the connection |
| `incompatible_version` | SDK | The daemon is older than 1.0.0 |
| `cancelled` | SDK | The caller cancelled; the daemon may still finish |
| `timeout` | SDK or daemon | The operation's timeout ran out |
| `closed` | SDK | The client was closed |
| `invalid_response` | SDK | The daemon's answer could not be decoded |
| `scope_cleanup_failed` | SDK | A managed scope could not close or release; `details.step` says which |
| `sdk_unsupported` | daemon | This SDK is older than the daemon accepts |
| `exists`, `in_use`, `not_found`, `still_open`, `busy` | daemon | See [troubleshooting](sqlite-backup-errors.md) |

Only event watches retry, with backoff from 250 ms to 10 s. Acquire, release,
start, stop, restore, shutdown, and launch are never retried.

## Release verification

`findRelease(metadata)` in TypeScript, `litebackup.release.find_release` in
Python (`litebackup[release]`), and `litebackup.FindRelease` in Go verify the
package metadata served at `https://litebackup.com/pkg/metadata` with
Litebackup's Ed25519 release keys and return the release from its signed
document. A bad signature raises `SignatureError` or returns `ErrSignature`.
