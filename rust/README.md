# Litebackup for Rust

Back up, replicate, and restore SQLite databases with your Litebackup server
and bucket, from Rust. Rust 1.94 or later and a tokio runtime, on Linux or
macOS.

Add the crate `litebackup` from crates.io, at the version of the
`litebackup` binary:

```toml
[dependencies]
litebackup = "1.5.0"
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
```

```sh
export LITEBACKUP_API_KEY='JOIN_TOKEN@your-instance.litebackup.cloud'
```

`LITEBACKUP_API_KEY` is the value from **Machines → Add machine**, or an API
key followed by @ and your server address. The crate carries no
`litebackup` command, because the binary is over the crates.io size limit:
install it with `curl -fsSL https://litebackup.com/cli.sh | bash`, or set
`LITEBACKUP_PATH`. The SDK uses the builder's `binary_path`, then
`LITEBACKUP_PATH`, then `litebackup` on `PATH`, then
`~/.local/bin/litebackup`, and nothing is downloaded at runtime.

## Back up an app's database

```rust
use litebackup::{BackupOptions, Client};

#[tokio::main]
async fn main() -> litebackup::Result<()> {
    let client = Client::builder().connect().await?;
    let rule = client.backups().start("data/app.sqlite", BackupOptions::default()).await?;
    println!("{} {}", rule.id, rule.name);
    client.close();
    Ok(())
}
```

`backups().start()` returns the saved rule when the same path was registered
with the same settings, so call it on every start. Different settings for a
saved path fail with `rule_exists`, with the saved rule in
`error.details()["rule"]`. `litebackup::startup::register_backups` registers
a list of databases at once, creating a missing file empty.

Every call is async on tokio, and dropping a call's future stops waiting for
the daemon, which may still finish the operation. Options are structs built
from `Default` with setters, such as
`AcquireOptions::default().path("data/agents/42.sqlite").lock_timeout(Duration::from_secs(30))`,
so a new setting never breaks a caller. Durations are `std::time::Duration`,
and times `jiff::Timestamp`.

## Move a database between machines

A pool keeps a database open between calls. A lease locks the database for
this machine, so no other machine writes it, and keeps the lock until the
pool releases it. With the `rusqlite` feature:

```toml
litebackup = { version = "1.5.0", features = ["rusqlite"] }
```

```rust
use litebackup::integrations::rusqlite::RusqliteAdapter;
use litebackup::{Client, Durability, LeaseOptions, PoolOptions};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = Client::builder().connect().await?;
    let pool = client.databases().pool(RusqliteAdapter::default(), PoolOptions::default())?;
    let options = LeaseOptions::default().path("data/agents/42.sqlite").durability(Durability::SERVER);
    pool.lease("agents/42.sqlite", options, async |db| {
        db.call(|db| {
            db.execute_batch("CREATE TABLE IF NOT EXISTS steps (body TEXT)")?;
            db.execute("INSERT INTO steps (body) VALUES (?1)", ["tests passed"])
        })
        .await
    })
    .await?;
    pool.close().await?;
    client.close();
    Ok(())
}
```

`RusqliteConnection::call` runs the closure on the connection's own thread,
so the async runtime is never blocked. The adapters never commit: SQLite
commits each statement outside a transaction, and a transaction the work
leaves open is rolled back when the pool closes the database. Work that
fails, panics, or is cancelled keeps the lease and never offloads. The pool
releases a lease after a minute unused, on `pool.release(name)`, or on
`pool.close()`; call `close()` before the program exits.
[`examples/axum_tenants.rs`](examples/axum_tenants.rs) serves each tenant's
database from a pool in an axum service.

## Failovers

```rust
use futures_util::StreamExt as _;
use litebackup::{FailoverNotice, FailoverOptions};

let failover = client.failovers().start("/var/lib/app/main.sqlite", FailoverOptions::default()).await?;
if let Some(mut notices) = failover.notices() {
    tokio::spawn(async move {
        while let Some(notice) = notices.next().await {
            if let FailoverNotice::Role(role) = notice {
                eprintln!("this machine is now {role}");
            }
        }
    });
}
if failover.is_origin() {
    // your code: write one batch, well under 15 seconds
}
```

`notices()` is an ordered stream of the follower's notices:
`FailoverNotice::Role`, `FailoverNotice::Refused`, and `FailoverNotice::Error`.
`role()` reports `Role::UNKNOWN` 15 s after the daemon last confirmed it,
even if the follower stalls, so `is_origin()` never holds for a stale origin.

## Features

| Feature | Adds |
| --- | --- |
| `release` | `release::find_release`, which verifies Litebackup's signed package metadata, as served at `https://litebackup.com/pkg/metadata`, for an app that downloads the litebackup binary itself |
| `rusqlite` | `integrations::rusqlite::RusqliteAdapter`, a pool adapter whose connections each run on a thread of their own |
| `sqlx` | `integrations::sqlx::SqlxAdapter`, a pool adapter whose lease work gets sqlx's `SqliteConnection` |
| `tracing` | Spans for connecting, leasing, releasing, and failovers, and events for what the SDK does on its own, through `tracing` |

No feature is on by default. rusqlite and sqlx share `libsqlite3-sys`, which
links one bundled SQLite.

## Errors

Every failure is a `litebackup::Error` with `code()`, `message()`,
`status()`, and `details()`. `litebackup::ErrorCode` names the codes worth
matching:

```rust
use litebackup::{AcquireOptions, ErrorCode};

let options = AcquireOptions::default().path("data/agents/42.sqlite");
if let Err(error) = client.databases().acquire("agents/42.sqlite", options).await {
    if error.code() != ErrorCode::LOCK_UNAVAILABLE {
        return Err(error);
    }
    eprintln!("another machine writes agents/42.sqlite; try again later");
}
```

## Minimum Rust version

The crate builds with Rust 1.94 and later, edition 2024 (`rust-version =
"1.94"`). CI checks 1.94.1 and the current stable release.

## Development

`scripts/test` runs rustfmt, Clippy with each feature alone, with none, and
with all, the tests, and rustdoc in `rust:1.94.1-bookworm` and the current
stable image as the calling user, and, on stable, cargo-deny and a check that
`src/generated/models.rs` is what the generator writes.
`scripts/test --rust 1.94.1` runs one version, `scripts/test -- cargo test
--all-features --test pool` runs a command in the crate's directory, and
`scripts/test generate` regenerates `src/generated/models.rs` from
`../client-openapi.yaml`. `scripts/check-package` packages the crate and
builds a clean consumer project against it in the 1.94.1 image without
publishing anything. The [Rust guide](../docs/sqlite-rust.md) has the
details, the [SDK reference](../docs/sdk-reference.md) lists every method,
and [the contract](../docs/sdk-contract.md) the behavior the Go, TypeScript,
Python, Ruby, PHP, and Rust SDKs share.
