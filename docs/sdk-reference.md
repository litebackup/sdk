# Litebackup SDK reference for TypeScript, Python, Ruby, PHP, Go, and Rust

The six SDKs use the local client's [socket API](../client-openapi.yaml) and
behave the same way; [the SDK contract](sdk-contract.md) is the shared
behavior, checked by the same fixtures in each SDK's tests. Wire fields are
snake_case: TypeScript results use camelCase, Python keeps the wire names on
frozen dataclasses, Ruby keeps them on `Data` classes, PHP has readonly
classes with camelCase properties, Go has generated structs with JSON
tags, and Rust has generated structs with the wire names as fields. Times
are strings in TypeScript, `datetime` in Python, `Time` in Ruby,
`DateTimeImmutable` in PHP, `time.Time` in Go, where an absent optional
time is the zero time, and `jiff::Timestamp` in Rust, where it is `None`.

The Ruby gem is on RubyGems (`gem install litebackup`), the Composer
package on Packagist (`composer require litebackup/sdk`), and the Rust crate
on crates.io (`cargo add litebackup`); see the [Ruby](../ruby/README.md),
[PHP](../php/README.md), and [Rust](../rust/README.md) READMEs.

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

```ruby
require "litebackup"

Litebackup::Client.open(session: "default") do |litebackup|
  # ...
end
```

```php
<?php

use Litebackup\Client;

$litebackup = new Client(session: 'default');
try {
    // your code
} finally {
    $litebackup->close();
}
```

```go
client, err := litebackup.Connect(ctx, &litebackup.ConnectArgs{Session: "default"})
```

```rust
let client = litebackup::Client::builder().session("default").connect().await?;
```

| Option | TypeScript | Python | Ruby | PHP | Go | Rust | Meaning |
| --- | --- | --- | --- | --- | --- | --- | --- |
| API key | `apiKey` | `api_key` | `api_key:` | `apiKey:` | `APIKey` | `api_key()` | `JOIN_TOKEN@HOST` or `API_KEY@HOST`; the daemon keeps the key it saved unless this holds a different credential, refused with `drain_required` while the client holds write locks; `LITEBACKUP_API_KEY` by default |
| Session | `session` | `session` | `session:` | `session:` | `Session` | `session()` | `LITEBACKUP_SESSION`, then `default` |
| Socket | `socketPath` | `socket_path` | `socket_path:` | `socketPath:` | `SocketPath` | `socket_path()` | Connect to this socket and never start a daemon |
| Binary | `binaryPath` | `binary_path` | `binary_path:` | `binaryPath:` | `BinaryPath` | `binary_path()` | The `litebackup` binary that starts the daemon |
| Launch | `launch: false` | `launch=False` | `launch: false` | `launch: false` | not applicable | `launch(false)` | Never start the daemon |
| Environment | `environment` | `environment` | `environment:` | `environment:` | the process environment | `environment()` | Used to resolve sessions and start the daemon |

TypeScript, Python, Ruby, and PHP clients connect on first use; Go's
`Connect` and Rust's `connect().await` check at startup. Connecting starts the session's daemon with `litebackup
launch` when nothing listens on its socket. The binary is the explicit one,
then `LITEBACKUP_PATH`, then the one the npm package, wheel, or platform gem
bundles, then `litebackup` on `PATH`, then `~/.local/bin/litebackup`. Go,
PHP, and Rust bundle none.

Connecting fails with `daemon_unsupported` when the daemon is older than
1.1.0, and the message says how to replace it: `litebackup drain --session
NAME` stops it after a final sync, and the next call starts the new binary.
`close()` closes connections and stops watches; it never stops the daemon,
its failovers, or its acquired databases.

Go callers pass a context and a pointer to the operation's `Args` struct, or
`nil` for defaults. Rust callers pass an options struct built from `Default`
with setters, such as `AcquireOptions::default().lock_timeout(..)`; the
structs are `#[non_exhaustive]`, so fields cannot be set in a struct literal. TypeScript durations are milliseconds or strings such as
`"5s"` and `"2m"`, Python durations are seconds or a `timedelta`, Ruby
durations are seconds as a `Numeric` (an `ActiveSupport::Duration` such as
`5.minutes` is one), PHP durations are seconds as an `int` or a `float`, Go
uses `time.Duration`, and Rust `std::time::Duration`. Relative paths resolve against the working
directory; TypeScript also takes `file:` URLs, Python any `os.PathLike`, Ruby
a `Pathname` or anything with `to_path`, PHP an `SplFileInfo` or any
`Stringable`, and Rust anything that is `AsRef<Path>`. Ruby listings and
watches without a block return an `Enumerator`. PHP takes options as named
arguments, and its `all()`, `allVersions()`, and `events->watch()` return a
`Generator`. Rust's `all()`, `all_versions()`, and `events().watch()` return
a `Stream`.

## Operations

| TypeScript `litebackup.` | Python `litebackup.` | Ruby `litebackup.` | PHP `$litebackup->` | Go `client.` | Rust `client.` | Returns when / result |
| --- | --- | --- | --- | --- | --- | --- |
| `backups.start(path, options?)` | `backups.start(path, ...)` | `backups.start(path, ...)` | `backups->start(path, ...)` | `StartBackup(ctx, path, args)` | `backups().start(path, options)` | The rule is saved. Idempotent; other settings fail with `rule_exists` |
| `backups.stop(name)` | `backups.stop(name)` | `backups.stop(name)` | `backups->stop(name)` | `StopBackup(ctx, name)` | `backups().stop(name)` | A single-file backup stopped |
| `rules.list()` / `rules.stop(id)` | `rules.list()` / `rules.stop(id)` | `rules.list()` / `rules.stop(id)` | `rules->list()` / `rules->stop(id)` | `BackupRules(ctx)` / `StopBackupRule(ctx, id)` | `rules().list()` / `rules().stop(id)` | Saved rules; a whole rule stopped |
| `backups.once(path, options?)` | `backups.once(path, ...)` | `backups.once(path, ...)` | `backups->once(path, ...)` | `Backup(ctx, path, args)` | `backups().once(path, options)` | A foreground job is acknowledged at its `wait` |
| `replicas.start(name, options?)` | `replicas.start(name, ...)` | `replicas.start(name, ...)` | `replicas->start(name, ...)` | `Replicate(ctx, name, args)` | `replicas().start(name, options)` | The first local version is available |
| `replicas.startPrefix(prefix, { directory })` | `replicas.start_prefix(prefix, directory=)` | `replicas.start_prefix(prefix, directory:)` | `replicas->startPrefix(prefix, directory:)` | `ReplicatePrefix(ctx, prefix, args)` | `replicas().start_prefix(prefix, directory, options)` | The namespace is registered; files download in the background |
| `replicas.list()`, `stop(name)`, `stopPrefix(prefix, directory)` | `replicas.list()`, `stop(name)`, `stop_prefix(prefix, directory=)` | `replicas.list()`, `stop(name)`, `stop_prefix(prefix, directory:)` | `replicas->list()`, `stop(name)`, `stopPrefix(prefix, directory:)` | `Replicas`, `StopReplica`, `StopReplicaPrefix` | `replicas().list()`, `stop(name)`, `stop_prefix(prefix, directory)` | Replicas; files stay when stopped |
| `databases.restore(name, { path, version?, time?, umask? })` | `databases.restore(name, path=, version=, time=, umask=)` | `databases.restore(name, path:, version:, time:, umask:)` | `databases->restore(name, path:, version:, time:, umask:)` | `Restore(ctx, name, args)` | `databases().restore(name, path, options)` | A new file is written and checked; an existing file is never replaced. It gets 0666 less `umask`, this process's umask when omitted |
| `databases.revert(name, { version?, time? })` | `databases.revert(name, version=, time=)` | `databases.revert(name, version:, time:)` | `databases->revert(name, version:, time:)` | `Revert(ctx, name, args)` | `databases().revert(name, options)` | To roll back: the content of `version`, or of the newest version at or before `time`, is a new head, one past the current head, and every later version stays restorable. Only on the machine that writes the database, with its file closed (`still_open` otherwise); a replica or a failover standby is `invalid_argument`, and a file holding writes the server lacks is `conflict`, left untouched. The daemon takes the write lock when it does not hold it and gives it back once the new head is synced; when that sync fails for any reason but a lost lock or a refused file, which are final, it keeps the lock and syncs again on its own, so release an acquired database afterwards; writes an app makes meanwhile sync before a lock it took goes back, and a file still open keeps it (`still_open`, or `changed` when it changes during every sync) |
| `databases.versions(name, options?)` | `databases.versions(name, ...)` | `databases.versions(name, ...)` | `databases->versions(name, ...)` | `Versions(ctx, name, args)` | `databases().versions(name, options)` | Restorable versions, oldest first |
| `databases.versionsPage(name, options?)` | `databases.versions_page(name, ...)` | `databases.versions_page(name, ...)` | `databases->versionsPage(name, ...)` | `VersionsPage(ctx, name, args)` | `databases().versions_page(name, options)` | One page of them; pass `next` as `after` to continue |
| `databases.allVersions(name, options?)` | `databases.all_versions(name, ...)` | `databases.all_versions(name, ...)` | `databases->allVersions(name, ...)` | `AllVersions(ctx, name, args)` | `databases().all_versions(name, options)` | The same, a page at a time |
| `databases.acquire(name, options?)` | `databases.acquire(name, ...)` | `databases.acquire(name, ...)` | `databases->acquire(name, ...)` | `Acquire(ctx, name, args)` | `databases().acquire(name, options)` | The write lock is held and the local file is the head |
| `databases.release(name, options?)` | `databases.release(name, ...)` | `databases.release(name, ...)` | `databases->release(name, ...)` | `Release(ctx, name, args)` | `databases().release(name, options)` | The final sync reached `durability` and the lock is released; a write an app makes during the final upload syncs again before the lock goes back, and a file open again keeps it (`still_open`, or `changed` when it changes during every sync); a copy without its lock, as after a drain, whose file holds writes the server lacks is `changed`: acquire it again, then release it |
| `databases.pool(options)` | `databases.pool(adapter=...)` | `databases.pool(adapter, ...)` | `databases->pool(adapter, ...)` | `NewPool(client, args)` | `databases().pool(adapter, options)` | A pool that keeps databases open: `use` for a backup rule's database, `lease` for one that moves between machines; see below |
| `databases.offload(name, { durability? })` | `databases.offload(name, durability=)` | `databases.offload(name, durability:)` | `databases->offload(name, durability:)` | `Offload(ctx, name, args)` | `databases().offload(name, options)` | A released file is removed once the server holds its last version, or once the bucket holds it with `durability: "cloud"` |
| `databases.writer(name)` | `databases.writer(name)` | `databases.writer(name)` | `databases->writer(name)` | `Writer(ctx, name)` | `databases().writer(name)` | The writer's machine and lease |
| `databases.get(name)` | `databases.get(name)` | `databases.get(name)` | `databases->get(name)` | `Database(ctx, name)` | `databases().get(name)` | One database's status |
| `databases.sync(name)` | `databases.sync(name)` | `databases.sync(name)` | `databases->sync(name)` | `Sync(ctx, name)` | `databases().sync(name)` | The server acknowledged a sync; a released acquired database does not sync and is `invalid_argument` |
| `databases.list()` / `page()` / `all()` | `databases.list()` / `page()` / `all()` | `databases.list()` / `page()` / `all()` | `databases->list()` / `page()` / `all()` | `Databases` / `DatabasesPage` / `AllDatabases` | `databases().list()` / `page(options)` / `all()` | Every database on this machine: all at once, a page, or one at a time |
| `failovers.start(path, options?)` | `failovers.start(path, ...)` | `failovers.start(path, ...)` | `failovers->start(path, ...)` | `StartFailover(ctx, path, args)` | `failovers().start(path, options)` | This machine is the origin or holds a replica |
| `events.watch(options?)` | `events.watch(...)` | `events.watch(...)` | `events->watch(...)` | `WatchEvents(ctx, args)` | `events().watch(options)` | Events as they happen; failed long-polls are retried, and `cursor_expired` ends the watch |
| `events.page(options?)` | `events.page(...)` | `events.page(...)` | `events->page(...)` | `Events(ctx, args)` | `events().page(options)` | One page of events |
| `session.status(options?)` | `session.status(...)` | `session.status(...)` | `session->status(...)` | `Status(ctx)` / `StatusPage` | `session().status(options)` | The session, its server, and a page of databases |
| `session.shutdown(options?)` | `session.shutdown(...)` | `session.shutdown(...)` | `session->shutdown(...)` | `Shutdown(ctx, args)` | `session().shutdown(options)` | Every database final-synced and released, and the daemon stopped; each one that could not be is in `failed`, as is an acquired copy whose file holds writes the server lacks or is open again (`changed`, `still_open`) unless `durability` is `none`, and the daemon then runs on |

Every TypeScript operation takes `signal`, and Go takes `ctx`. Cancelling stops
the local wait only: the daemon may still finish the operation, and the error
is `cancelled`. In Rust, dropping a call's future stops its wait, and a pool
lease also takes a `CancellationToken` in `LeaseOptions::cancellation`, which
fails it with `cancelled` as a TypeScript signal does; a dropped lease future
counts as unfinished and never offloads. In async Python, cancelling the task raises
`asyncio.CancelledError` as usual. Ruby has no async client and no
cancellation; a pool lease block left with `break` or `return`, like one that
raises, counts as unfinished and never offloads. PHP has no async client and
no cancellation either; a lease call that `exit` or a fatal error ends counts
as unfinished, and a shutdown function releases it without offload. Calls with a `timeout` wait that long plus
30 seconds for the daemon's answer; others wait until it answers. A foreground
backup (`backups.once`, `Backup`) with a `timeout` that runs 30 seconds past
it is stopped (SIGTERM, then SIGKILL 15 seconds later) and fails with
`timeout`.

## Options

| Backup TS | Python | Ruby | PHP | Go | Rust | Meaning |
| --- | --- | --- | --- | --- | --- | --- |
| `name` | `name` | `name:` | `name:` | `Name` | `name()` | Exact remote name of a single file |
| `prefix` | `prefix` | `prefix:` | `prefix:` | `Prefix` | `prefix()` | Prefix before each relative path |
| `maximumLocalDatabases` | `maximum_local_databases` | `maximum_local_databases:` | `maximumLocalDatabases:` | `MaximumLocalDatabases` | `maximum_local_databases()` | Continuous rules: offload idle files past this count once `offloadDurability` holds them |
| `offloadDurability` | `offload_durability` | `offload_durability:` | `offloadDurability:` | `OffloadDurability` | `offload_durability()` | Continuous rules: what `maximumLocalDatabases` waits for, `server` (default: the server holds the last version) or `cloud` (the bucket holds it too) |
| `offloadRollbackJournal` | `offload_rollback_journal` | `offload_rollback_journal:` | `offloadRollbackJournal:` | `OffloadRollbackJournal` | `offload_rollback_journal(true)` | Continuous rules: `maximumLocalDatabases` offloads rollback-journal databases too; set it only when nothing keeps them open while idle |
| `durability` | `durability` | `durability:` | `durability:` | `Durability` | `OnceOptions::durability()` | `backups.once`: `server` (default) or `cloud` |
| `timeout` | `timeout` | `timeout:` | `timeout:` | `Timeout` | `OnceOptions::timeout()` | `backups.once`: bounds the job |

| Acquire TS | Python | Ruby | PHP | Go | Rust | Meaning |
| --- | --- | --- | --- | --- | --- | --- |
| `path` | `path` | `path:` | `path:` | `Path` | `path()` | Local file; the known path when omitted |
| `maintenance` | `maintenance` | `maintenance:` | `maintenance:` | `Maintenance` | `maintenance(true)` | Checkpoint and VACUUM past the freelist threshold on release |
| `offloadOnRelease: true` | `offload_on_release=True` | `offload_on_release: true` | `offloadOnRelease: true` | `OffloadOnRelease: true` | `offload_on_release(true)` | Offload after release once `offloadDurability` holds it |
| `offloadDurability: "cloud"` | `offload_durability="cloud"` | `offload_durability: "cloud"` | `offloadDurability: 'cloud'` | `OffloadDurability: DurabilityCloud` | `offload_durability(Durability::CLOUD)` | What that offload waits for: `server` (default) once the server holds the released version, `cloud` once the bucket holds it too |
| `machineLabel` | `machine_label` | `machine_label:` | `machineLabel:` | `MachineLabel` | `machine_label()` | What other machines see as the writer |
| `timeout` | `timeout` | `timeout:` | `timeout:` | `Timeout` | `timeout()` | Bounds downloading the head |
| `lockTimeout` | `lock_timeout` | `lock_timeout:` | `lockTimeout:` | `LockTimeout` | `lock_timeout()` | How long to wait while another machine holds the write lock, trying again until it is released; 0 fails at once with `lock_unavailable`. The call may take `timeout` plus `lockTimeout` |
| `umask` | `umask` | `umask:` | `umask:` | `Umask` | `umask()` | File mode creation mask: a file the acquire creates gets 0666 less it; this process's umask when omitted. A copy the head replaces keeps its bits |
| `discardLocal` | `discard_local` | `discard_local:` | `discardLocal:` | `DiscardLocal` | `discard_local(true)` | Replace a local file that diverged from the head with the head, discarding its writes the head lacks; without it such a file is refused with `conflict` and left untouched |

Release takes `durability` (`server` or `cloud`), `offload`, and `timeout`, which
bounds waiting for every connection to close and for `durability`. With `offload`
the file is removed once `durability` holds the released version: with `server`,
the default, right after the release; a later offload pass removes a copy
that is not durable as asked yet. Close all SQLite
connections first, or release fails with `still_open`.

A pool takes an `adapter`, which opens existing files only and never creates
one, `idleClose` (close a rule's database after this long unused; never by
default, 2 minutes under a rule with `maximumLocalDatabases` so it can be
offloaded), `idleRelease` (release a lease after this long unused; a minute by
default, 0 or a negative Go value to keep it, `Duration::MAX` in Rust), `cleanupTimeout` / `cleanup_timeout` / `CleanupTimeout` (one bound on
closing and releasing a lease together, 10 minutes by default),
`machineLabel`, and `onReleaseError` for releases the pool runs on its own.
PHP has no threads, so its idle close and idle release run at the start of
each pool call and in `close()`; the [PHP guide](sqlite-php.md#leases-with-a-pool)
has the details. Rust's pool takes an `Adapter` and `PoolOptions`, and the
`rusqlite` and `sqlx` features add adapters; the
[Rust guide](sqlite-rust.md#leases-with-a-pool) has the details.

`use(name, work)` runs work with a backup rule's database: the pool opens it
once and keeps it open, brings an offloaded copy back first, and refuses a
name no rule on this machine covers. `lease(name, options, work)` runs work
with a database that moves between machines, one call at a time: the first
call acquires the write lock, later calls reuse it and the open handle, and
the pool releases it after `idleRelease` (a minute unused by default), on
`release(name)`, or on `close()`, never because another machine waits. PHP's
is `lease(name, work, path: ...)`, with the work second and the options as
named arguments. Rust's is `lease(name, LeaseOptions, async |connection| ..)`,
and its `use` is `use_database(name, work)`, because `use` is a Rust keyword. Lease
options are `path` (an empty file is created there when nothing is, unless
`requireExisting`), `durability` (the call returns once the server or the
bucket holds what the work committed; the release that ends the lease waits
for it too, the server by default), `offload` (remove the file when the lease
ends, unless the last call failed), `lockTimeout`, `acquireTimeout`, and
`maintenance`. A lost lock ends the running call with `lock_lost`. See
[integrations](sqlite-integrations.md) for adapters.

Options equal to the daemon's defaults are not sent, so the SDKs send identical
requests. Replica options are `path`, `writeProtection` (`lock`, `detect`, or
`none`), and `timeout` for the first download.

## Status and errors

`DatabaseStatus` has `role` (`origin` or `replica`), `state`, the versions the
server and bucket hold (`syncedServer`, with the `time` the server took it,
and `syncedCloud`, with the time it was `archived`), `localChanged`,
`acquired`, and diagnostics such as `lastError`. States are `pending`,
`syncing`, `retrying`, `synced`, `waiting`, `offloaded`, `stopped`,
`conflicted`, and `error`. A backup or failover origin is `pending` while it
holds no write lock, as after a start or a drain, until a sync takes it. A `retrying` origin's `lastError` says why and `nextRetryTime` when
the next attempt runs. A backup or failover origin is `synced` while the
server holds every change seen, and `syncing` again from its next change; an
acquired database is `syncing` until it is released, then `synced`.
`firstUnsyncedChangeTime` is when the first change the server lacks was seen;
it is absent when the server has every change seen and may be earlier than the
real first change, never later.

Every failure is one error type with `code`, `message`, `status` (the HTTP
status, or 0 for errors the SDK raised), and `details`: `LitebackupError` in
TypeScript and Python, `Litebackup::Error` in Ruby, whose `message` is the
message alone, `Litebackup\LitebackupError` in PHP, whose code is
`$error->errorCode` because `getCode()` is PHP's integer, and
`*litebackup.Error` in Go (`IsCode(err, code)`), and `litebackup::Error` in
Rust, whose fields are the methods `code()`, `message()`, `status()`, and
`details()`; compare `error.code()` with the `litebackup::ErrorCode`
constants, such as `ErrorCode::LOCK_UNAVAILABLE`.
Codes the daemon answers with are kept verbatim; `details` holds the answer's
other fields under their wire names, such as `details.rule` for `rule_exists`.

```ts
import { ErrorCode, Litebackup, LitebackupError } from "@litebackup/sdk";

const litebackup = new Litebackup();

try {
  await litebackup.databases.acquire("agents/42.sqlite", { path: "/var/lib/app/42.sqlite" });
} catch (error) {
  if (error instanceof LitebackupError && error.code === ErrorCode.lockUnavailable) {
    console.error("Agent already running; retry later");
  } else {
    throw error;
  }
}
```

| Code | Raised by | Meaning |
| --- | --- | --- |
| `invalid_argument` | SDK or daemon | An argument failed validation; the SDK sends nothing |
| `binary_not_found` | SDK | No `litebackup` binary, or `LITEBACKUP_PATH` or the binary path option names no file; the message has the install command |
| `launch_failed` | SDK | `litebackup launch` failed |
| `drain_required` | SDK | `litebackup launch` refused another credential while the client holds write locks; shut down with the current one first |
| `connection_failed` | SDK | The socket refused or dropped the connection |
| `daemon_unsupported` | SDK | The daemon is older than 1.1.0 |
| `cancelled` | SDK | The caller cancelled; the daemon may still finish |
| `timeout` | SDK or daemon | The operation's timeout ran out |
| `closed` | SDK | The client was closed |
| `invalid_response` | SDK | The daemon's answer could not be decoded |
| `lease_cleanup_failed` | SDK | A pool could not close or release a leased database; the error is a `LeaseCleanupError` (in Rust, `error.lease_cleanup()`) whose `step` is `close` (not released, still acquired) or `release` |
| `sdk_unsupported` | daemon | This SDK is older than the daemon accepts |
| `cursor_expired` | daemon | An events cursor is older than the oldest event kept (status 410); a watch ends with it, a failover resumes at the log's end |
| `changed` | daemon | The local file changed under an offload or an acquire (status 409): written after its last sync, never synced, or written again while an acquire put the head in its place; acquire it so the write syncs, or retry once the app stops writing |
| `not_durable` | daemon | An offload refused because the chosen durability does not hold the synced version yet (status 409): the server does not, or with `durability: "cloud"` the bucket does not; retry once it does, or release with `offload` and the same `wait`, which removes the file on a later pass |
| `conflict` | daemon | The local file differs from the server head and continues no head this machine synced, and was left untouched (status 409): a sync, an acquire, a revert, or a failover refused it, and the message names the file; acquire with `discardLocal` to replace it with the head, restore the head to another path, or back the file up under another name |
| `lock_lost` | daemon | This copy's write lock was lost, taken by another machine or broken by an admin, so it is kept as conflicted with its file as it was and never synced again on its own, even when nobody wrote (status 409); an acquire resumes it while the head is still the version it last synced |
| `file_exists`, `rule_exists`, `rule_database`, `lock_unavailable`, `not_found`, `still_open`, `busy` | daemon | See [troubleshooting](sqlite-backup-errors.md) |
| `client_unsupported` | daemon | The server needs a newer litebackup client on this machine (status 426) |
| `not_configured`, `wrong_server`, `unauthenticated`, `forbidden`, `plan_limit`, `integrity`, `unavailable` | daemon | The server refused or could not be reached; the message says why (status 503); `unavailable` also answers a sync or an acquire while the daemon releases its locks before it stops |

Only event watches and failovers retry, with backoff from 250 ms to 10 s.
Acquire, release, start, stop, restore, revert, shutdown, and launch are
never retried. A watch ends with `cursor_expired` when its cursor is older than the
oldest event the daemon keeps; start again from a new cursor, such as `-1` for
the log's end. A failover reports it, resumes at the log's end, and re-reads
its role, so a role change it missed is still reported.

A failover's role callback (`OnRoleChange`, `on_role_change`, `onRoleChange`,
or the TypeScript `origin`, `replica`, and `unknown` listeners) hears the role
the start answered once, then every change. TypeScript calls it on the next
turn of the event loop, so register listeners right after `failovers.start()`
resolves; in Go, sync Python, and Ruby it may run before the start returns,
so use its role argument. Rust has no callbacks: `failover.notices()` hands
out, once, an ordered stream whose items are `FailoverNotice::Role`,
`FailoverNotice::Refused`, and `FailoverNotice::Error`, starting with the
role the start answered, and `role()` reports `Role::UNKNOWN` 15 s after the
last confirmation even if the follower stalls. PHP follows a failover only inside
`$failover->follow(seconds)`, which runs the callbacks in the calling process;
outside it nothing confirms the role, so `role()` turns `unknown` 15 s after
the last confirmation. While the daemon does not answer, the role is `unknown`
(Go `RoleUnknown`, TypeScript `FailoverRole`, PHP `Failover::UNKNOWN`, Rust
`Role::UNKNOWN`), and `IsOrigin()`, `isOrigin`, `is_origin`, Ruby's
`origin?`, PHP's `isOrigin()`, and Rust's `is_origin()` are false: at once when a call gets no answer
(`connection_failed`, `timeout`), and after 15 s of failed reads the daemon
answered. Once the daemon answers again, the failover reads its role and
reports it. A stopped failover is `unknown` too. The
[contract](sdk-contract.md#failover-roles) has the exact rules.

The refusal callback (`OnRefused`, `on_refused`, `onRefused`, the
TypeScript `refused` listener, or Rust's `FailoverNotice::Refused`) hears the daemon's message when it refused this machine's file and
the failover stays conflicted there: a file that differs from the server head when this
machine starts as the origin, or one holding writes the new origin never saw
when this machine lost the origin role. The file is left exactly as it is,
the database is `conflicted`, and the role is `unknown`, so `IsOrigin()`,
`isOrigin`, and `is_origin` are false. A refusal made before the last role
read, which a failover hears once its daemon answers again, is still reported
and leaves the role that read gave: the file may have been moved and the
failover started again meanwhile. Otherwise start it again with `discardLocal`
(`discard_local`, Go `DiscardLocal`, Rust `discard_local(true)`,
`litebackup failover --discard-local`),
which replaces the file with the head without a stop, or stop the failover,
move the file aside, and start it again.

A machine that starts a failover as a replica keeps the file at the path
only when it is empty, already the head, or a clean copy of the version this
machine last synced there; it then follows the head. Any other file is
refused the same way at the start, which answers `conflict` naming the file,
unless `discardLocal` asked for the head in its place.

## Release compatibility

The SDKs and the litebackup client share one release version, 1.3.0. An SDK
refuses a client older than 1.1.0 with `daemon_unsupported`, and the client
answers an SDK older than 1.1.0 with 426 `sdk_unsupported`. A server that needs
a newer client on the machine is refused with `client_unsupported`. Keep the
npm package, wheel, gem, or Composer package, the Go module, the Rust
crate's tag, and the client binary at the same version.

## Release verification

`findRelease(metadata)` in TypeScript, `litebackup.release.find_release` in
Python (`litebackup[release]`), `Litebackup::Release.find_release` in Ruby,
`Litebackup\Release::findRelease` in PHP (with `ext-sodium`),
`litebackup.FindRelease` in Go, and `litebackup::release::find_release` in
Rust (feature `release`) verify the
package metadata served at `https://litebackup.com/pkg/metadata` with
Litebackup's Ed25519 release keys and return the release from its signed
document. A bad signature raises `SignatureError` (Ruby
`Litebackup::Release::SignatureError`, PHP `Litebackup\ReleaseSignatureError`)
or returns `ErrSignature` or, in Rust, `ReleaseError::Signature`.
