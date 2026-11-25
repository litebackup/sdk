//! `client.databases()`.

use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::time::Duration;

use futures_core::Stream;

use crate::adapter::Adapter;
use crate::client::Client;
use crate::error::{Error, Result};
use crate::foreground::checked_durability;
use crate::models::{
    AcquireResult, DatabaseStatus, DatabaseStatusList, Durability, ReleaseResult, RestorableVersion, RestoreResult,
    RevertResult, VersionList, Writer,
};
use crate::options::option_setters;
use crate::pool::{Pool, PoolOptions};
use crate::umask::request_umask;
use crate::wire::{self, Body, Call};

/// Selects one page of a listing by name: [`Databases::page`] and
/// [`Session::status`](crate::Session::status).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct PageOptions {
    /// Lists names that sort after this one; pass the previous page's `next`.
    pub after: Option<String>,
    /// Lists at most this many, from 1 to 1000; 100 when unset.
    pub limit: Option<u32>,
}

option_setters!(PageOptions {
    after: impl Into<String> => Some(after.into()),
    limit: u32 => Some(limit),
});

/// Selects versions: [`Databases::versions`] and its page and stream.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct VersionsOptions {
    /// Skips versions up to and including this one.
    pub after: Option<i64>,
    /// Lists at most this many versions; every version when unset, and none
    /// for 0.
    pub limit: Option<u32>,
}

option_setters!(VersionsOptions {
    after: i64 => Some(after),
    limit: u32 => Some(limit),
});

/// Picks the version [`Databases::restore`] writes: the head when neither
/// `version` nor `time` is set.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct RestoreOptions {
    /// Restores this version.
    pub version: Option<i64>,
    /// Restores the newest version at or before this time.
    pub time: Option<jiff::Timestamp>,
    /// The file mode creation mask the new file is created under: it gets
    /// 0666 less it, as a file the caller created would. This process's
    /// umask when unset; up to 0o777.
    pub umask: Option<u32>,
}

option_setters!(RestoreOptions {
    version: i64 => Some(version),
    time: jiff::Timestamp => Some(time),
    umask: u32 => Some(umask),
});

/// Picks the version [`Databases::revert`] publishes again: a version or a
/// time, one of them.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct RevertOptions {
    /// The version whose content becomes the new head; 1 or more.
    pub version: Option<i64>,
    /// Picks the newest version at or before this time.
    pub time: Option<jiff::Timestamp>,
}

option_setters!(RevertOptions {
    version: i64 => Some(version),
    time: jiff::Timestamp => Some(time),
});

/// Options of [`Databases::offload`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct OffloadOptions {
    /// What the offload waits for before it removes the file: the server
    /// (the daemon's default) or the bucket too ([`Durability::CLOUD`]).
    pub durability: Option<Durability>,
}

option_setters!(OffloadOptions {
    durability: Durability => Some(durability),
});

/// Options of [`Databases::acquire`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct AcquireOptions {
    /// The local file; the path this machine already uses when unset, which
    /// brings an offloaded database back.
    pub path: Option<PathBuf>,
    /// Checkpoints on release and VACUUMs past the freelist threshold.
    pub maintenance: bool,
    /// Lets Litebackup remove the local copy once `offload_durability` holds
    /// the released version.
    pub offload_on_release: bool,
    /// What an offload of the released copy waits for: the server (the
    /// default) or the bucket too.
    pub offload_durability: Option<Durability>,
    /// What other machines see as the writer.
    pub machine_label: Option<String>,
    /// Bounds downloading the head.
    pub timeout: Option<Duration>,
    /// How long to wait while another machine holds the write lock, trying
    /// again until it is released; unset fails at once with
    /// `lock_unavailable`. The call may take `timeout` plus this.
    pub lock_timeout: Option<Duration>,
    /// The file mode creation mask a file the acquire creates gets; this
    /// process's umask when unset.
    pub umask: Option<u32>,
    /// Replaces a local file that diverged from the server head with the
    /// head, discarding its writes the head lacks. Without it such a file is
    /// refused with `conflict` and left untouched.
    pub discard_local: bool,
}

option_setters!(AcquireOptions {
    path: impl AsRef<Path> => Some(path.as_ref().to_path_buf()),
    maintenance: bool => maintenance,
    offload_on_release: bool => offload_on_release,
    offload_durability: Durability => Some(offload_durability),
    machine_label: impl Into<String> => Some(machine_label.into()),
    timeout: Duration => Some(timeout),
    lock_timeout: Duration => Some(lock_timeout),
    umask: u32 => Some(umask),
    discard_local: bool => discard_local,
});

/// Options of [`Databases::release`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct ReleaseOptions {
    /// How far the final sync must reach: server or cloud; the daemon's
    /// default when unset.
    pub durability: Option<Durability>,
    /// Removes the local file once `durability` holds the released version.
    pub offload: bool,
    /// Bounds waiting for the database to close and for `durability`.
    pub timeout: Option<Duration>,
}

option_setters!(ReleaseOptions {
    durability: Durability => Some(durability),
    offload: bool => offload,
    timeout: Duration => Some(timeout),
});

/// Databases on this machine and the server; [`Client::databases`].
#[derive(Debug, Clone, Copy)]
pub struct Databases<'c> {
    client: &'c Client,
}

impl<'c> Databases<'c> {
    pub(crate) fn new(client: &'c Client) -> Self {
        Self { client }
    }

    /// A pool that keeps databases open with `adapter`: see [`Pool`].
    ///
    /// # Errors
    ///
    /// `invalid_argument` for a zero cleanup timeout, or outside a tokio runtime.
    pub fn pool<A: Adapter>(&self, adapter: A, options: PoolOptions) -> Result<Pool<A>> {
        Pool::new(self.client, adapter, options)
    }

    /// One database's role, state, and synced versions.
    ///
    /// # Errors
    ///
    /// `not_found` for a database neither the server nor this machine has.
    pub async fn get(&self, name: &str) -> Result<DatabaseStatus> {
        self.client.call(Call::get("/v1/databases/status", vec![("name", name.to_owned())])).await
    }

    /// One page of databases; pass its `next` as `after` to continue.
    ///
    /// # Errors
    ///
    /// `invalid_argument` for a limit outside 1 to 1000.
    pub async fn page(&self, options: PageOptions) -> Result<DatabaseStatusList> {
        let query = vec![("after", options.after.unwrap_or_default()), ("limit", wire::page_limit(options.limit)?)];
        self.client.call(Call::get("/v1/databases/list", query)).await
    }

    /// Every database, page by page as the stream is read.
    pub fn all(&self) -> impl Stream<Item = Result<DatabaseStatus>> + Send + 'c {
        let client = self.client;
        let pages = PagesState { after: Some(String::new()), buffered: VecDeque::new() };
        futures_util::stream::unfold(pages, move |mut pages| async move {
            loop {
                if let Some(database) = pages.buffered.pop_front() {
                    return Some((Ok(database), pages));
                }
                let after = pages.after.take()?;
                match Databases::new(client).page(PageOptions::default().after(after)).await {
                    Ok(page) => {
                        pages.buffered.extend(page.databases);
                        pages.after = page.next.filter(|next| !next.is_empty());
                    }
                    Err(error) => return Some((Err(error), pages)),
                }
            }
        })
    }

    /// Every database, collected.
    ///
    /// # Errors
    ///
    /// The first page's error.
    pub async fn list(&self) -> Result<Vec<DatabaseStatus>> {
        read_listing(self.all()).await
    }

    /// One page of the versions the server restores, oldest first; pass its
    /// `next` as `after` to continue, 0 meaning no more.
    ///
    /// # Errors
    ///
    /// `not_found` for a database the server does not have.
    pub async fn versions_page(&self, name: &str, options: VersionsOptions) -> Result<VersionList> {
        let mut query = vec![("name", name.to_owned())];
        if let Some(after) = options.after.filter(|after| *after > 0) {
            query.push(("after", after.to_string()));
        }
        if let Some(limit) = options.limit {
            query.push(("limit", limit.to_string()));
        }
        self.client.call(Call::get("/v1/databases/versions", query)).await
    }

    /// The versions the server restores, oldest first, page by page as the
    /// stream is read; at most `limit` when set.
    #[allow(clippy::needless_pass_by_value, reason = "every call takes its options by value")]
    pub fn all_versions(
        &self,
        name: &str,
        options: VersionsOptions,
    ) -> impl Stream<Item = Result<RestorableVersion>> + Send + 'c {
        let client = self.client;
        let walk = VersionsState {
            name: name.to_owned(),
            after: options.after.unwrap_or(0),
            remaining: options.limit,
            buffered: VecDeque::new(),
            done: options.limit == Some(0),
        };
        futures_util::stream::unfold(walk, move |mut walk| async move {
            loop {
                if walk.remaining == Some(0) {
                    return None;
                }
                if let Some(version) = walk.buffered.pop_front() {
                    walk.remaining = walk.remaining.map(|remaining| remaining - 1);
                    return Some((Ok(version), walk));
                }
                if walk.done {
                    return None;
                }
                let page_options = VersionsOptions { after: Some(walk.after), limit: walk.remaining };
                match Databases::new(client).versions_page(&walk.name, page_options).await {
                    Ok(page) => {
                        let next = page.next.unwrap_or(0);
                        walk.done = next == 0 || next <= walk.after || page.versions.is_empty();
                        walk.after = next;
                        walk.buffered.extend(page.versions);
                    }
                    Err(error) => {
                        walk.done = true;
                        return Some((Err(error), walk));
                    }
                }
            }
        })
    }

    /// The versions the server restores, oldest first, collected.
    ///
    /// # Errors
    ///
    /// The first page's error.
    pub async fn versions(&self, name: &str, options: VersionsOptions) -> Result<Vec<RestorableVersion>> {
        read_listing(self.all_versions(name, options)).await
    }

    /// Writes a version to a new file and checks it. An existing file is
    /// never overwritten (`file_exists`). The file gets 0666 less the umask,
    /// this process's when unset.
    ///
    /// # Errors
    ///
    /// `invalid_argument` for an empty path or a version and a time together.
    pub async fn restore(&self, name: &str, path: impl AsRef<Path>, options: RestoreOptions) -> Result<RestoreResult> {
        let path = path.as_ref();
        if path.as_os_str().is_empty() {
            return Err(Error::invalid_argument("restore needs a path to write to"));
        }
        if options.version.is_some() && options.time.is_some() {
            return Err(Error::invalid_argument("restore takes a version or a time, not both"));
        }
        let body = Body::new()
            .text("name", name)
            .text("path", &wire::absolute_path(path)?)
            .integer("version", options.version)
            .optional_text("time", options.time.map(wire::wire_time).as_deref())
            .integer("umask", Some(i64::from(request_umask(options.umask)?)));
        self.client.call(Call::post("/v1/databases/restore", body)).await
    }

    /// Rolls a database back under its own name: a past version's content
    /// becomes a new head, one past the current head, and every later version
    /// stays restorable. The database must be one this machine acquired or
    /// backs up, and its file closed (`still_open` otherwise); a replica or a
    /// failover standby is refused, and a file holding writes the server
    /// lacks is refused with `conflict` and left untouched.
    ///
    /// # Errors
    ///
    /// `invalid_argument` without exactly one of a version and a time, or for
    /// a version below 1.
    pub async fn revert(&self, name: &str, options: RevertOptions) -> Result<RevertResult> {
        if options.version.is_some() == options.time.is_some() {
            return Err(Error::invalid_argument("revert takes a version or a time, one of them"));
        }
        if options.version.is_some_and(|version| version < 1) {
            return Err(Error::invalid_argument("revert needs a positive version"));
        }
        let body = Body::new()
            .text("name", name)
            .integer("version", options.version)
            .optional_text("time", options.time.map(wire::wire_time).as_deref());
        self.client.call(Call::post("/v1/databases/revert", body)).await
    }

    /// Syncs a database now and returns once the server acknowledged it.
    ///
    /// # Errors
    ///
    /// The daemon's error, such as `not_found`.
    pub async fn sync(&self, name: &str) -> Result<DatabaseStatus> {
        self.client.call(Call::post("/v1/databases/sync", Body::new().text("name", name))).await
    }

    /// Removes the local file of a released database once the server holds
    /// its last version, or the bucket with [`Durability::CLOUD`]; until then
    /// it fails with `not_durable`. Acquiring brings it back.
    ///
    /// # Errors
    ///
    /// `invalid_argument` for a durability other than server or cloud.
    pub async fn offload(&self, name: &str, options: OffloadOptions) -> Result<DatabaseStatus> {
        let durability = checked_durability(options.durability.as_ref())?;
        let body =
            Body::new().text("name", name).optional_text("durability", durability.as_ref().map(Durability::as_str));
        self.client.call(Call::post("/v1/databases/offload", body)).await
    }

    /// Which machine writes a database and how long its lease runs.
    ///
    /// # Errors
    ///
    /// The daemon's error, such as `not_found`.
    pub async fn writer(&self, name: &str) -> Result<Writer> {
        self.client.call(Call::get("/v1/databases/writer", vec![("name", name.to_owned())])).await
    }

    /// Takes the write lock on a database and makes the local file the
    /// server's head, for worker pools that move databases between machines.
    /// A pool acquires, opens, closes, and releases around a closure.
    ///
    /// # Errors
    ///
    /// `lock_unavailable` while another machine writes it, `conflict` for a
    /// local file that diverged, and `invalid_argument` for a bad umask or
    /// offload durability.
    pub async fn acquire(&self, name: &str, options: AcquireOptions) -> Result<AcquireResult> {
        let offload_durability = checked_durability(options.offload_durability.as_ref())?
            .filter(|durability| *durability != Durability::SERVER);
        let path = match wire::supplied(options.path.as_deref()) {
            Some(path) => Some(wire::absolute_path(path)?),
            None => None,
        };
        // name is required, so it is sent even when empty and the daemon answers for it.
        let body = Body::new()
            .text("name", name)
            .optional_text("path", path.as_deref())
            .flag("maintenance", options.maintenance)
            .flag("discard_local", options.discard_local)
            .flag("offload_on_release", options.offload_on_release)
            .optional_text("offload_durability", offload_durability.as_ref().map(Durability::as_str))
            .optional_text("machine_label", options.machine_label.as_deref())
            .millis("timeout_millis", options.timeout)
            .millis("lock_timeout_millis", options.lock_timeout)
            .integer("umask", Some(i64::from(request_umask(options.umask)?)));
        // No timeout leaves the call without a deadline, as the daemon applies its own.
        let call_timeout = options
            .timeout
            .filter(|timeout| !timeout.is_zero())
            .map(|timeout| timeout.saturating_add(options.lock_timeout.unwrap_or_default()));
        self.client.call(Call::post("/v1/databases/acquire", body).with_timeout(call_timeout)).await
    }

    /// Syncs an acquired database a final time, runs its maintenance, and
    /// releases its write lock. Every connection to it must be closed first,
    /// or it fails with `still_open` once the timeout runs out.
    ///
    /// # Errors
    ///
    /// `still_open`, `rule_database` for a backup rule's database, or the
    /// daemon's other errors.
    pub async fn release(&self, name: &str, options: ReleaseOptions) -> Result<ReleaseResult> {
        let body = Body::new()
            .text("name", name)
            .optional_text("durability", options.durability.as_ref().map(Durability::as_str))
            .flag("offload", options.offload)
            .millis("timeout_millis", options.timeout);
        self.client.call(Call::post("/v1/databases/release", body).with_timeout(options.timeout)).await
    }
}

/// Where [`Databases::all`] is: the next page's cursor, `None` once the
/// last page was read, and the databases not yet yielded.
struct PagesState {
    after: Option<String>,
    buffered: VecDeque<DatabaseStatus>,
}

/// Where [`Databases::all_versions`] is.
struct VersionsState {
    name: String,
    after: i64,
    remaining: Option<u32>,
    buffered: VecDeque<RestorableVersion>,
    done: bool,
}

/// Reads a listing's stream to its end, stopping at its first error.
pub(crate) async fn read_listing<T>(stream: impl Stream<Item = Result<T>>) -> Result<Vec<T>> {
    use futures_util::StreamExt as _;
    let mut stream = std::pin::pin!(stream);
    let mut items = Vec::new();
    while let Some(item) = stream.next().await {
        items.push(item?);
    }
    Ok(items)
}
