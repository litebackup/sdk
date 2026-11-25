//! The pool adapter for sqlx's SQLite connections (feature `sqlx`). sqlx is
//! async already: a lease's work gets the [`SqliteConnection`] itself.
//!
//! ```no_run
//! # async fn example(client: &litebackup::Client) -> Result<(), Box<dyn std::error::Error>> {
//! use litebackup::integrations::sqlx::SqlxAdapter;
//! use litebackup::{LeaseOptions, PoolOptions};
//!
//! let pool = client.databases().pool(SqlxAdapter::default(), PoolOptions::default())?;
//! pool.lease("agents/42.sqlite", LeaseOptions::default().path("data/agents/42.sqlite"), async |db| {
//!     sqlx::query("INSERT INTO steps (body) VALUES (?)").bind("tests passed").execute(&mut *db).await
//! })
//! .await?;
//! # Ok(())
//! # }
//! ```

use std::path::Path;

use sqlx::sqlite::{SqliteConnectOptions, SqliteConnection};
use sqlx::{ConnectOptions as _, Connection as _, Executor as _};

use crate::adapter::{Adapter, CloseError};
use crate::error::BoxError;

/// Opens leased databases with sqlx: read-write, never creating the file.
#[derive(Debug, Clone, Default)]
pub struct SqlxAdapter {
    options: Option<SqliteConnectOptions>,
}

impl SqlxAdapter {
    /// Opens connections with these options, such as pragmas; the pool sets
    /// the file name, and the file is never created.
    #[must_use]
    pub fn options(mut self, options: SqliteConnectOptions) -> Self {
        self.options = Some(options);
        self
    }
}

/// SQLite's message when there is no transaction to roll back.
const NO_TRANSACTION: &str = "no transaction is active";

impl Adapter for SqlxAdapter {
    type Connection = SqliteConnection;

    async fn open(&self, path: &Path) -> Result<SqliteConnection, BoxError> {
        let options = self.options.clone().unwrap_or_default().filename(path).create_if_missing(false).read_only(false);
        Ok(options.connect().await?)
    }

    async fn close(&self, mut connection: SqliteConnection) -> Result<(), CloseError<SqliteConnection>> {
        // sqlx does not say whether a transaction is open: roll back, and
        // take SQLite's word when there was none.
        if let Err(error) = connection.execute("ROLLBACK").await
            && !error.to_string().contains(NO_TRANSACTION)
        {
            return Err(CloseError::retry(error, connection));
        }
        // close consumes the connection, so after a failed close nothing
        // tells whether the file is still held.
        connection.close().await.map_err(CloseError::unknown)
    }
}
