//! Back up, replicate, and restore SQLite databases with Litebackup from a
//! Rust program. The client talks to the litebackup daemon of a session over
//! its unix socket, as the `litebackup` command does, and starts the daemon
//! when it is not running, with the credential in `LITEBACKUP_API_KEY`.
//!
//! ```no_run
//! # async fn example() -> litebackup::Result<()> {
//! let client = litebackup::Client::builder().connect().await?;
//! client
//!     .backups()
//!     .start("/var/lib/app/main.sqlite", litebackup::BackupOptions::default())
//!     .await?;
//! # Ok(())
//! # }
//! ```
//!
//! Every call is async on tokio. Dropping a call's future stops waiting for
//! the daemon, which may still finish the operation. Optional settings are
//! options structs built from `Default` with setters, so new settings never
//! break a caller:
//!
//! ```no_run
//! # async fn example(client: &litebackup::Client) -> litebackup::Result<()> {
//! use std::time::Duration;
//!
//! let options = litebackup::AcquireOptions::default()
//!     .path("data/agents/42.sqlite")
//!     .lock_timeout(Duration::from_secs(30));
//! client.databases().acquire("agents/42.sqlite", options).await?;
//! # Ok(())
//! # }
//! ```
//!
//! The crate bundles no litebackup binary. It finds one through
//! `LITEBACKUP_PATH`, then `litebackup` on `PATH`, then
//! `~/.local/bin/litebackup`; install it with
//! `curl -fsSL https://litebackup.com/cli.sh | bash`.
#![deny(missing_docs, unsafe_code)]

mod adapter;
mod binary;
mod child;
mod client;
#[cfg(test)]
mod contract_tests;
mod credential;
mod environment;
mod error;
mod failover;
mod foreground;
mod generated;
mod groups;
pub mod integrations;
mod launch;
mod model_fields;
mod options;
mod paths;
mod pool;
#[cfg(feature = "release")]
pub mod release;
pub mod startup;
mod transport;
mod umask;
mod wire;

pub(crate) use generated::models;

pub use adapter::{Adapter, CloseError};
pub use client::{Client, ClientBuilder};
pub use error::{BoxError, Error, ErrorCode, LeaseCleanupError, LeaseCleanupStep, Result};
pub use failover::{Failover, FailoverNotice, FailoverNotices, FailoverOptions, Failovers};
pub use generated::models::{
    AcquireDecision, AcquireResult, ArchivedVersion, BackupRule, DatabaseState, DatabaseStatus, DatabaseStatusList,
    DrainFailure, Durability, Event, EventList, EventType, FailoverStatus, PrefixReplica, ReleaseResult, ReplicaList,
    RestorableVersion, RestoreResult, RevertResult, Role, ShutdownResult, Status, StopBackupResult, SyncedState,
    VersionList, WriteProtection, Writer,
};
pub use groups::{
    AcquireOptions, BackupOptions, Backups, Databases, Events, EventsOptions, OffloadOptions, OnceOptions, PageOptions,
    PrefixReplicaOptions, ReleaseOptions, ReplicaOptions, Replicas, RestoreOptions, RevertOptions, Rules, Session,
    ShutdownOptions, VersionsOptions, WatchOptions,
};
pub use pool::{
    DEFAULT_CLEANUP_TIMEOUT, DEFAULT_IDLE_RELEASE, DEFAULT_OFFLOAD_IDLE_CLOSE, LeaseError, LeaseOptions, Pool,
    PoolCloseError, PoolOptions, ReleaseErrorHandler,
};
/// Cancels a lease's waits; see [`LeaseOptions::cancellation`].
pub use tokio_util::sync::CancellationToken;

/// The version sent with every call, the package's own. Daemons refuse SDKs
/// older than their minimum with `sdk_unsupported`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The oldest daemon this SDK talks to. Connecting to an older one fails with
/// `daemon_unsupported`.
pub const MINIMUM_DAEMON_VERSION: &str = "1.5.0";

impl Durability {
    /// `none`: a shutdown returns without waiting for any final sync to reach
    /// the server.
    pub const NONE: Self = Self(std::borrow::Cow::Borrowed("none"));
}
