//! The pool adapter for rusqlite (feature `rusqlite`).
//!
//! rusqlite blocks while SQLite works, so each connection lives on a thread
//! of its own: [`RusqliteConnection::call`] runs a closure there and awaits
//! its result, and the async runtime is never blocked, whatever its flavor.
//!
//! ```no_run
//! # async fn example(client: &litebackup::Client) -> Result<(), Box<dyn std::error::Error>> {
//! use litebackup::integrations::rusqlite::RusqliteAdapter;
//! use litebackup::{LeaseOptions, PoolOptions};
//!
//! let pool = client.databases().pool(RusqliteAdapter::default(), PoolOptions::default())?;
//! let steps = pool
//!     .lease("agents/42.sqlite", LeaseOptions::default().path("data/agents/42.sqlite"), async |db| {
//!         db.call(|db| {
//!             db.execute("INSERT INTO steps (body) VALUES (?1)", ["tests passed"])?;
//!             db.query_row("SELECT count(*) FROM steps", [], |columns| columns.get::<_, i64>(0))
//!         })
//!         .await
//!     })
//!     .await?;
//! # Ok(())
//! # }
//! ```

use std::fmt;
use std::path::Path;
use std::sync::Arc;
use std::sync::mpsc as thread_channel;

use rusqlite::{Connection, OpenFlags};
use tokio::sync::oneshot;

use crate::adapter::{Adapter, CloseError};
use crate::error::BoxError;

/// Runs once on each connection after it opens, as for pragmas.
pub type ConnectionSetup = Arc<dyn Fn(&mut Connection) -> rusqlite::Result<()> + Send + Sync>;

/// Opens leased databases with rusqlite: read-write, never creating the
/// file, each connection on a thread of its own.
#[derive(Clone, Default)]
pub struct RusqliteAdapter {
    setup: Option<ConnectionSetup>,
}

impl fmt::Debug for RusqliteAdapter {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.debug_struct("RusqliteAdapter").field("setup", &self.setup.as_ref().map(|_| "<set>")).finish()
    }
}

impl RusqliteAdapter {
    /// Runs `setup` on each connection after it opens, such as
    /// `|db| db.pragma_update(None, "foreign_keys", true)`; a setup that fails
    /// fails the open.
    #[must_use]
    pub fn setup(mut self, setup: impl Fn(&mut Connection) -> rusqlite::Result<()> + Send + Sync + 'static) -> Self {
        self.setup = Some(Arc::new(setup));
        self
    }
}

/// What the connection's thread runs: a closure, or the close.
enum Request {
    Call(Box<dyn FnOnce(&mut Connection) + Send>),
    Close(oneshot::Sender<rusqlite::Result<()>>),
}

/// A rusqlite connection on a thread of its own; what a lease's work gets.
#[derive(Debug)]
pub struct RusqliteConnection {
    requests: thread_channel::Sender<Request>,
}

impl RusqliteConnection {
    /// Runs `work` with the connection on its thread and returns its result.
    /// The work runs to its end even when this future is dropped; the
    /// pool's close waits for it. A panic in the work continues on the
    /// caller's task.
    ///
    /// # Errors
    ///
    /// The work's own error, or `SQLITE_MISUSE` once the connection closed.
    pub async fn call<T, F>(&self, work: F) -> rusqlite::Result<T>
    where
        F: FnOnce(&mut Connection) -> rusqlite::Result<T> + Send + 'static,
        T: Send + 'static,
    {
        let (answer, answered) = oneshot::channel();
        let request = Request::Call(Box::new(move |connection| {
            // The panic is handed to the caller, so the thread and its
            // connection outlive it; the pool's close rolls back what it left.
            let worked = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| work(connection)));
            let _ = answer.send(worked);
        }));
        if self.requests.send(request).is_err() {
            return Err(closed_connection());
        }
        match answered.await {
            Ok(Ok(worked)) => worked,
            Ok(Err(panic)) => std::panic::resume_unwind(panic),
            Err(_) => Err(closed_connection()),
        }
    }
}

/// The error of a call on a connection that closed.
fn closed_connection() -> rusqlite::Error {
    rusqlite::Error::SqliteFailure(
        rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_MISUSE),
        Some("the connection is closed".to_owned()),
    )
}

/// Serves a connection's requests on its thread until it closes.
fn serve(mut connection: Connection, requests: &thread_channel::Receiver<Request>) {
    while let Ok(request) = requests.recv() {
        match request {
            Request::Call(work) => work(&mut connection),
            Request::Close(answer) => {
                let rolled_back =
                    if connection.is_autocommit() { Ok(()) } else { connection.execute_batch("ROLLBACK") };
                if let Err(error) = rolled_back {
                    let _ = answer.send(Err(error));
                    continue;
                }
                match connection.close() {
                    Ok(()) => {
                        let _ = answer.send(Ok(()));
                        return;
                    }
                    Err((still_open, error)) => {
                        connection = still_open;
                        let _ = answer.send(Err(error));
                    }
                }
            }
        }
    }
}

impl Adapter for RusqliteAdapter {
    type Connection = RusqliteConnection;

    async fn open(&self, path: &Path) -> Result<RusqliteConnection, BoxError> {
        let path = path.to_path_buf();
        let setup = self.setup.clone();
        let (opened_answer, opened) = oneshot::channel();
        let (requests, received) = thread_channel::channel();
        std::thread::Builder::new().name("litebackup-sqlite".to_owned()).spawn(move || {
            let flags = OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX;
            let opening = Connection::open_with_flags(&path, flags).and_then(|mut connection| {
                if let Some(setup) = &setup {
                    setup(&mut connection)?;
                }
                Ok(connection)
            });
            match opening {
                Ok(connection) => {
                    let _ = opened_answer.send(Ok(()));
                    serve(connection, &received);
                }
                Err(error) => {
                    let _ = opened_answer.send(Err(error));
                }
            }
        })?;
        opened.await.map_err(|_| BoxError::from("the connection's thread ended before it opened"))??;
        Ok(RusqliteConnection { requests })
    }

    async fn close(&self, connection: RusqliteConnection) -> Result<(), CloseError<RusqliteConnection>> {
        let (answer, answered) = oneshot::channel();
        if connection.requests.send(Request::Close(answer)).is_err() {
            return Err(CloseError::unknown("the connection's thread ended"));
        }
        match answered.await {
            Ok(Ok(())) => Ok(()),
            Ok(Err(error)) => Err(CloseError::retry(error, connection)),
            Err(_) => Err(CloseError::unknown("the connection's thread ended while it closed")),
        }
    }
}
