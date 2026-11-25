//! `client.replicas()`.

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::de::IgnoredAny;

use crate::client::Client;
use crate::error::{Error, ErrorCode, Result};
use crate::models::{DatabaseStatus, PrefixReplica, ReplicaList, ReplicaResult, WriteProtection};
use crate::options::option_setters;
use crate::wire::{self, Body, Call};

/// Options of [`Replicas::start`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct ReplicaOptions {
    /// The replica file; the path this machine uses when unset.
    pub path: Option<PathBuf>,
    /// How the replica keeps app writes out: lock (the daemon's default),
    /// detect, or none.
    pub write_protection: Option<WriteProtection>,
    /// Bounds downloading the first version.
    pub timeout: Option<Duration>,
}

option_setters!(ReplicaOptions {
    path: impl AsRef<Path> => Some(path.as_ref().to_path_buf()),
    write_protection: WriteProtection => Some(write_protection),
    timeout: Duration => Some(timeout),
});

/// Options of [`Replicas::start_prefix`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct PrefixReplicaOptions {
    /// How each replica keeps app writes out: lock (the daemon's default),
    /// detect, or none.
    pub write_protection: Option<WriteProtection>,
}

option_setters!(PrefixReplicaOptions {
    write_protection: WriteProtection => Some(write_protection),
});

/// Live readonly replicas; [`Client::replicas`].
#[derive(Debug, Clone, Copy)]
pub struct Replicas<'c> {
    client: &'c Client,
}

impl<'c> Replicas<'c> {
    pub(crate) fn new(client: &'c Client) -> Self {
        Self { client }
    }

    /// Keeps a live readonly replica of a database that streams from the
    /// server, and returns once its first version is local.
    ///
    /// # Errors
    ///
    /// `not_found` for a database the server does not have, or
    /// `invalid_response` when the daemon returns no replica.
    pub async fn start(&self, name: &str, options: ReplicaOptions) -> Result<DatabaseStatus> {
        let path = match wire::supplied(options.path.as_deref()) {
            Some(path) => Some(wire::absolute_path(path)?),
            None => None,
        };
        let body = Body::new()
            .text("name", name)
            .optional_text("path", path.as_deref())
            .optional_text("write_protection", options.write_protection.as_ref().map(WriteProtection::as_str))
            .millis("timeout_millis", options.timeout);
        let started: ReplicaResult =
            self.client.call(Call::post("/v1/replicas/start", body).with_timeout(options.timeout)).await?;
        started.database.ok_or_else(|| missing_answer("the daemon did not return the replica"))
    }

    /// Keeps live readonly replicas of current and future databases under a
    /// prefix, each at its name relative to the prefix inside `directory`;
    /// an empty prefix follows every database. Downloads run in the
    /// background, so returning does not mean every replica is local.
    ///
    /// # Errors
    ///
    /// `invalid_argument` for an empty directory.
    pub async fn start_prefix(
        &self,
        prefix: &str,
        directory: impl AsRef<Path>,
        options: PrefixReplicaOptions,
    ) -> Result<PrefixReplica> {
        let directory = directory.as_ref();
        if directory.as_os_str().is_empty() {
            return Err(Error::invalid_argument("replicating a prefix needs a directory"));
        }
        let body = Body::new()
            .text("prefix", prefix)
            .text("directory", &wire::absolute_path(directory)?)
            .optional_text("write_protection", options.write_protection.as_ref().map(WriteProtection::as_str));
        let started: ReplicaResult = self.client.call(Call::post("/v1/replicas/start", body)).await?;
        started.prefix_replica.ok_or_else(|| missing_answer("the daemon did not return the prefix replica"))
    }

    /// Prefix replicas and every replica on this machine.
    ///
    /// # Errors
    ///
    /// The daemon's error, or `connection_failed` when it does not answer.
    pub async fn list(&self) -> Result<ReplicaList> {
        self.client.call(Call::get("/v1/replicas/list", Vec::new())).await
    }

    /// Stops a replica. The file stays, unlocked, at its last version.
    ///
    /// # Errors
    ///
    /// The daemon's error, such as `not_found`.
    pub async fn stop(&self, name: &str) -> Result<()> {
        let IgnoredAny = self.client.call(Call::post("/v1/replicas/stop", Body::new().text("name", name))).await?;
        Ok(())
    }

    /// Stops a prefix replica and its replicas; use the prefix and directory
    /// it started with. Files stay, unlocked, at their last versions.
    ///
    /// # Errors
    ///
    /// `invalid_argument` for an empty directory.
    pub async fn stop_prefix(&self, prefix: &str, directory: impl AsRef<Path>) -> Result<()> {
        let directory = directory.as_ref();
        if directory.as_os_str().is_empty() {
            return Err(Error::invalid_argument("stopping a prefix replica needs a directory"));
        }
        let body = Body::new().text("prefix", prefix).text("directory", &wire::absolute_path(directory)?);
        let IgnoredAny = self.client.call(Call::post("/v1/replicas/stop", body)).await?;
        Ok(())
    }
}

/// `invalid_response` for an answer that lacks what the call returns.
fn missing_answer(message: &str) -> Error {
    Error::new(ErrorCode::INVALID_RESPONSE, message).with_status(200)
}
