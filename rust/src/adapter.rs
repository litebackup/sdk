//! How a pool opens and closes the databases it leases.

use std::fmt;
use std::future::Future;
use std::path::Path;

use crate::error::BoxError;

/// Opens and closes one kind of database connection for a
/// [`Pool`](crate::Pool). The crate has adapters for rusqlite (feature
/// `rusqlite`) and sqlx (feature `sqlx`); implement it for another driver:
///
/// ```
/// use std::path::Path;
///
/// struct Files;
///
/// impl litebackup::Adapter for Files {
///     type Connection = std::fs::File;
///
///     async fn open(&self, path: &Path) -> Result<std::fs::File, litebackup::BoxError> {
///         // Open the existing file; never create one.
///         Ok(std::fs::OpenOptions::new().read(true).write(true).open(path)?)
///     }
///
///     async fn close(&self, file: std::fs::File) -> Result<(), litebackup::CloseError<std::fs::File>> {
///         drop(file);
///         Ok(())
///     }
/// }
/// ```
pub trait Adapter: Send + Sync + 'static {
    /// What a lease's work gets, as `&mut`.
    type Connection: Send + 'static;

    /// Opens the existing database file at `path`, never creating one.
    fn open(&self, path: &Path) -> impl Future<Output = Result<Self::Connection, BoxError>> + Send;

    /// Rolls back an unfinished transaction, never committing, then closes
    /// the connection. Succeeding means nothing this connection opened still
    /// holds the file, because the pool releases the database next and
    /// another machine may then write it. On failure, hand the connection
    /// back in [`CloseError::connection`] so the pool can try again; without
    /// it, the pool releases only when the caller asks it to.
    fn close(
        &self,
        connection: Self::Connection,
    ) -> impl Future<Output = Result<(), CloseError<Self::Connection>>> + Send;
}

/// A failed [`Adapter::close`]: why, and the connection when it is still
/// whole, for the pool to close again later.
#[non_exhaustive]
pub struct CloseError<C> {
    /// Why the close failed.
    pub error: BoxError,
    /// The connection, still open, or `None` when the adapter cannot tell
    /// whether the file is still held.
    pub connection: Option<C>,
}

impl<C> CloseError<C> {
    /// A failed close that hands the connection back.
    pub fn retry(error: impl Into<BoxError>, connection: C) -> Self {
        Self { error: error.into(), connection: Some(connection) }
    }

    /// A failed close that leaves the connection's state unknown.
    pub fn unknown(error: impl Into<BoxError>) -> Self {
        Self { error: error.into(), connection: None }
    }
}

impl<C> fmt::Debug for CloseError<C> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CloseError")
            .field("error", &self.error)
            .field("connection_returned", &self.connection.is_some())
            .finish()
    }
}

impl<C> fmt::Display for CloseError<C> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "closing the connection failed: {}", self.error)
    }
}

impl<C> std::error::Error for CloseError<C> {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&*self.error)
    }
}
