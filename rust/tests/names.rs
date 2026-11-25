//! The names `../testdata/contract/names/sdk-names.json` lists for every SDK:
//! each operation `group.method` is `client.group().method(..)` in Rust, as
//! Python and Ruby spell it, and each shared type is exported under its name.
#![allow(clippy::expect_used, clippy::unwrap_used, reason = "tests assert with expect")]

mod support;

use std::collections::BTreeSet;

// Every shared type, imported by name: a missing one fails to compile.
use litebackup::{
    AcquireOptions, BackupOptions, Client, EventsOptions, FailoverOptions, OffloadOptions, OnceOptions, PageOptions,
    PoolOptions, PrefixReplicaOptions, ReleaseOptions, ReplicaOptions, RestoreOptions, RevertOptions, ShutdownOptions,
    VersionsOptions, WatchOptions,
};
#[allow(unused_imports, reason = "the import itself is the check")]
use litebackup::{
    AcquireResult, Adapter, ArchivedVersion, BackupRule, DatabaseStatus, DatabaseStatusList, DrainFailure, Durability,
    Event, EventList, LeaseCleanupError, Pool, PrefixReplica, ReleaseResult, ReplicaList, RestorableVersion,
    RestoreResult, RevertResult, ShutdownResult, Status, StopBackupResult, SyncedState, VersionList, WriteProtection,
    Writer,
};
use support::fixture_root;

/// The types imported above, as the names file spells them.
const SHARED_TYPES: [&str; 25] = [
    "AcquireResult",
    "Adapter",
    "ArchivedVersion",
    "BackupRule",
    "DatabaseStatus",
    "DatabaseStatusList",
    "DrainFailure",
    "Durability",
    "Event",
    "EventList",
    "LeaseCleanupError",
    "Pool",
    "PrefixReplica",
    "ReleaseResult",
    "ReplicaList",
    "RestorableVersion",
    "RestoreResult",
    "RevertResult",
    "ShutdownResult",
    "Status",
    "StopBackupResult",
    "SyncedState",
    "VersionList",
    "WriteProtection",
    "Writer",
];

/// A pool adapter for naming `databases.pool`.
struct NoDatabase;

impl Adapter for NoDatabase {
    type Connection = ();

    fn open(&self, _path: &std::path::Path) -> impl Future<Output = Result<(), litebackup::BoxError>> + Send {
        std::future::ready(Ok(()))
    }

    fn close(&self, (): ()) -> impl Future<Output = Result<(), litebackup::CloseError<()>>> + Send {
        std::future::ready(Ok(()))
    }
}

/// Names each operation by building its call, without running it, so a
/// missing or misspelled method fails to compile.
#[allow(clippy::let_underscore_future, reason = "the calls are built to name them, never run")]
fn rust_operations(client: &Client) -> Vec<&'static str> {
    let _ = client.backups().start("", BackupOptions::default());
    let _ = client.backups().once("", OnceOptions::default());
    let _ = client.backups().stop("");
    let _ = client.rules().list();
    let _ = client.rules().stop("");
    let _ = client.databases().get("");
    let _ = client.databases().page(PageOptions::default());
    let _ = client.databases().all();
    let _ = client.databases().list();
    let _ = client.databases().versions("", VersionsOptions::default());
    let _ = client.databases().versions_page("", VersionsOptions::default());
    let _ = client.databases().all_versions("", VersionsOptions::default());
    let _ = client.databases().restore("", "", RestoreOptions::default());
    let _ = client.databases().revert("", RevertOptions::default());
    let _ = client.databases().sync("");
    let _ = client.databases().offload("", OffloadOptions::default());
    let _ = client.databases().writer("");
    let _ = client.databases().acquire("", AcquireOptions::default());
    let _ = client.databases().release("", ReleaseOptions::default());
    let _: Pool<NoDatabase> = client.databases().pool(NoDatabase, PoolOptions::default()).expect("a pool");
    let _ = client.replicas().start("", ReplicaOptions::default());
    let _ = client.replicas().start_prefix("", "", PrefixReplicaOptions::default());
    let _ = client.replicas().list();
    let _ = client.replicas().stop("");
    let _ = client.replicas().stop_prefix("", "");
    let _ = client.events().page(EventsOptions::default());
    let _ = client.events().watch(WatchOptions::default());
    let _ = client.session().status(PageOptions::default());
    let _ = client.session().shutdown(ShutdownOptions::default());
    let _ = client.failovers().start("", FailoverOptions::default());
    vec![
        "backups.start",
        "backups.once",
        "backups.stop",
        "rules.list",
        "rules.stop",
        "databases.get",
        "databases.page",
        "databases.all",
        "databases.list",
        "databases.versions",
        "databases.versions_page",
        "databases.all_versions",
        "databases.restore",
        "databases.revert",
        "databases.sync",
        "databases.offload",
        "databases.writer",
        "databases.acquire",
        "databases.release",
        "databases.pool",
        "replicas.start",
        "replicas.start_prefix",
        "replicas.list",
        "replicas.stop",
        "replicas.stop_prefix",
        "events.page",
        "events.watch",
        "session.status",
        "session.shutdown",
        "failovers.start",
    ]
}

#[tokio::test]
async fn every_operation_and_type_is_named_as_the_contract_lists_it() {
    let daemon = support::FakeDaemon::start(Vec::new());
    let client = support::client_to(&daemon).await;
    let text = std::fs::read_to_string(fixture_root().join("names/sdk-names.json")).expect("the names file");
    let names: serde_json::Value = serde_json::from_str(&text).expect("the names file's JSON");
    let listed: BTreeSet<&str> = names["operations"]
        .as_array()
        .expect("operations")
        .iter()
        .map(|operation| operation["operation"].as_str().expect("an operation name"))
        .collect();
    let rust: BTreeSet<&str> = rust_operations(&client).into_iter().collect();
    assert_eq!(rust, listed, "the Rust operations differ from sdk-names.json");
    let types: BTreeSet<&str> =
        names["types"].as_array().expect("types").iter().map(|name| name.as_str().unwrap()).collect();
    assert_eq!(SHARED_TYPES.into_iter().collect::<BTreeSet<_>>(), types, "the Rust types differ from sdk-names.json");
    assert!(daemon.requests().is_empty(), "naming an operation ran it");
}
