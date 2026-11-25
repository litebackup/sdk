//! The client's groups of calls: `client.backups()`, `client.databases()`,
//! and the others.

mod backups;
pub(crate) mod databases;
mod events;
mod replicas;

pub use backups::{BackupOptions, Backups, OnceOptions, Rules};
pub use databases::{
    AcquireOptions, Databases, OffloadOptions, PageOptions, ReleaseOptions, RestoreOptions, RevertOptions,
    VersionsOptions,
};
pub use events::{Events, EventsOptions, Session, ShutdownOptions, WatchOptions};
pub use replicas::{PrefixReplicaOptions, ReplicaOptions, Replicas};
