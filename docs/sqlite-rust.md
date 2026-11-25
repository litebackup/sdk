# SQLite backups from Rust

The Rust SDK talks to the litebackup client on the same machine, as the other
SDKs do. It needs Rust 1.94 or later and a tokio runtime on Linux or macOS.
Add the crate `litebackup` from crates.io, with the features you need:

```toml
[dependencies]
litebackup = { version = "1.6.0", features = ["rusqlite"] }
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
```

The crate bundles no `litebackup` binary, because the binary is over the
crates.io size limit. The SDK uses the builder's `binary_path`, then
`LITEBACKUP_PATH`, then `litebackup` on `PATH`, then
`~/.local/bin/litebackup`; install it with
`curl -fsSL https://litebackup.com/cli.sh | bash`.

## Plain Rust

```rust
use litebackup::{BackupOptions, Client, VersionsOptions};

#[tokio::main]
async fn main() -> litebackup::Result<()> {
    let client = Client::builder().connect().await?;
    let rule = client.backups().start("/var/lib/app/main.sqlite", BackupOptions::default()).await?;
    println!("{} {}", rule.id, rule.name);
    for version in client.databases().versions("main.sqlite", VersionsOptions::default()).await? {
        println!("{} {:?}", version.version, version.time);
    }
    client.close();
    Ok(())
}
```

`Client::builder()` takes `api_key`, `session`, `socket_path`,
`binary_path`, `launch`, and `environment`, which the
[SDK reference](sdk-reference.md) describes, and `connect().await` starts the
session's daemon when nothing listens on its socket. A client is cheap to
clone, and clones share its connections. `close()` ends the calls in flight
with `closed`, stops its watches and failover followers, and never stops the
daemon. A client belongs to the process that connected it: in a child made
with `fork`, its calls fail with `invalid_argument`, so connect one there.

`backups().start()` returns the saved rule when the same path was registered
with the same settings, so call it on every start. Different settings for a
saved path fail with `rule_exists`, with the saved rule in
`error.details()["rule"]`.

Every call is async. Optional settings are options structs built from
`Default` with setters, such as
`AcquireOptions::default().path("data/agents/42.sqlite").lock_timeout(Duration::from_secs(30))`;
the structs are `#[non_exhaustive]`, so a new setting never breaks a caller.
Durations are `std::time::Duration`, times `jiff::Timestamp`, and paths
anything that is `AsRef<Path>`. Results are structs with the wire names as
fields, such as `status.synced_server.version`. `databases().all()`,
`databases().all_versions()`, and `events().watch()` return a `Stream`:

```rust
use futures_util::StreamExt as _;
use litebackup::{EventType, WatchOptions};

let mut events = std::pin::pin!(client.events().watch(WatchOptions::default().after(-1)));
while let Some(event) = events.next().await {
    let event = event?;
    if event.r#type == EventType::RELEASED {
        println!("{} was released", event.name.unwrap_or_default());
    }
}
```

## Register backups at startup

`litebackup::startup::register_backups` starts the backup of each database in
order and returns the saved rules. Call it at every deploy and in each
long-running worker:

```rust
use litebackup::startup::{DatabaseBackup, register_backups};
use litebackup::{BackupOptions, Client};

let client = Client::builder().connect().await?;
let backups = [
    DatabaseBackup::new("/var/lib/app/main.sqlite"),
    DatabaseBackup::new("/var/lib/app/queue.sqlite").options(BackupOptions::default().name("app/queue.sqlite")),
];
register_backups(&client, &backups).await?;
```

A database file that does not exist yet is created empty, the database
SQLite would create on first connect, when its directory exists. A memory
database or a missing directory is `invalid_argument`, and a path saved with
different settings is `rule_exists`; each error names the database. It never
shuts the daemon down.

## Restore

```rust
use jiff::{SignedDuration, Timestamp};
use litebackup::RestoreOptions;

let hour_ago = Timestamp::now() - SignedDuration::from_hours(1);
let restored = client
    .databases()
    .restore("main.sqlite", "/var/lib/app/main-restored.sqlite", RestoreOptions::default().time(hour_ago))
    .await?;
println!("{}", restored.version);
```

A restore writes a new file and checks it; an existing file is never
replaced and fails with `file_exists`. Set `version` instead of `time` for
an exact version.

## Leases with a pool

A pool keeps databases open between calls. The `rusqlite` feature adds
`litebackup::integrations::rusqlite::RusqliteAdapter`, and the `sqlx` feature
`litebackup::integrations::sqlx::SqlxAdapter`. Both open an existing file
read-write and never create one, so a missing file fails the open.

```rust
use std::time::Duration;

use litebackup::integrations::rusqlite::RusqliteAdapter;
use litebackup::{Client, Durability, LeaseOptions, PoolOptions};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = Client::builder().connect().await?;
    let pool_options = PoolOptions::default().idle_release(Duration::from_secs(300));
    let pool = client.databases().pool(RusqliteAdapter::default(), pool_options)?;
    let lease_options = LeaseOptions::default()
        .path("data/agents/42.sqlite")
        .durability(Durability::CLOUD)
        .offload(true);
    pool.lease("agents/42.sqlite", lease_options, async |db| {
        db.call(|db| {
            let transaction = db.transaction()?;
            transaction.execute_batch("CREATE TABLE IF NOT EXISTS steps (body TEXT)")?;
            transaction.execute("INSERT INTO steps (body) VALUES (?1)", ["tests passed"])?;
            transaction.commit()
        })
        .await
    })
    .await?;
    pool.close().await?;
    client.close();
    Ok(())
}
```

rusqlite blocks while SQLite works, so each `RusqliteConnection` lives on a
thread of its own: `call` sends the closure to that thread and awaits its
result, so the async runtime is never blocked. A closure keeps going to its
end even when the future that awaits it is dropped, and the pool's close
waits for it. `RusqliteAdapter::default().setup(|db| db.pragma_update(None,
"foreign_keys", true))` runs on each connection after it opens. sqlx is async
already, so its lease work gets the `SqliteConnection` itself:

```rust
use litebackup::integrations::sqlx::SqlxAdapter;
use litebackup::{LeaseOptions, PoolOptions};

let pool = client.databases().pool(SqlxAdapter::default(), PoolOptions::default())?;
pool.lease("agents/42.sqlite", LeaseOptions::default().path("data/agents/42.sqlite"), async |db| {
    sqlx::query("INSERT INTO steps (body) VALUES (?)").bind("tests passed").execute(&mut *db).await
})
.await?;
```

The first lease of a name locks the database for this machine and opens it,
creating an empty file when none is there unless `require_existing(true)`.
Later leases reuse the lock and the open connection, after a status read
that fails with `lock_lost` when another machine broke the lock; the call
after that acquires again. `lock_timeout` is how long the first lease waits
while another machine has the write lock; without it the lease fails at once
with `lock_unavailable`. With `durability`, work that returns comes back once
the server, or with `Durability::CLOUD` the bucket, has what it committed.
Calls on one name run one at a time. `pool.use_database(name, work)` is for a
database a backup rule on this machine covers.

The pool releases a lease after `idle_release` unused (a minute by default,
`DEFAULT_IDLE_RELEASE`; `Duration::MAX` keeps it), on
`pool.release(name).await`, or on `pool.close().await`. Closing rolls back a
transaction the work left open, never commits it, and closes the database;
the release then syncs a final time, and with `offload` removes the file.
`cleanup_timeout` bounds the close and the release together, 10 minutes by
default (`DEFAULT_CLEANUP_TIMEOUT`), and `on_release_error` hears a close or
release the pool ran on its own that failed. Work that fails, panics, or is
cancelled counts as unfinished: the lease stays, and its release never
offloads the file. Never call `close()` from inside a lease's work, which it
waits for.

A lease fails with a `LeaseError`:

| Variant | What failed | What the pool leaves |
| --- | --- | --- |
| `LeaseError::Work(error)` | The work returned this error | The lease and its local copy |
| `LeaseError::Open { error, release_failure }` | The adapter could not open the database | Released without offload; `release_failure` is the release's `lease_cleanup_failed` when it failed too |
| `LeaseError::Litebackup(error)` | The acquire, the status read (`lock_lost`), the sync or the durability wait, a cancellation (`cancelled`), or the arguments | The lease and its local copy |

`LeaseError::code()` is the Litebackup code, or `None` for the work's or the
adapter's own error. A close that fails never releases, because a connection
may still be open. `pool.close()` fails with a `PoolCloseError` whose
`errors` hold one `lease_cleanup_failed` error per database left as it was;
`error.lease_cleanup()` names its `step` (`LeaseCleanupStep::Close` or
`LeaseCleanupStep::Release`), `name`, and `path`:

```rust
if let Err(failed) = pool.close().await {
    for error in &failed.errors {
        if let Some(cleanup) = error.lease_cleanup() {
            eprintln!("{} at {} stays acquired: {error}", cleanup.name, cleanup.path.display());
        }
    }
}
```

A pool is cheap to clone, and clones share it. Dropping the last handle
without `close()` makes a best-effort cleanup on the current tokio runtime:
each connection is closed, and a leased database is released without offload
only after its close succeeded. With no runtime, or when the runtime shuts
down first, the leases stay acquired; release them with
`litebackup release NAME` or `client.databases().release(name, ..)`.

## Cancellation

Dropping any call's future stops its wait; the daemon may still finish the
operation, as an acquire may still lock the database. A lease also takes a
`CancellationToken`, as a TypeScript lease takes a signal:

```rust
use litebackup::{CancellationToken, LeaseOptions};

let cancellation = CancellationToken::new();
let options = LeaseOptions::default().path("data/agents/42.sqlite").cancellation(cancellation.clone());
// cancellation.cancel() from a shutdown signal ends the lease's waits.
```

A token cancelled before the lease starts sends nothing, and the lease fails
with `cancelled`. One cancelled by the time the work returns fails the lease
with `cancelled` even when the work succeeded. Either way, and when the lease
future is dropped, the pool keeps the lease and its release never offloads
the file. A release or a close that started keeps going when its future is
dropped, so its cleanup finishes.

## Failovers

```rust
use std::time::Duration;

use futures_util::StreamExt as _;
use litebackup::{FailoverNotice, FailoverOptions};

let failover = client.failovers().start("/var/lib/app/main.sqlite", FailoverOptions::default()).await?;
if let Some(mut notices) = failover.notices() {
    tokio::spawn(async move {
        while let Some(notice) = notices.next().await {
            match notice {
                FailoverNotice::Role(role) => eprintln!("this machine is now {role}"),
                FailoverNotice::Denied(message) => eprintln!("the daemon denied this file: {message}"),
                FailoverNotice::Error(error) => eprintln!("reading the failover's events failed: {error}"),
                _ => {}
            }
        }
    });
}
loop {
    if failover.is_origin() {
        // your code: write one batch, well under 15 seconds
    }
    tokio::time::sleep(Duration::from_secs(1)).await;
}
```

A task follows the failover from the start. `notices()` hands out, once, an
ordered stream of what it reports: first the role the start answered, then
every role change (`Role::UNKNOWN` included), each refusal of this machine's
file, and each failed read, which the follower tries again. Notices wait in
the stream until it is read, and dropping it does not stop following.
`role()` is `Role::ORIGIN`, `Role::REPLICA`, or `Role::UNKNOWN`, and
`is_origin()` is false while it is unknown: while the daemon does not answer,
15 s after the last confirmation even if the follower stalls, and once the
failover stopped or its client closed. A program that writes only while
`is_origin()` holds never writes as a stale origin. `failover.stop().await`
stops the failover and releases its write lock.

## Tracing

The `tracing` feature adds spans for connecting (`litebackup.connect`),
leasing (`litebackup.lease`, `litebackup.use_database`), releasing
(`litebackup.release`, `litebackup.pool_close`), and starting a failover
(`litebackup.failover_start`), and events for what the SDK does on its own,
such as a watch's retried long-poll or a pool dropped without `close()`.

## Errors

Every call fails with a `litebackup::Error`, which has `code()`, `message()`,
`status()`, and `details()`; it displays as the code and the message.
`litebackup::ErrorCode` names the codes worth matching, and a code the
daemon answers with that it does not list is kept verbatim:

```rust
use litebackup::{AcquireOptions, ErrorCode};

let options = AcquireOptions::default().path("data/agents/42.sqlite");
match client.databases().acquire("agents/42.sqlite", options).await {
    Ok(acquired) => println!("acquired: {}", acquired.decision),
    Err(error) if error.code() == ErrorCode::LOCK_UNAVAILABLE => {
        eprintln!("another machine writes agents/42.sqlite; try again later");
    }
    Err(error) => return Err(error),
}
```

[Troubleshooting](sqlite-backup-errors.md) lists what each code means and what
to do.
