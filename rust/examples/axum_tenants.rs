//! An axum service that keeps each tenant's SQLite database in a Litebackup
//! pool: the first request for a tenant leases its database on this
//! machine, later ones reuse the open connection, and the pool releases it
//! after a minute without requests, so another machine can serve it.
//!
//! Run it with `cargo run --example axum_tenants --features rusqlite`, then
//! `curl -X POST localhost:3000/tenants/42/notes -d 'hello'`.

use std::sync::Arc;

use axum::Router;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::routing::post;
use litebackup::integrations::rusqlite::RusqliteAdapter;
use litebackup::{LeaseError, LeaseOptions, Pool, PoolOptions};

/// What every request shares: the pool of tenant databases.
struct Tenants {
    pool: Pool<RusqliteAdapter>,
}

/// Adds a note to a tenant's database and answers with how many it holds.
async fn add_note(
    State(tenants): State<Arc<Tenants>>,
    Path(tenant): Path<u32>,
    body: String,
) -> Result<String, (StatusCode, String)> {
    let name = format!("tenants/{tenant}.sqlite");
    let options = LeaseOptions::default().path(format!("data/{name}"));
    let count = tenants
        .pool
        .lease(&name, options, async |db| {
            db.call(move |db| {
                db.execute_batch("CREATE TABLE IF NOT EXISTS notes (body TEXT NOT NULL)")?;
                db.execute("INSERT INTO notes (body) VALUES (?1)", [body])?;
                db.query_row("SELECT count(*) FROM notes", [], |columns| columns.get::<_, i64>(0))
            })
            .await
        })
        .await
        .map_err(|error| match error {
            LeaseError::Litebackup(error) if error.code() == litebackup::ErrorCode::LOCK_UNAVAILABLE => {
                (StatusCode::SERVICE_UNAVAILABLE, "another machine serves this tenant".to_owned())
            }
            error => (StatusCode::INTERNAL_SERVER_ERROR, error.to_string()),
        })?;
    Ok(format!("{count} notes\n"))
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = litebackup::Client::builder().connect().await?;
    let pool = client.databases().pool(RusqliteAdapter::default(), PoolOptions::default())?;
    let tenants = Arc::new(Tenants { pool: pool.clone() });
    let app = Router::new().route("/tenants/{tenant}/notes", post(add_note)).with_state(tenants);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:3000").await?;
    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    // Every leased tenant is closed and released before the process exits.
    pool.close().await?;
    client.close();
    Ok(())
}
