// Code generated from client-openapi.yaml by `cargo xtask`. DO NOT EDIT.

//! The socket API's answers, written from `client-openapi.yaml`. Fields keep
//! their wire names. Unknown fields are ignored, and a required field the
//! daemon leaves out or sends as null holds its type's default, so a newer
//! daemon's answers keep decoding; a field of the wrong type is
//! `invalid_response`. Optional fields are `Option`s, lists are empty when
//! left out, and enums are string newtypes, so values a newer daemon sends
//! survive.
#![allow(clippy::doc_markdown, reason = "the descriptions are the spec's prose")]

use std::borrow::Cow;
use std::fmt;

use serde::{Deserialize, Serialize};

use crate::model_fields::null_as_default;

/// What happened to a database: role_changed (role is origin or replica, and message is text),
/// draining (a failover origin stops being the origin before a drain releases its lock: its role is
/// unknown until the next role_changed), refused (path names the refused file, such as a failover's
/// file that holds writes the server lacks), path_unreadable (path names the path a backup rule
/// could not read), and the others by their names.
///
/// Values this SDK does not know are kept as they are, so compare with `==`
/// against the constants or read [`as_str`](Self::as_str).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct EventType(pub(crate) Cow<'static, str>);

impl EventType {
    /// `acquired`.
    pub const ACQUIRED: Self = Self(Cow::Borrowed("acquired"));
    /// `download_finished`.
    pub const DOWNLOAD_FINISHED: Self = Self(Cow::Borrowed("download_finished"));
    /// `download_started`.
    pub const DOWNLOAD_STARTED: Self = Self(Cow::Borrowed("download_started"));
    /// `draining`.
    pub const DRAINING: Self = Self(Cow::Borrowed("draining"));
    /// `image_written`.
    pub const IMAGE_WRITTEN: Self = Self(Cow::Borrowed("image_written"));
    /// `local_write`.
    pub const LOCAL_WRITE: Self = Self(Cow::Borrowed("local_write"));
    /// `lock_lost`.
    pub const LOCK_LOST: Self = Self(Cow::Borrowed("lock_lost"));
    /// `offloaded`.
    pub const OFFLOADED: Self = Self(Cow::Borrowed("offloaded"));
    /// `path_unreadable`.
    pub const PATH_UNREADABLE: Self = Self(Cow::Borrowed("path_unreadable"));
    /// `rebased`.
    pub const REBASED: Self = Self(Cow::Borrowed("rebased"));
    /// `refused`.
    pub const REFUSED: Self = Self(Cow::Borrowed("refused"));
    /// `released`.
    pub const RELEASED: Self = Self(Cow::Borrowed("released"));
    /// `replicating`.
    pub const REPLICATING: Self = Self(Cow::Borrowed("replicating"));
    /// `restored`.
    pub const RESTORED: Self = Self(Cow::Borrowed("restored"));
    /// `reverted`.
    pub const REVERTED: Self = Self(Cow::Borrowed("reverted"));
    /// `role_changed`.
    pub const ROLE_CHANGED: Self = Self(Cow::Borrowed("role_changed"));
    /// `rollback_scan`.
    pub const ROLLBACK_SCAN: Self = Self(Cow::Borrowed("rollback_scan"));
    /// `server_generation`.
    pub const SERVER_GENERATION: Self = Self(Cow::Borrowed("server_generation"));
    /// `stopped`.
    pub const STOPPED: Self = Self(Cow::Borrowed("stopped"));
    /// `tracked`.
    pub const TRACKED: Self = Self(Cow::Borrowed("tracked"));

    /// A value as the daemon sends it, known to this SDK or not.
    #[must_use]
    pub fn new(value: impl Into<String>) -> Self {
        Self(Cow::Owned(value.into()))
    }

    /// The wire value.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for EventType {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// How far a version must reach: server once the server holds it, cloud once the bucket holds it
/// too.
///
/// Values this SDK does not know are kept as they are, so compare with `==`
/// against the constants or read [`as_str`](Self::as_str).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Durability(pub(crate) Cow<'static, str>);

impl Durability {
    /// `server`.
    pub const SERVER: Self = Self(Cow::Borrowed("server"));
    /// `cloud`.
    pub const CLOUD: Self = Self(Cow::Borrowed("cloud"));

    /// A value as the daemon sends it, known to this SDK or not.
    #[must_use]
    pub fn new(value: impl Into<String>) -> Self {
        Self(Cow::Owned(value.into()))
    }

    /// The wire value.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for Durability {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// Origin when this machine writes the database and backs it up; replica for a live readonly copy.
///
/// Values this SDK does not know are kept as they are, so compare with `==`
/// against the constants or read [`as_str`](Self::as_str).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Role(pub(crate) Cow<'static, str>);

impl Role {
    /// `origin`.
    pub const ORIGIN: Self = Self(Cow::Borrowed("origin"));
    /// `replica`.
    pub const REPLICA: Self = Self(Cow::Borrowed("replica"));

    /// A value as the daemon sends it, known to this SDK or not.
    #[must_use]
    pub fn new(value: impl Into<String>) -> Self {
        Self(Cow::Owned(value.into()))
    }

    /// The wire value.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for Role {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// Pending while a backup or failover origin holds no write lock, as after a start or a drain,
/// until a sync takes it (a start takes back the lock of each backup a drain left pending while the
/// head is still the version it last synced); syncing while changes stream; retrying while an
/// origin's syncs fail, with last_error saying why and next_retry_time when the next attempt runs;
/// synced when the server holds every change: a backup or failover origin whose last sync landed
/// with no change seen after it, until its next change, or a released database whose last version
/// is on the server; waiting while another machine is the origin; offloaded when the local file was
/// removed because the server, or the bucket when the offload asked for it, holds its last version;
/// stopped for a stopped replica; conflicted when this copy was refused and left as it is, because
/// its file differs from the server head or its write lock was lost or broken; error with
/// last_error.
///
/// Values this SDK does not know are kept as they are, so compare with `==`
/// against the constants or read [`as_str`](Self::as_str).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct DatabaseState(pub(crate) Cow<'static, str>);

impl DatabaseState {
    /// `pending`.
    pub const PENDING: Self = Self(Cow::Borrowed("pending"));
    /// `syncing`.
    pub const SYNCING: Self = Self(Cow::Borrowed("syncing"));
    /// `retrying`.
    pub const RETRYING: Self = Self(Cow::Borrowed("retrying"));
    /// `synced`.
    pub const SYNCED: Self = Self(Cow::Borrowed("synced"));
    /// `waiting`.
    pub const WAITING: Self = Self(Cow::Borrowed("waiting"));
    /// `offloaded`.
    pub const OFFLOADED: Self = Self(Cow::Borrowed("offloaded"));
    /// `stopped`.
    pub const STOPPED: Self = Self(Cow::Borrowed("stopped"));
    /// `conflicted`.
    pub const CONFLICTED: Self = Self(Cow::Borrowed("conflicted"));
    /// `error`.
    pub const ERROR: Self = Self(Cow::Borrowed("error"));

    /// A value as the daemon sends it, known to this SDK or not.
    #[must_use]
    pub fn new(value: impl Into<String>) -> Self {
        Self(Cow::Owned(value.into()))
    }

    /// The wire value.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for DatabaseState {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// What the acquire did to the local file.
///
/// Values this SDK does not know are kept as they are, so compare with `==`
/// against the constants or read [`as_str`](Self::as_str).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct AcquireDecision(pub(crate) Cow<'static, str>);

impl AcquireDecision {
    /// `created`.
    pub const CREATED: Self = Self(Cow::Borrowed("created"));
    /// `matched`.
    pub const MATCHED: Self = Self(Cow::Borrowed("matched"));
    /// `resumed`.
    pub const RESUMED: Self = Self(Cow::Borrowed("resumed"));
    /// `downloaded`.
    pub const DOWNLOADED: Self = Self(Cow::Borrowed("downloaded"));
    /// `fast_forwarded`.
    pub const FAST_FORWARDED: Self = Self(Cow::Borrowed("fast_forwarded"));
    /// `replaced`.
    pub const REPLACED: Self = Self(Cow::Borrowed("replaced"));

    /// A value as the daemon sends it, known to this SDK or not.
    #[must_use]
    pub fn new(value: impl Into<String>) -> Self {
        Self(Cow::Owned(value.into()))
    }

    /// The wire value.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for AcquireDecision {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// Lock (default) holds the SQLite write lock so app writes get SQLITE_BUSY while readers carry on;
/// detect rebases after a local write; none trusts apps to open with mode=ro.
///
/// Values this SDK does not know are kept as they are, so compare with `==`
/// against the constants or read [`as_str`](Self::as_str).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct WriteProtection(pub(crate) Cow<'static, str>);

impl WriteProtection {
    /// `lock`.
    pub const LOCK: Self = Self(Cow::Borrowed("lock"));
    /// `detect`.
    pub const DETECT: Self = Self(Cow::Borrowed("detect"));
    /// `none`.
    pub const NONE: Self = Self(Cow::Borrowed("none"));

    /// A value as the daemon sends it, known to this SDK or not.
    #[must_use]
    pub fn new(value: impl Into<String>) -> Self {
        Self(Cow::Owned(value.into()))
    }

    /// The wire value.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for WriteProtection {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// A `Event` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct Event {
    /// The daemon's `id`.
    #[serde(deserialize_with = "null_as_default")]
    pub id: i64,
    /// The daemon's `time`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub time: Option<jiff::Timestamp>,
    /// The daemon's `name`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    /// What happened to a database: role_changed (role is origin or replica, and message is text),
    /// draining (a failover origin stops being the origin before a drain releases its lock: its
    /// role is unknown until the next role_changed), refused (path names the refused file, such as
    /// a failover's file that holds writes the server lacks), path_unreadable (path names the path
    /// a backup rule could not read), and the others by their names.
    #[serde(deserialize_with = "null_as_default")]
    pub r#type: EventType,
    /// The daemon's `message`.
    #[serde(deserialize_with = "null_as_default")]
    pub message: String,
    /// For role_changed, the new role.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    /// For refused, the refused file; for path_unreadable, the path the rule could not read.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
}

/// A version the server acknowledged. Clients decide what durability is enough.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct SyncedState {
    /// The daemon's `version`.
    #[serde(deserialize_with = "null_as_default")]
    pub version: i64,
    /// The daemon's `time`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub time: Option<jiff::Timestamp>,
}

/// The newest version the server reported the primary bucket holds.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct ArchivedVersion {
    /// The daemon's `version`.
    #[serde(deserialize_with = "null_as_default")]
    pub version: i64,
    /// When the server recorded the version as archived.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub archived: Option<jiff::Timestamp>,
}

/// A `DatabaseStatus` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct DatabaseStatus {
    /// The daemon's `name`.
    #[serde(deserialize_with = "null_as_default")]
    pub name: String,
    /// The daemon's `path`.
    #[serde(deserialize_with = "null_as_default")]
    pub path: String,
    /// Origin when this machine writes the database and backs it up; replica for a live readonly
    /// copy.
    #[serde(deserialize_with = "null_as_default")]
    pub role: Role,
    /// Pending while a backup or failover origin holds no write lock, as after a start or a drain,
    /// until a sync takes it (a start takes back the lock of each backup a drain left pending while
    /// the head is still the version it last synced); syncing while changes stream; retrying while
    /// an origin's syncs fail, with last_error saying why and next_retry_time when the next attempt
    /// runs; synced when the server holds every change: a backup or failover origin whose last sync
    /// landed with no change seen after it, until its next change, or a released database whose
    /// last version is on the server; waiting while another machine is the origin; offloaded when
    /// the local file was removed because the server, or the bucket when the offload asked for it,
    /// holds its last version; stopped for a stopped replica; conflicted when this copy was refused
    /// and left as it is, because its file differs from the server head or its write lock was lost
    /// or broken; error with last_error.
    #[serde(deserialize_with = "null_as_default")]
    pub state: DatabaseState,
    /// The database is acquired and released by a worker rather than backed up continuously.
    #[serde(deserialize_with = "null_as_default")]
    pub acquired: bool,
    /// The database belongs to a failover and changes role when the origin does.
    #[serde(deserialize_with = "null_as_default")]
    pub failover: bool,
    /// The backup or failover rule that tracks the database on this machine; absent for an acquired
    /// database and a replica.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub backup_rule_id: Option<String>,
    /// The daemon's `local_changed`.
    #[serde(deserialize_with = "null_as_default")]
    pub local_changed: bool,
    /// A version the server acknowledged. Clients decide what durability is enough.
    #[serde(deserialize_with = "null_as_default")]
    pub synced_server: SyncedState,
    /// The newest version the server reported the primary bucket holds.
    #[serde(deserialize_with = "null_as_default")]
    pub synced_cloud: ArchivedVersion,
    /// Wal, rollback, or unknown before the first scan. A full scan of a rollback-journal database
    /// holds a read lock that blocks the app's writers until it ends, so it runs at full speed
    /// rather than at --scan-bytes-per-second, and the first scan past 256 MB records a
    /// rollback_scan event with its duration. Switch such a database to WAL mode (PRAGMA
    /// journal_mode = WAL), where scans never block writers; Litebackup never changes the journal
    /// mode itself.
    #[serde(deserialize_with = "null_as_default")]
    pub journal_mode: String,
    /// The daemon's `page_count`.
    #[serde(deserialize_with = "null_as_default")]
    pub page_count: i64,
    /// The daemon's `sync_count`.
    #[serde(deserialize_with = "null_as_default")]
    pub sync_count: i64,
    /// The daemon's `hint_misses`.
    #[serde(deserialize_with = "null_as_default")]
    pub hint_misses: i64,
    /// The daemon's `last_full_scan_reason`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_full_scan_reason: Option<String>,
    /// The machine label of the last writer this machine saw; empty once this machine released the
    /// database, drained it, or offloaded a copy whose write lock it held, as no writer is known
    /// then.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub writer_machine_label: Option<String>,
    /// The daemon's `last_error`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    /// The daemon's `last_error_time`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_error_time: Option<jiff::Timestamp>,
    /// When the first change the server lacks was seen; absent when the server has every change
    /// seen. It survives restarts and may be earlier than the real first change, never later.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub first_unsynced_change_time: Option<jiff::Timestamp>,
    /// When the next attempt of a failed sync runs; absent when none is scheduled.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub next_retry_time: Option<jiff::Timestamp>,
    /// Set on a failover origin while this machine drains, and on one a drain released, which holds
    /// no write lock until it takes one again; a failover then reports its role as unknown, as
    /// after a draining event.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub draining: Option<bool>,
    /// The name this machine shows on the server and as the writer of its databases.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub machine_label: Option<String>,
    /// The daemon's `maintenance`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub maintenance: Option<bool>,
    /// The client deletes the released copy once offload_durability holds it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub offload_on_release: Option<bool>,
    /// What an offload of this acquired copy waits for: server removes the released copy once the
    /// server holds its last version, cloud once the bucket holds it too. An acquire, a release
    /// with offload, and an explicit offload each save the durability they asked for. Absent when
    /// the database is not acquired.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub offload_durability: Option<Durability>,
}

/// A `Status` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct Status {
    /// Pass this name as after to read the next page. Omitted on the last page.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub next: Option<String>,
    /// The daemon's `session`.
    #[serde(deserialize_with = "null_as_default")]
    pub session: String,
    /// The daemon's `server`.
    #[serde(deserialize_with = "null_as_default")]
    pub server: String,
    /// The daemon's `client_version`.
    #[serde(deserialize_with = "null_as_default")]
    pub client_version: String,
    /// The daemon's `session_id`.
    #[serde(deserialize_with = "null_as_default")]
    pub session_id: String,
    /// The daemon's `lease_expires`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub lease_expires: Option<jiff::Timestamp>,
    /// The daemon's `watch`.
    #[serde(deserialize_with = "null_as_default")]
    pub watch: String,
    /// The soft limit on open files the daemon runs with. Each active backup holds about four, and
    /// the daemon lowers its default number of active backups to fit.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub open_file_limit: Option<i64>,
    /// The paths a backup rule cannot read, most recent first; the events API has the history.
    #[serde(deserialize_with = "null_as_default")]
    pub unreadable_paths: Vec<Event>,
    /// The daemon's `databases`.
    #[serde(deserialize_with = "null_as_default")]
    pub databases: Vec<DatabaseStatus>,
}

/// A `BackupRule` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct BackupRule {
    /// The daemon's `id`.
    #[serde(deserialize_with = "null_as_default")]
    pub id: String,
    /// The path or glob.
    #[serde(deserialize_with = "null_as_default")]
    pub path: String,
    /// The resolved remote name for a single file, or empty for a directory or glob.
    #[serde(deserialize_with = "null_as_default")]
    pub name: String,
    /// The daemon's `prefix`.
    #[serde(deserialize_with = "null_as_default")]
    pub prefix: String,
    /// The daemon's `watch`.
    #[serde(deserialize_with = "null_as_default")]
    pub watch: String,
    /// The daemon's `maximum_local_databases`.
    #[serde(deserialize_with = "null_as_default")]
    pub maximum_local_databases: i64,
    /// What maximum_local_databases waits for before it removes a copy.
    #[serde(deserialize_with = "null_as_default")]
    pub offload_durability: Durability,
    /// The daemon's `offload_rollback_journal`.
    #[serde(deserialize_with = "null_as_default")]
    pub offload_rollback_journal: bool,
    /// The daemon's `failover`.
    #[serde(deserialize_with = "null_as_default")]
    pub failover: bool,
}

/// A `DatabaseStatusList` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct DatabaseStatusList {
    /// Pass this name as after to read the next page. Omitted on the last page.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub next: Option<String>,
    /// The daemon's `databases`.
    #[serde(deserialize_with = "null_as_default")]
    pub databases: Vec<DatabaseStatus>,
}

/// A `RestorableVersion` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct RestorableVersion {
    /// The daemon's `version`.
    #[serde(deserialize_with = "null_as_default")]
    pub version: i64,
    /// The daemon's `state_digest`.
    #[serde(deserialize_with = "null_as_default")]
    pub state_digest: String,
    /// When the version was synced.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub time: Option<jiff::Timestamp>,
    /// When the server recorded the primary bucket's object of this version as archived; absent
    /// when the primary bucket does not hold the version, as for one synced between archive passes,
    /// which restores only until history pruning removes its segment.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub archived: Option<jiff::Timestamp>,
}

/// A `VersionList` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct VersionList {
    /// The daemon's `versions`.
    #[serde(deserialize_with = "null_as_default")]
    pub versions: Vec<RestorableVersion>,
    /// Pass as after to read the next page.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub next: Option<i64>,
}

/// A `RestoreResult` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct RestoreResult {
    /// The daemon's `name`.
    #[serde(deserialize_with = "null_as_default")]
    pub name: String,
    /// The daemon's `path`.
    #[serde(deserialize_with = "null_as_default")]
    pub path: String,
    /// The daemon's `version`.
    #[serde(deserialize_with = "null_as_default")]
    pub version: i64,
    /// The daemon's `page_count`.
    #[serde(deserialize_with = "null_as_default")]
    pub page_count: i64,
    /// The daemon's `page_size`.
    #[serde(deserialize_with = "null_as_default")]
    pub page_size: i64,
    /// The daemon's `state_digest`.
    #[serde(deserialize_with = "null_as_default")]
    pub state_digest: String,
}

/// A `RevertResult` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct RevertResult {
    /// The daemon's `database`.
    #[serde(deserialize_with = "null_as_default")]
    pub database: DatabaseStatus,
    /// The version whose content the new head holds; it and every version after it stay restorable.
    #[serde(deserialize_with = "null_as_default")]
    pub source_version: i64,
    /// The new head, one past the head before the revert.
    #[serde(deserialize_with = "null_as_default")]
    pub version: i64,
    /// The new head's state digest, which is the source version's.
    #[serde(deserialize_with = "null_as_default")]
    pub state_digest: String,
}

/// A `AcquireResult` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct AcquireResult {
    /// The daemon's `database`.
    #[serde(deserialize_with = "null_as_default")]
    pub database: DatabaseStatus,
    /// What the acquire did to the local file.
    #[serde(deserialize_with = "null_as_default")]
    pub decision: AcquireDecision,
}

/// A `ReleaseResult` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct ReleaseResult {
    /// The daemon's `database`.
    #[serde(deserialize_with = "null_as_default")]
    pub database: DatabaseStatus,
    /// The daemon's `checkpointed`.
    #[serde(deserialize_with = "null_as_default")]
    pub checkpointed: bool,
    /// The daemon's `vacuumed`.
    #[serde(deserialize_with = "null_as_default")]
    pub vacuumed: bool,
    /// The daemon's `offloaded`.
    #[serde(deserialize_with = "null_as_default")]
    pub offloaded: bool,
}

/// A `Writer` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct Writer {
    /// The daemon's `name`.
    #[serde(deserialize_with = "null_as_default")]
    pub name: String,
    /// Whether some machine holds the write lock and its lease still runs.
    #[serde(deserialize_with = "null_as_default")]
    pub active: bool,
    /// Whether this machine's session holds the write lock.
    #[serde(deserialize_with = "null_as_default")]
    pub this_machine: bool,
    /// The machine label of the writer, or empty when nobody holds the lock.
    #[serde(deserialize_with = "null_as_default")]
    pub writer_machine_label: String,
    /// When the writer took the lock.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub acquired_time: Option<jiff::Timestamp>,
    /// On the server's clock.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub lease_expires: Option<jiff::Timestamp>,
    /// How long the lease still runs, negative once it ran out.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub lease_remaining_millis: Option<i64>,
    /// The writer's lease ran out, so another machine may take over.
    #[serde(deserialize_with = "null_as_default")]
    pub lapsed: bool,
}

/// A `BackupList` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub(crate) struct BackupList {
    /// The daemon's `rules`.
    #[serde(deserialize_with = "null_as_default")]
    pub(crate) rules: Vec<BackupRule>,
}

/// A `StopBackupResult` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct StopBackupResult {
    /// The daemon's `lease_pending`.
    #[serde(deserialize_with = "null_as_default")]
    pub lease_pending: bool,
}

/// A `Failover` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct FailoverStatus {
    /// The daemon's `name`.
    #[serde(deserialize_with = "null_as_default")]
    pub name: String,
    /// The daemon's `path`.
    #[serde(deserialize_with = "null_as_default")]
    pub path: String,
    /// The daemon's `role`.
    #[serde(deserialize_with = "null_as_default")]
    pub role: Role,
    /// The daemon's `database`.
    #[serde(deserialize_with = "null_as_default")]
    pub database: DatabaseStatus,
    /// The daemon's `writer`.
    #[serde(deserialize_with = "null_as_default")]
    pub writer: Writer,
}

/// A `PrefixReplica` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct PrefixReplica {
    /// The daemon's `id`.
    #[serde(deserialize_with = "null_as_default")]
    pub id: String,
    /// The daemon's `prefix`.
    #[serde(deserialize_with = "null_as_default")]
    pub prefix: String,
    /// The directory that receives \<directory\>/\<name relative to the prefix\>.
    #[serde(deserialize_with = "null_as_default")]
    pub directory: String,
    /// The daemon's `write_protection`.
    #[serde(deserialize_with = "null_as_default")]
    pub write_protection: WriteProtection,
    /// How many databases under the prefix this machine replicates.
    #[serde(deserialize_with = "null_as_default")]
    pub database_count: i64,
}

/// The replica for a name, or the prefix replica for a prefix.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub(crate) struct ReplicaResult {
    /// The daemon's `database`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) database: Option<DatabaseStatus>,
    /// The daemon's `prefix_replica`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) prefix_replica: Option<PrefixReplica>,
}

/// A `ReplicaList` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct ReplicaList {
    /// The daemon's `prefixes`.
    #[serde(deserialize_with = "null_as_default")]
    pub prefixes: Vec<PrefixReplica>,
    /// The daemon's `replicas`.
    #[serde(deserialize_with = "null_as_default")]
    pub replicas: Vec<DatabaseStatus>,
}

/// A `EventList` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct EventList {
    /// The daemon's `events`.
    #[serde(deserialize_with = "null_as_default")]
    pub events: Vec<Event>,
    /// Pass as after to continue.
    #[serde(deserialize_with = "null_as_default")]
    pub next: i64,
}

/// A `DrainFailure` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct DrainFailure {
    /// The daemon's `name`.
    #[serde(deserialize_with = "null_as_default")]
    pub name: String,
    /// What went wrong, in one word. The socket answers with file_exists (the file a restore or an
    /// acquire would write, or a -journal, -wal, or -shm beside it, already exists), rule_exists (a
    /// backup rule with other settings already covers the path, and the error carries that rule),
    /// lock_unavailable (another machine holds the write lock a command asks for, as an acquire, a
    /// revert, or a failover does), not_found, still_open, invalid_argument, timeout,
    /// cursor_expired (an events cursor older than the oldest event kept), changed (an offload or
    /// acquire refused because the local file changed under it: written after its last sync, never
    /// synced, or written while an acquire replaced it), rule_database (a release of a database a
    /// backup rule on this machine keeps, whose write lock the rule never gives up; stop its backup
    /// to hand it off), not_durable (an offload refused until the server, or the bucket when it
    /// waits for cloud, holds the synced version, or a cloud wait whose name now holds another
    /// server database), and the others by their names; busy (a foreground backup refused because
    /// another job, a continuous backup, or another machine writes the database), cancelled (a
    /// command interrupted before it finished), drain_required (a credential other than the one the
    /// client took its write locks with, refused until a drain released them), and launch_failed
    /// (the command could not start the client on this machine, or the client exited before it
    /// listened) come from the litebackup command alone, which also answers unauthenticated when
    /// this machine has not joined a server. A refusal from the server becomes client_unsupported
    /// (426: the server needs a newer litebackup on this machine) or, with 503, not_configured (the
    /// server waits for setup), wrong_server (the server this machine reached is not the one its
    /// session joined, or does not own the database), unauthenticated (the server no longer accepts
    /// this machine's key), forbidden (the key may not do this), plan_limit (the server's plan has
    /// no room for another database), or integrity (the version's bucket objects failed an
    /// integrity check). lock_unavailable, lock_lost, not_found, invalid_argument, and timeout keep
    /// their meaning, and any other refusal (a busy server, one short of disk, one unreachable) is
    /// unavailable. A client treats a code it does not know as a failure.
    #[serde(deserialize_with = "null_as_default")]
    pub code: String,
    /// The daemon's `message`.
    #[serde(deserialize_with = "null_as_default")]
    pub message: String,
}

/// A `ShutdownResult` the daemon answers with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct ShutdownResult {
    /// How many databases the drain synced as asked and released.
    #[serde(deserialize_with = "null_as_default")]
    pub released: i64,
    /// Every database the drain could not sync, wait for, or release, each keeping any write lock
    /// it holds, and every conflicted copy whose writes are not on the server, with conflict; empty
    /// only when the drain released every database it covers, which is the one case the daemon
    /// stops.
    #[serde(deserialize_with = "null_as_default")]
    pub failed: Vec<DrainFailure>,
}
