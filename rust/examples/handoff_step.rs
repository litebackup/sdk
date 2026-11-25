//! The Rust step of the cross-language handoff test, which
//! `sdk/python/tests/test_integration.py` runs through `scripts/rust-in-docker`
//! when `LITEBACKUP_RUST` names a command.
//!
//! Usage: `cargo run --example handoff_step --features rusqlite -- NAME PATH EXPECTED-ROWS ADDED-ROWS`
//!
//! It leases NAME at PATH in the session `LITEBACKUP_SESSION`, checks that
//! the database holds EXPECTED-ROWS rows, adds ADDED-ROWS, and closes the
//! pool, which releases the database. Its daemon runs in this container, so
//! the step shuts the session down before it exits: the next step's machine
//! acquires the database from the server.

use litebackup::integrations::rusqlite::RusqliteAdapter;
use litebackup::{Client, Durability, LeaseOptions, PoolOptions, ShutdownOptions};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let [name, path, expected, added] = arguments.as_slice() else {
        return Err("usage: handoff_step NAME PATH EXPECTED-ROWS ADDED-ROWS".into());
    };
    let expected: i64 = expected.parse()?;
    let added: i64 = added.parse()?;
    let client = Client::builder().connect().await?;
    let stepped = lease_and_add(&client, name, path, expected, added).await;
    let shutdown = client.session().shutdown(ShutdownOptions::default().durability(Durability::SERVER)).await;
    client.close();
    stepped?;
    let shutdown = shutdown?;
    if !shutdown.failed.is_empty() {
        let names: Vec<&str> = shutdown.failed.iter().map(|failure| failure.name.as_str()).collect();
        eprintln!("the session kept {}", names.join(", "));
    }
    println!("rust handoff ok");
    Ok(())
}

async fn lease_and_add(
    client: &Client,
    name: &str,
    path: &str,
    expected: i64,
    added: i64,
) -> Result<(), Box<dyn std::error::Error>> {
    let pool = client.databases().pool(RusqliteAdapter::default(), PoolOptions::default())?;
    let options = LeaseOptions::default().path(path).durability(Durability::SERVER);
    let leased = pool
        .lease(name, options, async |db| {
            db.call(move |db| {
                let found: i64 = db.query_row("SELECT count(*) FROM steps", [], |columns| columns.get(0))?;
                if found != expected {
                    return Ok(Err(format!("found {found} rows, want {expected}")));
                }
                let transaction = db.transaction()?;
                for index in 0..added {
                    transaction.execute("INSERT INTO steps (body) VALUES (?1)", [format!("rust {index}")])?;
                }
                transaction.commit()?;
                Ok(Ok(()))
            })
            .await
        })
        .await;
    let closed = pool.close().await;
    leased??;
    closed?;
    Ok(())
}
