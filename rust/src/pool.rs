//! Pools: databases kept open between calls, and leased across machines.

use std::collections::HashMap;
use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, Weak};
use std::time::Duration;

use serde_json::{Map, Value};
use tokio::sync::{Mutex as TurnLock, Notify};
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

use crate::adapter::{Adapter, CloseError};
use crate::client::Client;
use crate::error::{BoxError, Error, ErrorCode, LeaseCleanupError, LeaseCleanupStep, Result};
use crate::models::{BackupRule, DatabaseState, Durability, Role};
use crate::options::option_setters;
use crate::{AcquireOptions, ReleaseOptions, wire};

/// How long closing and releasing a leased database may take together,
/// unless [`PoolOptions::cleanup_timeout`] sets it.
pub const DEFAULT_CLEANUP_TIMEOUT: Duration = Duration::from_secs(600);
/// How long a leased database stays leased unused before the pool releases
/// it, unless [`PoolOptions::idle_release`] sets it, so another machine's
/// lease with a lock timeout gets it.
pub const DEFAULT_IDLE_RELEASE: Duration = Duration::from_secs(60);
/// How long a database a backup rule offloads (`maximum_local_databases`)
/// stays open unused before the pool closes it, so the rule can offload it,
/// unless [`PoolOptions::idle_close`] sets it.
pub const DEFAULT_OFFLOAD_IDLE_CLOSE: Duration = Duration::from_secs(120);
/// How often a lease with cloud durability asks whether the bucket holds its
/// version yet.
const CLOUD_DURABILITY_POLL: Duration = Duration::from_secs(1);
/// How often a pool looks for idle databases.
const POOL_CHECK_INTERVAL: Duration = Duration::from_secs(15);
/// The journal mode the daemon reports for a rollback-journal database.
const JOURNAL_MODE_ROLLBACK: &str = "rollback";

/// Hears a close or release a pool ran on its own failed: an idle close of a
/// rule's database, or an idle release. The database stays as it was, and
/// the pool tries again at its next check and on close.
pub type ReleaseErrorHandler = Arc<dyn Fn(Error) + Send + Sync>;

/// Options of [`Databases::pool`](crate::Databases::pool).
#[derive(Clone, Default)]
#[non_exhaustive]
pub struct PoolOptions {
    /// Closes a backup rule's database after this long without a call, to
    /// cap open files. Unset keeps it open, except under a rule with
    /// `maximum_local_databases`, where it is [`DEFAULT_OFFLOAD_IDLE_CLOSE`]
    /// so the rule can offload it.
    pub idle_close: Option<Duration>,
    /// Releases a leased database after this long without a call;
    /// [`DEFAULT_IDLE_RELEASE`] when unset, and [`Duration::MAX`] keeps it
    /// until `release` or `close`. The pool never asks another machine to
    /// release: the app coordinates handovers.
    pub idle_release: Option<Duration>,
    /// Bounds closing and releasing a leased database together, however the
    /// caller cancels; [`DEFAULT_CLEANUP_TIMEOUT`] when unset. Zero is
    /// `invalid_argument`.
    pub cleanup_timeout: Option<Duration>,
    /// What other machines see as the writer of leased databases.
    pub machine_label: Option<String>,
    /// Hears each failure of a close or release the pool ran on its own.
    pub on_release_error: Option<ReleaseErrorHandler>,
}

option_setters!(PoolOptions {
    idle_close: Duration => Some(idle_close),
    idle_release: Duration => Some(idle_release),
    cleanup_timeout: Duration => Some(cleanup_timeout),
    machine_label: impl Into<String> => Some(machine_label.into()),
    on_release_error: impl Fn(Error) + Send + Sync + 'static => Some(Arc::new(on_release_error)),
});

impl fmt::Debug for PoolOptions {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("PoolOptions")
            .field("idle_close", &self.idle_close)
            .field("idle_release", &self.idle_release)
            .field("cleanup_timeout", &self.cleanup_timeout)
            .field("machine_label", &self.machine_label)
            .field("on_release_error", &self.on_release_error.as_ref().map(|_| "<set>"))
            .finish()
    }
}

/// Options of [`Pool::lease`].
#[derive(Debug, Clone, Default)]
#[non_exhaustive]
pub struct LeaseOptions {
    /// The local file. When nothing is there, the pool creates an empty file
    /// and its directory first, so a database neither the server nor this
    /// machine has becomes a new one. The path this machine already uses when
    /// unset.
    pub path: Option<PathBuf>,
    /// Skips creating `path`, so a database neither the server nor this
    /// machine has fails with `not_found`.
    pub require_existing: bool,
    /// Makes the call return only once the server, or the bucket with
    /// [`Durability::CLOUD`], holds the version its work committed, without
    /// releasing. The release that ends the lease waits for the same
    /// durability, the server when unset.
    pub durability: Option<Durability>,
    /// Removes the local file when the lease ends, once the release's
    /// durability holds the final version, unless the last call failed.
    pub offload: bool,
    /// How long the first call waits while another machine holds the write
    /// lock; unset fails at once with `lock_unavailable`.
    pub lock_timeout: Option<Duration>,
    /// Bounds downloading the head.
    pub acquire_timeout: Option<Duration>,
    /// Checkpoints on release and VACUUMs past the freelist threshold.
    pub maintenance: bool,
    /// Cancels the call's waits: cancelled before the lease starts, it sends
    /// nothing; cancelled by the time the work returns, the lease fails with
    /// `cancelled` even when the work succeeded, and keeps the lease without
    /// offload. Dropping the lease's future cancels it the same way, without
    /// the error.
    pub cancellation: Option<CancellationToken>,
}

option_setters!(LeaseOptions {
    path: impl AsRef<Path> => Some(path.as_ref().to_path_buf()),
    require_existing: bool => require_existing,
    durability: Durability => Some(durability),
    offload: bool => offload,
    lock_timeout: Duration => Some(lock_timeout),
    acquire_timeout: Duration => Some(acquire_timeout),
    maintenance: bool => maintenance,
    cancellation: CancellationToken => Some(cancellation),
});

/// Why a [`Pool::lease`] failed.
#[derive(Debug)]
#[non_exhaustive]
pub enum LeaseError<E> {
    /// The work returned this error. The pool keeps the lease and its local
    /// copy, and the release that ends it offloads nothing.
    Work(E),
    /// The adapter could not open the database. The pool released it again
    /// without offload; `release_failure` is that release's
    /// `lease_cleanup_failed` when it failed too.
    Open {
        /// The adapter's error.
        error: BoxError,
        /// The failed release, when the database stays acquired.
        release_failure: Option<Error>,
    },
    /// A Litebackup step failed: the acquire, the status read that finds a
    /// lost lock (`lock_lost`), the sync or the durability wait, a
    /// cancellation (`cancelled`), or the arguments.
    Litebackup(Error),
}

impl<E> LeaseError<E> {
    /// The Litebackup error's code, or `None` for the work's or the
    /// adapter's own error.
    #[must_use]
    pub fn code(&self) -> Option<&str> {
        match self {
            Self::Litebackup(error) => Some(error.code()),
            Self::Work(_) | Self::Open { .. } => None,
        }
    }
}

impl<E> From<Error> for LeaseError<E> {
    fn from(error: Error) -> Self {
        Self::Litebackup(error)
    }
}

impl<E: fmt::Display> fmt::Display for LeaseError<E> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Work(error) => write!(formatter, "the lease's work failed: {error}"),
            Self::Open { error, release_failure: None } => write!(formatter, "opening the database failed: {error}"),
            Self::Open { error, release_failure: Some(release) } => {
                write!(formatter, "opening the database failed: {error}; then {release}")
            }
            Self::Litebackup(error) => fmt::Display::fmt(error, formatter),
        }
    }
}

impl<E: std::error::Error + 'static> std::error::Error for LeaseError<E> {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Work(error) => Some(error),
            Self::Open { error, .. } => Some(&**error),
            Self::Litebackup(error) => Some(error),
        }
    }
}

/// The failures of [`Pool::close`], each a `lease_cleanup_failed` [`Error`]
/// whose [`lease_cleanup`](Error::lease_cleanup) names a database that
/// stays as it was.
#[derive(Debug)]
#[non_exhaustive]
pub struct PoolCloseError {
    /// Each database the pool could not close or release, in no order.
    pub errors: Vec<Error>,
}

impl fmt::Display for PoolCloseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "closing the pool left {} database(s) as they were", self.errors.len())?;
        for error in &self.errors {
            write!(formatter, "; {error}")?;
        }
        Ok(())
    }
}

impl std::error::Error for PoolCloseError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.errors.first().map(|error| error as &(dyn std::error::Error + 'static))
    }
}

/// The pool's settings with their defaults applied.
struct PoolSettings {
    idle_close: Option<Duration>,
    idle_release: Option<Duration>,
    cleanup_timeout: Duration,
    machine_label: Option<String>,
    on_release_error: Option<ReleaseErrorHandler>,
}

/// One database of a pool.
struct Entry<C> {
    name: String,
    /// Serializes acquiring, lease calls, and releasing.
    turn: TurnLock<EntryState<C>>,
    usage: Mutex<Usage>,
}

/// The calls on an entry, guarded by its usage lock.
#[derive(Default)]
struct Usage {
    /// The calls running or waiting for the turn.
    users: usize,
    /// When the last call ended.
    used: Option<Instant>,
}

/// An entry's database, guarded by its turn.
#[allow(clippy::struct_excessive_bools, reason = "each flag is a fact of its own about the entry's lease")]
struct EntryState<C> {
    connection: Option<C>,
    path: PathBuf,
    /// Set while this pool holds the write lock through a lease.
    leased: bool,
    /// Set after a close failed without handing the connection back: only an
    /// explicit release, the caller's word that every connection is closed,
    /// releases then.
    close_unconfirmed: bool,
    /// The last lease call's durability and offload, which the release uses.
    durability: Option<Durability>,
    offload: bool,
    /// Keeps the release from offloading after a call that did not finish.
    last_call_failed: bool,
    /// When a rule's database closes unused; `None` never.
    idle_close: Option<Duration>,
    /// Closes a rule's database after its last call: a rollback-journal
    /// database whose rule offloads such databases.
    close_after_use: bool,
    /// An open the pool started for a lease, which it owns apart from the
    /// call that started it: a call dropped while it opens leaves it here,
    /// and cleanup waits for it and closes what it opened before releasing.
    pending_open: Option<tokio::task::JoinHandle<Result<C, BoxError>>>,
    /// Set while the connection belongs to a lease whose lock was lost and
    /// whose close failed: no call runs on it until a close succeeds.
    lost_lease_open: bool,
}

impl<C> Default for EntryState<C> {
    fn default() -> Self {
        Self {
            connection: None,
            path: PathBuf::new(),
            leased: false,
            close_unconfirmed: false,
            durability: None,
            offload: false,
            last_call_failed: false,
            idle_close: None,
            close_after_use: false,
            pending_open: None,
            lost_lease_open: false,
        }
    }
}

/// Keeps databases open between calls, the way an app serving many tenants
/// needs them. [`Databases::pool`](crate::Databases::pool) makes one:
///
/// ```no_run
/// # #[cfg(feature = "rusqlite")]
/// # async fn example(client: &litebackup::Client) -> Result<(), Box<dyn std::error::Error>> {
/// use litebackup::{LeaseOptions, PoolOptions};
/// use litebackup::integrations::rusqlite::RusqliteAdapter;
///
/// let pool = client.databases().pool(RusqliteAdapter::default(), PoolOptions::default())?;
/// let options = LeaseOptions::default().path("data/agents/42.sqlite");
/// pool.lease("agents/42.sqlite", options, async |db| {
///     db.call(|db| db.execute("INSERT INTO steps (body) VALUES ('tests passed')", [])).await
/// })
/// .await?;
/// pool.close().await?;
/// # Ok(())
/// # }
/// ```
///
/// [`lease`](Self::lease) is for databases that move between machines: the
/// first call acquires the write lock, and the pool keeps it, with the open
/// connection, between calls until `idle_release` runs out,
/// [`release`](Self::release), or [`close`](Self::close). The app
/// coordinates handovers: another machine's lease with a lock timeout gets
/// the database once this pool released it. A call on a database whose lock
/// was lost meanwhile fails with `lock_lost` before its work, and the next
/// call acquires again. A close that fails never releases: a connection may
/// still be open, and another machine must not write while this one still
/// can. [`use_database`](Self::use_database) is for databases a backup rule
/// on this machine covers.
///
/// Calls on one database run one at a time. A pool is cheap to clone, and
/// clones share it. Call [`close`](Self::close) when done, never from inside
/// a call. Dropping the last handle without closing makes a best-effort
/// cleanup on the current runtime: each connection is closed, and a leased
/// database released without offload only after its close succeeded; with
/// no runtime, or when the runtime shuts down first, leases stay acquired.
pub struct Pool<A: Adapter> {
    shared: Arc<PoolShared<A>>,
}

impl<A: Adapter> Clone for Pool<A> {
    fn clone(&self) -> Self {
        Self { shared: Arc::clone(&self.shared) }
    }
}

impl<A: Adapter> fmt::Debug for Pool<A> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.debug_struct("Pool").field("databases", &self.shared.entries_lock().len()).finish_non_exhaustive()
    }
}

struct PoolShared<A: Adapter> {
    client: Client,
    adapter: Arc<A>,
    settings: PoolSettings,
    entries: Mutex<PoolEntries<A::Connection>>,
    rules: Mutex<HashMap<String, BackupRule>>,
    /// Counts running calls, which close waits for.
    running: AtomicUsize,
    calls_ended: Notify,
    /// Stops the idle checks.
    stop: CancellationToken,
    owner_pid: u32,
}

struct PoolEntries<C> {
    by_name: HashMap<String, Arc<Entry<C>>>,
    closed: bool,
}

impl<C> PoolEntries<C> {
    fn len(&self) -> usize {
        self.by_name.len()
    }
}

/// One running call: it counts the entry's users and the pool's running
/// calls, and ends both when dropped, however the call ends.
struct PoolCall<A: Adapter> {
    pool: Arc<PoolShared<A>>,
    entry: Arc<Entry<A::Connection>>,
    /// Set while a rule's database must close after this call, so a call
    /// that panics or is dropped still closes it.
    close_pending: bool,
}

impl<A: Adapter> Drop for PoolCall<A> {
    fn drop(&mut self) {
        {
            let mut usage = lock(&self.entry.usage);
            usage.users -= 1;
            usage.used = Some(Instant::now());
        }
        if self.close_pending
            && let Ok(runtime) = tokio::runtime::Handle::try_current()
        {
            // The call ended without its close: close in a task of its own,
            // once no other call uses the database.
            let pool = Arc::clone(&self.pool);
            let entry = Arc::clone(&self.entry);
            runtime.spawn(async move {
                let mut state = entry.turn.lock().await;
                if state.close_after_use
                    && lock(&entry.usage).users == 0
                    && let Err(error) = pool.close_rule_database(&entry.name, &mut state).await
                {
                    pool.report_release_error(error);
                }
            });
        }
        if self.pool.running.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.pool.calls_ended.notify_waiters();
        }
    }
}

/// Waits for an open the pool started, unless the token is cancelled first;
/// a cancelled wait leaves the open for cleanup.
async fn finish_open_unless_cancelled<C: Send + 'static>(
    token: Option<&CancellationToken>,
    state: &mut EntryState<C>,
) -> Result<Result<(), BoxError>> {
    match token {
        None => Ok(finish_open(state).await),
        Some(token) => tokio::select! {
            biased;
            () = token.cancelled() => Err(cancelled("the lease was cancelled while it opened the database")),
            opened = finish_open(state) => Ok(opened),
        },
    }
}

/// Waits for an open the pool started and keeps what it opened. An open that
/// panicked leaves whether the file is held unknown.
async fn finish_open<C: Send + 'static>(state: &mut EntryState<C>) -> Result<(), BoxError> {
    let Some(opening) = state.pending_open.as_mut() else {
        return Ok(());
    };
    let opened = opening.await;
    state.pending_open = None;
    match opened {
        Ok(Ok(connection)) => {
            state.connection = Some(connection);
            Ok(())
        }
        Ok(Err(error)) => Err(error),
        Err(failure) => {
            state.close_unconfirmed = true;
            Err(BoxError::from(format!("opening the database did not finish: {failure}")))
        }
    }
}

/// Locks a std mutex; a panic while it was held leaves only bookkeeping.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Checks a durability a lease waits for: server or cloud.
fn checked_lease_durability(durability: Option<&Durability>) -> Result<()> {
    match durability {
        Some(durability) if *durability != Durability::SERVER && *durability != Durability::CLOUD => {
            Err(Error::invalid_argument("a lease's durability must be server or cloud"))
        }
        _ => Ok(()),
    }
}

/// The error of a call that found no connection after opening one, which
/// opening rules out.
fn missing_connection(name: &str) -> Error {
    Error::new(ErrorCode::INVALID_RESPONSE, format!("{name} has no open connection after it was opened"))
}

fn cancelled(message: &str) -> Error {
    Error::new(ErrorCode::CANCELLED, message)
}

/// Waits for a call unless the token is cancelled first.
async fn unless_cancelled<T>(
    token: Option<&CancellationToken>,
    message: &str,
    call: impl Future<Output = Result<T>>,
) -> Result<T> {
    match token {
        None => call.await,
        Some(token) => tokio::select! {
            biased;
            () = token.cancelled() => Err(cancelled(message)),
            answer = call => answer,
        },
    }
}

impl<A: Adapter> Pool<A> {
    pub(crate) fn new(client: &Client, adapter: A, options: PoolOptions) -> Result<Self> {
        if options.cleanup_timeout.is_some_and(|timeout| timeout.is_zero()) {
            return Err(Error::invalid_argument("a pool's cleanup timeout must be positive"));
        }
        let runtime = tokio::runtime::Handle::try_current()
            .map_err(|_| Error::invalid_argument("a pool runs on a tokio runtime; make it inside one"))?;
        let settings = PoolSettings {
            idle_close: options.idle_close.filter(|idle_close| !idle_close.is_zero()),
            idle_release: match options.idle_release {
                None => Some(DEFAULT_IDLE_RELEASE),
                Some(Duration::MAX) => None,
                Some(idle_release) => Some(idle_release),
            },
            cleanup_timeout: options.cleanup_timeout.unwrap_or(DEFAULT_CLEANUP_TIMEOUT),
            machine_label: options.machine_label,
            on_release_error: options.on_release_error,
        };
        let shared = Arc::new(PoolShared {
            client: client.clone(),
            adapter: Arc::new(adapter),
            settings,
            entries: Mutex::new(PoolEntries { by_name: HashMap::new(), closed: false }),
            rules: Mutex::new(HashMap::new()),
            running: AtomicUsize::new(0),
            calls_ended: Notify::new(),
            stop: CancellationToken::new(),
            owner_pid: std::process::id(),
        });
        runtime.spawn(check_idle_databases(Arc::downgrade(&shared), shared.stop.clone()));
        Ok(Self { shared })
    }

    /// Runs `work` with a database that moves between machines, exclusively:
    /// calls on one name run one at a time, and no other machine writes
    /// while the pool keeps the lock. The first call acquires the write lock,
    /// creating an empty file at the lease's path when nothing is there;
    /// later calls reuse the lock and the open connection after checking
    /// this machine still holds it. With a durability the call returns only
    /// once the server, or the bucket, holds what the work committed. The
    /// adapters never commit for the work: commit what it should keep.
    ///
    /// A call that fails, panics, or is cancelled or dropped keeps the lease
    /// and its local copy, and the release that ends the lease offloads
    /// nothing.
    ///
    /// # Errors
    ///
    /// [`LeaseError::Work`] with the work's error, [`LeaseError::Open`] when
    /// the adapter could not open the database, and
    /// [`LeaseError::Litebackup`] for the rest: `lock_unavailable`,
    /// `lock_lost`, `cancelled`, `closed` for a closed pool, and
    /// `invalid_argument` for a database a backup rule owns.
    #[cfg_attr(feature = "tracing", tracing::instrument(name = "litebackup.lease", skip_all, fields(database = name)))]
    pub async fn lease<T, E, F>(&self, name: &str, options: LeaseOptions, work: F) -> Result<T, LeaseError<E>>
    where
        F: AsyncFnOnce(&mut A::Connection) -> Result<T, E>,
    {
        let pool = &self.shared;
        checked_lease_durability(options.durability.as_ref())?;
        let token = options.cancellation.as_ref();
        if token.is_some_and(CancellationToken::is_cancelled) {
            return Err(cancelled("the lease was cancelled before it started").into());
        }
        let call = pool.join(name)?;
        let mut state = unless_cancelled(token, "the lease was cancelled while it waited for its turn", async {
            Ok(call.entry.turn.lock().await)
        })
        .await?;
        if state.lost_lease_open {
            pool.require_lost_lease_closed(name, &mut state).await?;
        }
        if !state.leased && state.connection.is_some() {
            return Err(Error::invalid_argument(format!(
                "{name} is a backup rule's database in this pool; use use_database for it"
            ))
            .into());
        }
        // The call counts as failed until its work and its durability wait
        // both finish, so a failure, a drop, or a panic anywhere in between,
        // the status read and the open included, keeps it so.
        state.last_call_failed = true;
        state.durability.clone_from(&options.durability);
        state.offload = options.offload;
        if state.leased {
            pool.require_lease(&call.entry.name, &mut state, token).await?;
        } else {
            pool.acquire_lease(&call.entry.name, &mut state, &options).await?;
        }
        if token.is_some_and(CancellationToken::is_cancelled) {
            return Err(cancelled("the lease was cancelled before its work started").into());
        }
        let Some(connection) = state.connection.as_mut() else {
            return Err(missing_connection(name).into());
        };
        let worked = work(connection).await;
        let value = match worked {
            Err(error) => return Err(LeaseError::Work(error)),
            Ok(_) if token.is_some_and(CancellationToken::is_cancelled) => {
                return Err(cancelled("the lease was cancelled while its work ran").into());
            }
            Ok(value) => value,
        };
        if let Some(durability) = &options.durability {
            pool.wait_durable(name, durability, token).await?;
        }
        state.last_call_failed = false;
        Ok(value)
    }

    /// Runs `work` with a database a backup rule on this machine covers. The
    /// pool opens it on first use and keeps it open; an offloaded copy is
    /// brought back first, never recreated empty. A name no rule on this
    /// machine covers is refused with `invalid_argument`; lease it instead.
    ///
    /// # Errors
    ///
    /// [`LeaseError::Work`] with the work's error, [`LeaseError::Open`] when
    /// the adapter could not open the database, and
    /// [`LeaseError::Litebackup`] for the rest.
    #[cfg_attr(feature = "tracing", tracing::instrument(name = "litebackup.use_database", skip_all, fields(database = name)))]
    pub async fn use_database<T, E, F>(&self, name: &str, work: F) -> Result<T, LeaseError<E>>
    where
        F: AsyncFnOnce(&mut A::Connection) -> Result<T, E>,
    {
        let pool = &self.shared;
        let mut call = pool.join(name)?;
        let mut state = call.entry.turn.lock().await;
        if state.lost_lease_open {
            pool.require_lost_lease_closed(name, &mut state).await?;
        }
        if state.leased || state.pending_open.is_some() {
            return Err(Error::invalid_argument(format!("{name} is leased in this pool; lease it")).into());
        }
        if state.close_after_use && state.connection.is_some() {
            // A call that ended without its close left the connection: close
            // it, and open again after the status read, which brings back a
            // copy offloaded meanwhile.
            pool.close_rule_database(name, &mut state).await?;
        }
        if state.connection.is_none() {
            pool.open_rule_database(name, &mut state).await?;
        }
        // Rule offload takes such a database on the word that nothing keeps
        // it open while idle, which no lock shows, so the pool keeps that
        // word, even after a call that panics or is dropped.
        call.close_pending = state.close_after_use;
        let Some(connection) = state.connection.as_mut() else {
            return Err(missing_connection(name).into());
        };
        let worked = work(connection).await.map_err(LeaseError::Work);
        if state.close_after_use && lock(&call.entry.usage).users == 1 {
            call.close_pending = false;
            if let Err(error) = pool.close_rule_database(&call.entry.name, &mut state).await {
                pool.report_release_error(error);
            }
        }
        worked
    }

    /// Ends the lease of a database: closes its connection and releases its
    /// write lock once the final version reaches the lease's durability,
    /// after a running call returns. The cleanup runs to its end even when
    /// this future is dropped.
    ///
    /// # Errors
    ///
    /// `lease_cleanup_failed` when closing or releasing failed, with
    /// [`Error::lease_cleanup`] naming the step, and `invalid_argument` for
    /// a database the pool does not lease.
    #[cfg_attr(feature = "tracing", tracing::instrument(name = "litebackup.release", skip_all, fields(database = name)))]
    pub async fn release(&self, name: &str) -> Result<()> {
        self.shared.deny_forked_child()?;
        let Some(entry) = self.shared.entries_lock().by_name.get(name).cloned() else {
            return Err(Error::invalid_argument(format!("{name} is not leased in this pool")));
        };
        let pool = Arc::clone(&self.shared);
        let cleanup = tokio::spawn(async move {
            let mut state = entry.turn.lock().await;
            if !state.leased {
                return Err(Error::invalid_argument(format!("{} is not leased in this pool", entry.name)));
            }
            pool.release_lease(&entry, &mut state, true).await
        });
        cleanup.await.unwrap_or_else(|failure| std::panic::resume_unwind(failure.into_panic()))
    }

    /// Refuses new calls, waits for running ones, then releases every leased
    /// database and closes every open one. The cleanup runs to its end even
    /// when this future is dropped. A second close does nothing. Never call
    /// it from inside a call, which it would wait for.
    ///
    /// # Errors
    ///
    /// [`PoolCloseError`] with each database that stays as it was.
    #[cfg_attr(feature = "tracing", tracing::instrument(name = "litebackup.pool_close", skip_all))]
    pub async fn close(&self) -> Result<(), PoolCloseError> {
        if let Err(error) = self.shared.deny_forked_child() {
            return Err(PoolCloseError { errors: vec![error] });
        }
        {
            let mut entries = self.shared.entries_lock();
            if entries.closed {
                return Ok(());
            }
            entries.closed = true;
        }
        self.shared.stop.cancel();
        let pool = Arc::clone(&self.shared);
        let cleanup = tokio::spawn(async move { pool.close_entries().await });
        let errors = cleanup.await.unwrap_or_else(|failure| std::panic::resume_unwind(failure.into_panic()));
        if errors.is_empty() { Ok(()) } else { Err(PoolCloseError { errors }) }
    }
}

impl<A: Adapter> PoolShared<A> {
    fn entries_lock(&self) -> MutexGuard<'_, PoolEntries<A::Connection>> {
        lock(&self.entries)
    }

    /// Fails with `invalid_argument` in a forked child, before anything
    /// touches the pool's locks or connections, which the parent owns.
    fn deny_forked_child(&self) -> Result<()> {
        if self.owner_pid == std::process::id() {
            return Ok(());
        }
        Err(Error::invalid_argument(format!(
            "this pool was made in process {} and cannot be used after a fork; make a new pool in this process",
            self.owner_pid
        )))
    }

    /// Counts a call on the name's entry, creating it.
    fn join(self: &Arc<Self>, name: &str) -> Result<PoolCall<A>> {
        if name.is_empty() {
            return Err(Error::invalid_argument("a pool call needs a database name"));
        }
        self.deny_forked_child()?;
        let mut entries = self.entries_lock();
        if entries.closed {
            return Err(Error::new(ErrorCode::CLOSED, "the pool is closed"));
        }
        let entry = Arc::clone(entries.by_name.entry(name.to_owned()).or_insert_with(|| {
            Arc::new(Entry {
                name: name.to_owned(),
                turn: TurnLock::new(EntryState::default()),
                usage: Mutex::default(),
            })
        }));
        lock(&entry.usage).users += 1;
        self.running.fetch_add(1, Ordering::AcqRel);
        Ok(PoolCall { pool: Arc::clone(self), entry, close_pending: false })
    }

    /// Forgets an entry nothing uses that holds no lease or connection.
    fn remove_unused(&self, entry_name: &str) {
        let mut entries = self.entries_lock();
        if let Some(entry) = entries.by_name.get(entry_name)
            && lock(&entry.usage).users == 0
        {
            entries.by_name.remove(entry_name);
        }
    }

    /// Acquires the write lock and opens the database; a database that opened
    /// nothing is released again at once, without offload.
    async fn acquire_lease<E>(
        &self,
        name: &str,
        state: &mut EntryState<A::Connection>,
        options: &LeaseOptions,
    ) -> Result<(), LeaseError<E>> {
        let path = wire::supplied(options.path.as_deref());
        if let Some(path) = path.filter(|_| !options.require_existing) {
            create_empty_file(path)?;
        }
        let acquire_options = AcquireOptions {
            path: path.map(Path::to_path_buf),
            maintenance: options.maintenance,
            machine_label: self.settings.machine_label.clone(),
            timeout: options.acquire_timeout,
            lock_timeout: options.lock_timeout,
            ..AcquireOptions::default()
        };
        let acquired = unless_cancelled(
            options.cancellation.as_ref(),
            "the lease was cancelled while it acquired the database; the daemon may still take the lock",
            self.client.databases().acquire(name, acquire_options),
        )
        .await?;
        if !acquired.database.acquired {
            return Err(Error::invalid_argument(format!(
                "{name} is a backup rule's database on this machine; use use_database for it"
            ))
            .into());
        }
        state.path = if acquired.database.path.is_empty() {
            PathBuf::from(wire::absolute_path(path.unwrap_or_else(|| Path::new("")))?)
        } else {
            PathBuf::from(&acquired.database.path)
        };
        // Leased before the open, which the pool owns, so a call dropped
        // while it opens leaves both for cleanup: it waits for the open and
        // closes what it opened before it releases.
        state.leased = true;
        state.last_call_failed = true;
        self.start_open(state);
        match finish_open_unless_cancelled(options.cancellation.as_ref(), state).await? {
            Ok(()) => Ok(()),
            Err(error) => {
                let release_failure = self.release_lease_named(name, state, false).await.err();
                Err(LeaseError::Open { error, release_failure })
            }
        }
    }

    /// Starts opening the database at the entry's path in a task the entry
    /// owns, unless an open is already pending.
    fn start_open(&self, state: &mut EntryState<A::Connection>) {
        if state.pending_open.is_none() {
            let adapter = Arc::clone(&self.adapter);
            let path = state.path.clone();
            state.pending_open = Some(tokio::spawn(async move { adapter.open(&path).await }));
        }
    }

    /// Closes the connection of a lease whose lock was lost before a call
    /// uses the entry; while that close fails, the call fails with it.
    async fn require_lost_lease_closed(&self, name: &str, state: &mut EntryState<A::Connection>) -> Result<()> {
        self.drop_lost_lease(state).await;
        if state.lost_lease_open {
            return Err(close_failure(
                name,
                &state.path,
                false,
                BoxError::from("closing the connection of a lost lease failed again"),
            ));
        }
        Ok(())
    }

    /// Checks that this machine still holds a leased database's lock, and
    /// opens a connection a failed release closed. A lost lock drops the
    /// lease and fails with `lock_lost`.
    async fn require_lease<E>(
        &self,
        name: &str,
        state: &mut EntryState<A::Connection>,
        token: Option<&CancellationToken>,
    ) -> Result<(), LeaseError<E>> {
        let message = "the lease was cancelled while it read the database's status";
        let status = unless_cancelled(token, message, self.client.databases().get(name)).await?;
        if !status.acquired || status.state == DatabaseState::CONFLICTED {
            self.drop_lost_lease(state).await;
            return Err(Error::new(
                ErrorCode::LOCK_LOST,
                format!(
                    "this machine lost the write lock of {name}, so its copy is {}; the next lease acquires it again",
                    status.state
                ),
            )
            .into());
        }
        if state.connection.is_none() {
            self.start_open(state);
            finish_open_unless_cancelled(token, state)
                .await?
                .map_err(|error| LeaseError::Open { error, release_failure: None })?;
        }
        Ok(())
    }

    /// Closes the connection of a database whose lock was lost and forgets
    /// the lease; the copy stays conflicted, its file as it was.
    async fn drop_lost_lease(&self, state: &mut EntryState<A::Connection>) {
        let _ = finish_open(state).await;
        state.lost_lease_open = false;
        if let Some(connection) = state.connection.take() {
            let closing = self.adapter.close(connection);
            if let Ok(Err(CloseError { connection: Some(connection), .. })) =
                tokio::time::timeout(self.settings.cleanup_timeout, closing).await
            {
                // Still open: the next call closes it before anything runs.
                state.connection = Some(connection);
                state.lost_lease_open = true;
            }
        }
        state.leased = false;
    }

    /// Syncs the database now and returns once the server, or the bucket
    /// with cloud durability, holds the version the sync reached.
    async fn wait_durable(&self, name: &str, durability: &Durability, token: Option<&CancellationToken>) -> Result<()> {
        let message = "the lease was cancelled while it waited for its durability";
        let mut status = unless_cancelled(token, message, self.client.databases().sync(name)).await?;
        if *durability != Durability::CLOUD {
            return Ok(());
        }
        let target = status.synced_server.version;
        while status.synced_cloud.version < target {
            unless_cancelled(token, message, async {
                tokio::time::sleep(CLOUD_DURABILITY_POLL).await;
                Ok(())
            })
            .await?;
            status = unless_cancelled(token, message, self.client.databases().get(name)).await?;
        }
        Ok(())
    }

    async fn release_lease(
        &self,
        entry: &Entry<A::Connection>,
        state: &mut EntryState<A::Connection>,
        explicit: bool,
    ) -> Result<()> {
        self.release_lease_named(&entry.name, state, explicit).await?;
        self.remove_unused(&entry.name);
        Ok(())
    }

    /// Closes the connection and releases the lock, within one cleanup
    /// timeout. A close that fails never releases; after one that could not
    /// hand the connection back, only an explicit release does.
    async fn release_lease_named(
        &self,
        name: &str,
        state: &mut EntryState<A::Connection>,
        explicit: bool,
    ) -> Result<()> {
        let deadline = Instant::now() + self.settings.cleanup_timeout;
        // An open a dropped call left: what it opened is closed first. One that
        // failed opened nothing; one that did not finish leaves the file's
        // state unknown, which only an explicit release overrides.
        let _ = finish_open(state).await;
        if let Some(connection) = state.connection.take() {
            let failure = match tokio::time::timeout_at(deadline, self.adapter.close(connection)).await {
                Ok(Ok(())) => None,
                Ok(Err(CloseError { error, connection })) => {
                    match connection {
                        Some(connection) => state.connection = Some(connection),
                        None => state.close_unconfirmed = true,
                    }
                    Some(error)
                }
                Err(_) => {
                    state.close_unconfirmed = true;
                    Some(BoxError::from("closing did not finish within the cleanup timeout"))
                }
            };
            if let Some(error) = failure {
                return Err(close_failure(name, &state.path, true, error));
            }
        }
        if state.close_unconfirmed && !explicit {
            return Err(close_failure(
                name,
                &state.path,
                true,
                BoxError::from("an earlier close could not tell whether the file is still open"),
            ));
        }
        let durability = state.durability.clone().unwrap_or(Durability::SERVER);
        let remaining = deadline.saturating_duration_since(Instant::now());
        let timeout = rounded_millis(remaining);
        let options = ReleaseOptions {
            durability: Some(durability.clone()),
            offload: state.offload && !state.last_call_failed,
            timeout: Some(timeout),
        };
        if let Err(error) = self.client.databases().release(name, options).await {
            return Err(release_failure(name, &state.path, &durability, error));
        }
        state.leased = false;
        state.close_unconfirmed = false;
        Ok(())
    }

    /// Opens a rule's database, bringing an offloaded copy back first, and
    /// tries once more when the copy was offloaded between the two.
    async fn open_rule_database<E>(
        &self,
        name: &str,
        state: &mut EntryState<A::Connection>,
    ) -> Result<(), LeaseError<E>> {
        let mut attempt = 0;
        loop {
            attempt += 1;
            let status = self.client.databases().get(name).await?;
            let rule_id = status.backup_rule_id.clone().filter(|rule_id| !rule_id.is_empty());
            let Some(rule_id) = rule_id.filter(|_| status.role == Role::ORIGIN && !status.acquired && !status.failover)
            else {
                return Err(Error::invalid_argument(format!(
                    "{name} is not a backup rule's database on this machine; lease a database that moves between machines"
                ))
                .into());
            };
            if status.state == DatabaseState::OFFLOADED {
                self.client.databases().acquire(name, AcquireOptions::default()).await?;
            }
            let path = PathBuf::from(&status.path);
            match self.adapter.open(&path).await {
                Ok(connection) => {
                    state.connection = Some(connection);
                    state.path = path;
                }
                Err(_) if attempt == 1 && !status.path.is_empty() && !Path::new(&status.path).exists() => continue,
                Err(error) => return Err(LeaseError::Open { error, release_failure: None }),
            }
            let rule = self.backup_rule(&rule_id).await?;
            state.idle_close = self.settings.idle_close.or_else(|| {
                rule.as_ref().filter(|rule| rule.maximum_local_databases > 0).map(|_| DEFAULT_OFFLOAD_IDLE_CLOSE)
            });
            state.close_after_use = status.journal_mode == JOURNAL_MODE_ROLLBACK
                && rule.as_ref().is_some_and(|rule| rule.offload_rollback_journal);
            return Ok(());
        }
    }

    /// A rule of this machine, listing the rules again when it is not known yet.
    async fn backup_rule(&self, rule_id: &str) -> Result<Option<BackupRule>> {
        if let Some(rule) = lock(&self.rules).get(rule_id) {
            return Ok(Some(rule.clone()));
        }
        let rules = self.client.rules().list().await?;
        let mut known = lock(&self.rules);
        for rule in rules {
            known.insert(rule.id.clone(), rule);
        }
        Ok(known.get(rule_id).cloned())
    }

    /// Closes a rule's database; a failed close keeps the connection for the
    /// next check and close to try again.
    async fn close_rule_database(&self, name: &str, state: &mut EntryState<A::Connection>) -> Result<()> {
        let Some(connection) = state.connection.take() else {
            return Ok(());
        };
        let error = match tokio::time::timeout(self.settings.cleanup_timeout, self.adapter.close(connection)).await {
            Ok(Ok(())) => {
                self.remove_unused(name);
                return Ok(());
            }
            Ok(Err(CloseError { error, connection })) => {
                state.connection = connection;
                error
            }
            Err(_) => BoxError::from("closing did not finish within the cleanup timeout"),
        };
        Err(close_failure(name, &state.path, false, error))
    }

    /// Waits for the running calls, then releases and closes every entry.
    async fn close_entries(&self) -> Vec<Error> {
        loop {
            let ended = self.calls_ended.notified();
            if self.running.load(Ordering::Acquire) == 0 {
                break;
            }
            ended.await;
        }
        let entries: Vec<Arc<Entry<A::Connection>>> = self.entries_lock().by_name.values().cloned().collect();
        let mut errors = Vec::new();
        for entry in entries {
            let mut state = entry.turn.lock().await;
            let closed = if state.leased {
                self.release_lease(&entry, &mut state, false).await
            } else {
                self.close_rule_database(&entry.name, &mut state).await
            };
            errors.extend(closed.err());
        }
        errors
    }

    /// Hands a failed close or release the pool ran on its own to
    /// `on_release_error`.
    fn report_release_error(&self, error: Error) {
        match &self.settings.on_release_error {
            Some(handler) => handler(error),
            None => {
                #[cfg(feature = "tracing")]
                tracing::warn!(%error, "a litebackup pool's own close or release failed; it tries again");
                #[cfg(not(feature = "tracing"))]
                drop(error);
            }
        }
    }
}

impl<A: Adapter> Drop for PoolShared<A> {
    fn drop(&mut self) {
        // A forked child's copy of the pool touches none of the parent's
        // locks or connections, and never releases its leases.
        if self.owner_pid != std::process::id() {
            return;
        }
        self.stop.cancel();
        let entries = {
            let mut entries = self.entries_lock();
            if entries.closed {
                return;
            }
            std::mem::take(&mut entries.by_name)
        };
        if entries.is_empty() {
            return;
        }
        let Ok(runtime) = tokio::runtime::Handle::try_current() else {
            #[cfg(feature = "tracing")]
            tracing::warn!(
                databases = entries.len(),
                "a litebackup pool was dropped without close outside a tokio runtime; its leases stay acquired"
            );
            return;
        };
        let client = self.client.clone();
        let adapter = Arc::clone(&self.adapter);
        let cleanup_timeout = self.settings.cleanup_timeout;
        runtime.spawn(async move {
            for (name, entry) in entries {
                let mut state = entry.turn.lock().await;
                release_dropped(&client, &*adapter, cleanup_timeout, &name, &mut state).await;
            }
        });
    }
}

/// The best-effort cleanup of a pool dropped without close: closes the
/// connection, and releases a leased database without offload only after its
/// close succeeded.
async fn release_dropped<A: Adapter>(
    client: &Client,
    adapter: &A,
    cleanup_timeout: Duration,
    name: &str,
    state: &mut EntryState<A::Connection>,
) {
    let deadline = Instant::now() + cleanup_timeout;
    let _ = finish_open(state).await;
    if state.lost_lease_open {
        return;
    }
    if let Some(connection) = state.connection.take()
        && !matches!(tokio::time::timeout_at(deadline, adapter.close(connection)).await, Ok(Ok(())))
    {
        return;
    }
    if !state.leased || state.close_unconfirmed {
        return;
    }
    let options = ReleaseOptions {
        durability: Some(state.durability.clone().unwrap_or(Durability::SERVER)),
        offload: false,
        timeout: Some(rounded_millis(deadline.saturating_duration_since(Instant::now()))),
    };
    let released = client.databases().release(name, options).await;
    #[cfg(feature = "tracing")]
    if let Err(error) = &released {
        tracing::warn!(database = name, %error, "releasing a dropped pool's lease failed; it stays acquired");
    }
    #[cfg(not(feature = "tracing"))]
    let _ = released;
}

/// Closes rule databases past their idle close and releases leases past
/// their idle release, until the pool closes or is dropped.
async fn check_idle_databases<A: Adapter>(pool_reference: Weak<PoolShared<A>>, stop: CancellationToken) {
    loop {
        tokio::select! {
            () = stop.cancelled() => return,
            () = tokio::time::sleep(POOL_CHECK_INTERVAL) => {}
        }
        let Some(pool) = pool_reference.upgrade() else {
            return;
        };
        let now = Instant::now();
        let idle: Vec<(Arc<Entry<A::Connection>>, Duration)> = pool
            .entries_lock()
            .by_name
            .values()
            .filter_map(|entry| {
                let usage = lock(&entry.usage);
                let used = usage.used.filter(|_| usage.users == 0)?;
                Some((Arc::clone(entry), now.saturating_duration_since(used)))
            })
            .collect();
        for (entry, idle_time) in idle {
            let Ok(mut state) = entry.turn.try_lock() else {
                continue;
            };
            if lock(&entry.usage).users > 0 {
                continue;
            }
            let checked = if state.leased {
                match pool.settings.idle_release {
                    Some(idle_release) if idle_time >= idle_release => {
                        pool.release_lease(&entry, &mut state, false).await
                    }
                    _ => Ok(()),
                }
            } else if state.connection.is_some() && state.idle_close.is_some_and(|idle_close| idle_time >= idle_close) {
                pool.close_rule_database(&entry.name, &mut state).await
            } else {
                Ok(())
            };
            drop(state);
            if let Err(error) = checked {
                pool.report_release_error(error);
            }
        }
    }
}

/// A `lease_cleanup_failed` error for a failed close; `acquired` says the
/// database stays acquired by this machine.
fn close_failure(name: &str, path: &Path, acquired: bool, cause: BoxError) -> Error {
    let message = if acquired {
        format!(
            "closing {name} failed, so it was not released and stays acquired by this machine: {cause}. \
             Close every connection to {}, then call release again",
            path.display()
        )
    } else {
        format!("closing {name} failed; the pool tries again: {cause}")
    };
    let mut details = Map::new();
    details.insert("step".to_owned(), Value::from(LeaseCleanupStep::Close.as_str()));
    details.insert("name".to_owned(), Value::from(name));
    details.insert("path".to_owned(), Value::from(path.to_string_lossy().into_owned()));
    details.insert("acquired".to_owned(), Value::Bool(acquired));
    cleanup_error(message, details, LeaseCleanupStep::Close, name, path).with_source(cause)
}

/// A `lease_cleanup_failed` error for a failed release.
fn release_failure(name: &str, path: &Path, durability: &Durability, cause: Error) -> Error {
    let message = format!(
        "releasing {name} failed: {cause}. Its file at {} is intact and this machine may still hold its write lock; \
         call release again",
        path.display()
    );
    let mut details = Map::new();
    details.insert("step".to_owned(), Value::from(LeaseCleanupStep::Release.as_str()));
    details.insert("name".to_owned(), Value::from(name));
    details.insert("path".to_owned(), Value::from(path.to_string_lossy().into_owned()));
    details.insert("durability".to_owned(), Value::from(durability.as_str()));
    cleanup_error(message, details, LeaseCleanupStep::Release, name, path).with_source(cause)
}

fn cleanup_error(
    message: String,
    details: Map<String, Value>,
    step: LeaseCleanupStep,
    name: &str,
    path: &Path,
) -> Error {
    Error::new(ErrorCode::LEASE_CLEANUP_FAILED, message).with_details(details).with_lease_cleanup(LeaseCleanupError {
        step,
        name: name.to_owned(),
        path: path.to_path_buf(),
    })
}

/// A duration rounded to whole milliseconds, at least one: what close left of
/// the cleanup timeout, as the release's `timeout_millis`.
fn rounded_millis(duration: Duration) -> Duration {
    let millis = duration.as_micros().saturating_add(500) / 1000;
    Duration::from_millis(u64::try_from(millis).unwrap_or(u64::MAX).max(1))
}

/// Creates an empty file and its directory unless something is already at path.
fn create_empty_file(path: &Path) -> Result<()> {
    let absolute = PathBuf::from(wire::absolute_path(path)?);
    let creating_failed =
        |error: std::io::Error| Error::invalid_argument(format!("creating {} failed: {error}", absolute.display()));
    if let Some(directory) = absolute.parent() {
        std::fs::create_dir_all(directory).map_err(creating_failed)?;
    }
    match std::fs::OpenOptions::new().write(true).create_new(true).open(&absolute) {
        Ok(_) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(()),
        Err(error) => Err(creating_failed(error)),
    }
}
