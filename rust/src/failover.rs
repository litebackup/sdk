//! Failovers: one database on several identical machines, and the follower
//! that tracks this machine's role.

use std::path::Path;
use std::pin::Pin;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::task::{Context, Poll};
use std::time::Duration;

use futures_core::Stream;
use tokio::sync::mpsc;
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

use crate::client::Client;
use crate::error::{Error, ErrorCode, Result};
use crate::models::{DatabaseState, DatabaseStatus, Event, EventList, EventType, FailoverStatus, Role, Writer};
use crate::options::option_setters;
use crate::wire::{self, Body, Call, FIRST_RETRY_DELAY, MAXIMUM_RETRY_DELAY};
use crate::{EventsOptions, StopBackupResult};

/// How many events one failover long-poll reads.
const EVENT_PAGE: u32 = 1000;
/// How long one failover long-poll waits; shorter than
/// [`ROLE_CONFIRMATION_LIMIT`], so a daemon that answers confirms the role
/// in time.
const FAILOVER_EVENT_WAIT: Duration = Duration::from_secs(10);
/// The longest a failover keeps reporting a role the daemon has not
/// confirmed: 15 s, the default client heartbeat interval and a quarter of
/// the default 60 s session lease another machine waits out before it takes
/// over.
const ROLE_CONFIRMATION_LIMIT: Duration = Duration::from_secs(15);

impl Role {
    /// `unknown`: a failover's role while its daemon does not answer, from a
    /// draining or denied event until the role is read again, and once the
    /// failover stopped or its client closed. The SDK sets it; the daemon
    /// never reports it.
    pub const UNKNOWN: Self = Self(std::borrow::Cow::Borrowed("unknown"));
}

/// Options of [`Failovers::start`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct FailoverOptions {
    /// The database name; the file name when unset.
    pub name: Option<String>,
    /// Bounds the first download of a replica.
    pub timeout: Option<Duration>,
    /// Replaces a file at the path that differs from the server head with the
    /// head, discarding its writes the head lacks. Without it such a file is
    /// refused with `conflict` and left untouched.
    pub discard_local: bool,
}

option_setters!(FailoverOptions {
    name: impl Into<String> => Some(name.into()),
    timeout: Duration => Some(timeout),
    discard_local: bool => discard_local,
});

/// What a failover's follower reports, in the order it happened.
#[derive(Debug)]
#[non_exhaustive]
pub enum FailoverNotice {
    /// This machine's role: first the role the start answered, then every
    /// change, [`Role::UNKNOWN`] included when the daemon stops answering.
    Role(Role),
    /// The daemon refused this machine's file and the failover stays
    /// conflicted there, the file as it is: a file that differs from the
    /// server head when this machine starts as the origin, or one holding
    /// writes the new origin never saw when it lost the origin role. The
    /// message is the daemon's.
    Denied(String),
    /// Reading the daemon's events failed; the follower tries again. An
    /// expired cursor (`cursor_expired`) is reported too, and following
    /// resumes at the log's end after reading the role again.
    Error(Error),
}

/// A failover's notices, in order, as a stream: see [`Failover::notices`].
#[derive(Debug)]
pub struct FailoverNotices {
    receiver: mpsc::UnboundedReceiver<FailoverNotice>,
}

impl Stream for FailoverNotices {
    type Item = FailoverNotice;

    fn poll_next(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Option<FailoverNotice>> {
        self.receiver.poll_recv(context)
    }
}

/// Failovers; [`Client::failovers`].
#[derive(Debug, Clone, Copy)]
pub struct Failovers<'c> {
    client: &'c Client,
}

impl<'c> Failovers<'c> {
    pub(crate) fn new(client: &'c Client) -> Self {
        Self { client }
    }

    /// Starts a failover of the database at `path`, the same path on every
    /// machine, and returns once this machine is the origin or holds a
    /// replica. A follower task then tracks the role.
    ///
    /// # Errors
    ///
    /// `invalid_argument` for an empty path, and `conflict` for a local file
    /// that differs from the server head without `discard_local`.
    #[cfg_attr(feature = "tracing", tracing::instrument(name = "litebackup.failover_start", skip_all))]
    pub async fn start(&self, path: impl AsRef<Path>, options: FailoverOptions) -> Result<Failover> {
        let path = path.as_ref();
        if path.as_os_str().is_empty() {
            return Err(Error::invalid_argument("failover needs a path"));
        }
        let absolute = wire::absolute_path(path)?;
        // Events after the log's end as of now are this failover's, including
        // those the start itself records.
        let end = self.client.events().page(EventsOptions::default().after(-1)).await?;
        let body = Body::new()
            .text("path", &absolute)
            .optional_text("name", options.name.as_deref())
            .millis("timeout_millis", options.timeout)
            .flag("discard_local", options.discard_local);
        let started: FailoverStatus =
            self.client.call(Call::post("/v1/failovers/start", body).with_timeout(options.timeout)).await?;
        Ok(Failover::follow(self.client, started, end.next))
    }
}

/// A database run on several identical machines: one origin backs it up,
/// and the others keep live readonly replicas at the same path. Each replica
/// asks for the write lock about once a second, so one of them takes over
/// once the origin's lease runs out.
///
/// ```no_run
/// # async fn example(client: &litebackup::Client) -> litebackup::Result<()> {
/// use futures_util::StreamExt as _;
/// use litebackup::{FailoverNotice, FailoverOptions};
///
/// let failover = client.failovers().start("/var/lib/app/main.sqlite", FailoverOptions::default()).await?;
/// let mut notices = failover.notices().expect("the first call hands out the notices");
/// while let Some(notice) = notices.next().await {
///     if let FailoverNotice::Role(role) = notice {
///         println!("this machine is now {role}");
///     }
/// }
/// # Ok(())
/// # }
/// ```
#[derive(Debug, Clone)]
pub struct Failover {
    shared: Arc<FailoverShared>,
}

#[derive(Debug)]
struct FailoverShared {
    client: Client,
    name: String,
    path: String,
    state: Mutex<FailoverState>,
    notices: Mutex<Option<mpsc::UnboundedReceiver<FailoverNotice>>>,
    sender: mpsc::UnboundedSender<FailoverNotice>,
    /// Cancelled by stop and by the client's close.
    stop: CancellationToken,
    follower: Mutex<Option<tokio::task::JoinHandle<()>>>,
}

#[derive(Debug)]
struct FailoverState {
    role: Role,
    /// When the daemon last confirmed the role.
    confirmed: Instant,
    stopped: bool,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

impl Failover {
    fn follow(client: &Client, started: FailoverStatus, cursor: i64) -> Self {
        let (sender, receiver) = mpsc::unbounded_channel();
        let shared = Arc::new(FailoverShared {
            client: client.clone(),
            name: started.name,
            path: started.path,
            state: Mutex::new(FailoverState { role: started.role, confirmed: Instant::now(), stopped: false }),
            notices: Mutex::new(Some(receiver)),
            sender,
            stop: client.lifetime().child_token(),
            follower: Mutex::new(None),
        });
        let follower = tokio::spawn(Arc::clone(&shared).follow(cursor));
        *lock(&shared.follower) = Some(follower);
        Self { shared }
    }

    /// The database's name.
    #[must_use]
    pub fn name(&self) -> &str {
        &self.shared.name
    }

    /// The database's file on this machine.
    #[must_use]
    pub fn path(&self) -> &str {
        &self.shared.path
    }

    /// This machine's role now: [`Role::ORIGIN`], [`Role::REPLICA`], or
    /// [`Role::UNKNOWN`] while the daemon does not answer, once the role went
    /// 15 s without the daemon confirming it, and once the failover stopped or
    /// its client closed.
    #[must_use]
    pub fn role(&self) -> Role {
        let state = lock(&self.shared.state);
        if state.stopped || self.shared.client.is_closed() || state.confirmed.elapsed() >= ROLE_CONFIRMATION_LIMIT {
            return Role::UNKNOWN;
        }
        state.role.clone()
    }

    /// Whether this machine writes the database now; false while the role
    /// is unknown.
    #[must_use]
    pub fn is_origin(&self) -> bool {
        self.role() == Role::ORIGIN
    }

    /// The follower's notices, in order, from the role the start answered;
    /// `None` after the first call, which hands them out. Notices wait in the
    /// stream until it is read; dropping it does not stop following.
    #[must_use]
    pub fn notices(&self) -> Option<FailoverNotices> {
        lock(&self.shared.notices).take().map(|receiver| FailoverNotices { receiver })
    }

    /// Which machine writes the database and how long its lease runs.
    ///
    /// # Errors
    ///
    /// The daemon's error, or `closed` after the client closed.
    pub async fn writer(&self) -> Result<Writer> {
        self.shared.client.databases().writer(&self.shared.name).await
    }

    /// Stops following and stops the failover on the daemon, releasing its
    /// write lock. The file stays, and the role is [`Role::UNKNOWN`] from then
    /// on; no notice follows once this returns.
    ///
    /// # Errors
    ///
    /// The daemon's error stopping the failover.
    pub async fn stop(&self) -> Result<StopBackupResult> {
        self.shared.stop_following();
        let follower = lock(&self.shared.follower).take();
        if let Some(follower) = follower {
            let _ = follower.await;
        }
        self.shared.client.backups().stop(&self.shared.name).await
    }
}

/// What the follower carries from one read to the next.
struct FollowProgress {
    /// The newest event ID handed to the failover.
    cursor: i64,
    /// The backoff after the next failed read.
    delay: Duration,
    /// When the daemon last confirmed the role: the start, a long-poll, or a
    /// role read answered.
    confirmed: Instant,
    /// The role is read before the next long-poll: after an expired cursor,
    /// and after the role became unknown.
    role_read_pending: bool,
    /// The log's end is read before that role read.
    log_end_pending: bool,
    /// The log's end read before the last role read: role changes up to it
    /// are older than that read and are skipped.
    role_read_log_end: i64,
}

/// The role a status read reports: unknown while the daemon drains the
/// origin, as after a draining event, and while the failover's file is
/// refused (conflicted), as after a denied event.
fn status_role(status: &DatabaseStatus) -> Role {
    if status.draining == Some(true) || status.state == DatabaseState::CONFLICTED {
        return Role::UNKNOWN;
    }
    status.role.clone()
}

/// Whether an event type sets a failover's role: `role_changed`, and
/// `draining` and `refused`, which make it unknown.
fn changes_role(event_type: &EventType) -> bool {
    *event_type == EventType::ROLE_CHANGED || *event_type == EventType::DRAINING || *event_type == EventType::DENIED
}

/// Whether a read failed without an answer from the daemon: an error this
/// SDK raised itself, with status 0.
fn unanswered(error: &Error) -> bool {
    error.status() == 0
}

impl FailoverShared {
    /// The role as the follower last set it, without the confirmation limit.
    fn current_role(&self) -> Role {
        lock(&self.state).role.clone()
    }

    fn confirm(&self, progress: &mut FollowProgress) {
        progress.confirmed = Instant::now();
        lock(&self.state).confirmed = progress.confirmed;
    }

    /// Stops following: no notice after this, and the role is unknown.
    fn stop_following(&self) {
        let mut state = lock(&self.state);
        state.stopped = true;
        state.role = Role::UNKNOWN;
        drop(state);
        self.stop.cancel();
    }

    /// Sends a notice unless the failover stopped or its client closed.
    fn notify(&self, notice: FailoverNotice) {
        let state = lock(&self.state);
        if !state.stopped && !self.stop.is_cancelled() {
            // A dropped stream hears nothing more; following goes on.
            let _ = self.sender.send(notice);
        }
    }

    /// Saves a role and reports it when it differs; whether it changed.
    fn change_role(&self, role: Role) -> bool {
        let mut state = lock(&self.state);
        let changed = state.role != role && !state.stopped;
        if changed {
            state.role = role.clone();
            drop(state);
            self.notify(FailoverNotice::Role(role));
        }
        changed
    }

    /// Makes the role unknown without a notice, as a closed client does.
    fn clear_role(&self) {
        lock(&self.state).role = Role::UNKNOWN;
    }

    /// Makes the role unknown and, when that changed it, has the follower read
    /// the log's end and the role once the daemon answers again.
    fn lose_role(&self, progress: &mut FollowProgress) {
        if self.change_role(Role::UNKNOWN) {
            progress.role_read_pending = true;
            progress.log_end_pending = true;
        }
    }

    /// Long-polls the daemon's events after the cursor until stopped, applying
    /// this failover's events. It first reports the role the start answered.
    ///
    /// An expired cursor does not end following: it moves to the log's end,
    /// and the role is read again before the next long-poll. A read the
    /// daemon did not answer (status 0) makes the role unknown at once, and
    /// so does no confirmation within 15 s, or failed reads the daemon
    /// answered for that long. Once the daemon answers again, the follower
    /// reads the log's end, then the role, then the log's end again and the
    /// events up to it: when one changed the role, the role read may predate
    /// it, so it reads the role again.
    async fn follow(self: Arc<Self>, cursor: i64) {
        self.notify(FailoverNotice::Role(self.current_role()));
        let mut progress = FollowProgress {
            cursor,
            delay: FIRST_RETRY_DELAY,
            confirmed: lock(&self.state).confirmed,
            role_read_pending: false,
            log_end_pending: false,
            role_read_log_end: 0,
        };
        while !self.stop.is_cancelled() {
            match self.read_daemon(&mut progress).await {
                Ok(()) => progress.delay = FIRST_RETRY_DELAY,
                Err(error) => {
                    if !self.retry_after_error(error, &mut progress).await {
                        return;
                    }
                }
            }
        }
    }

    /// Runs one read with a deadline 15 s after the last confirmation, or
    /// after now once the role is unknown; one the deadline ends is `timeout`
    /// with status 0.
    async fn deadline_read<T>(&self, progress: &FollowProgress, read: impl Future<Output = Result<T>>) -> Result<T> {
        let start = if self.current_role() == Role::UNKNOWN { Instant::now() } else { progress.confirmed };
        let deadline = start + ROLE_CONFIRMATION_LIMIT;
        tokio::select! {
            biased;
            () = self.stop.cancelled() => Err(Error::closed()),
            answer = tokio::time::timeout_at(deadline, read) => answer.unwrap_or_else(|_| {
                Err(Error::new(
                    ErrorCode::TIMEOUT,
                    format!(
                        "the daemon did not answer within {} s, so the failover's role is unknown",
                        ROLE_CONFIRMATION_LIMIT.as_secs()
                    ),
                ))
            }),
        }
    }

    async fn log_end(&self, progress: &FollowProgress) -> Result<EventList> {
        self.deadline_read(progress, self.client.events().page(EventsOptions::default().after(-1))).await
    }

    /// Makes the follower's next read: the log's end or the role when a role
    /// read is pending, otherwise one long-poll.
    async fn read_daemon(&self, progress: &mut FollowProgress) -> Result<()> {
        if self.current_role() != Role::UNKNOWN && progress.confirmed.elapsed() >= ROLE_CONFIRMATION_LIMIT {
            self.lose_role(progress);
        }
        if progress.role_read_pending {
            if progress.log_end_pending {
                progress.role_read_log_end = self.log_end(progress).await?.next;
                progress.log_end_pending = false;
            }
            let status = self.deadline_read(progress, self.client.databases().get(&self.name)).await?;
            if self.role_superseded(progress).await? {
                // The role read may predate a role change the log now holds:
                // read the role again, then look past the new log end.
                return Ok(());
            }
            self.confirm(progress);
            progress.role_read_pending = false;
            if !self.stop.is_cancelled() {
                self.change_role(status_role(&status));
            }
            return Ok(());
        }
        let options = EventsOptions::default().after(progress.cursor).wait(FAILOVER_EVENT_WAIT).limit(EVENT_PAGE);
        let page = match self.deadline_read(progress, self.client.events().page(options)).await {
            Err(error) if error.code() == ErrorCode::CURSOR_EXPIRED && !self.stop.is_cancelled() => {
                self.notify(FailoverNotice::Error(error));
                let end = self.log_end(progress).await?;
                progress.cursor = end.next;
                progress.role_read_log_end = end.next;
                progress.role_read_pending = true;
                progress.log_end_pending = false;
                return Ok(());
            }
            answer => answer?,
        };
        self.confirm(progress);
        let mut role_events = 0;
        for event in &page.events {
            progress.cursor = progress.cursor.max(event.id);
            let older_than_role_read = changes_role(&event.r#type) && event.id <= progress.role_read_log_end;
            if event.name.as_deref() == Some(self.name.as_str()) && !self.stop.is_cancelled() {
                self.apply_event(event, older_than_role_read);
                if changes_role(&event.r#type) && !older_than_role_read {
                    role_events += 1;
                }
            }
        }
        progress.cursor = progress.cursor.max(page.next);
        if role_events == 0 && self.current_role() == Role::UNKNOWN {
            // A role unknown since a draining event is read again after every
            // long-poll that brought no role change: a drain that failed says
            // nothing more, and its status shows the role once it stopped.
            progress.role_read_pending = true;
            progress.log_end_pending = true;
        }
        Ok(())
    }

    /// Reads the log's end as of now, then the events from the last role
    /// read's log end through it, without waiting, and reports whether one
    /// changed this database's role, so a role read that raced it is not
    /// trusted. When pruning passed that log end, it reports true with the
    /// log end pending, so the follower starts again from a fresh one.
    async fn role_superseded(&self, progress: &mut FollowProgress) -> Result<bool> {
        let end = self.log_end(progress).await?;
        let mut superseded = false;
        while progress.role_read_log_end < end.next {
            let options = EventsOptions::default().after(progress.role_read_log_end).limit(EVENT_PAGE);
            let page = match self.deadline_read(progress, self.client.events().page(options)).await {
                Err(error) if error.code() == ErrorCode::CURSOR_EXPIRED => {
                    progress.log_end_pending = true;
                    return Ok(true);
                }
                answer => answer?,
            };
            for event in page.events.iter().take_while(|event| event.id <= end.next) {
                if event.name.as_deref() == Some(self.name.as_str()) && changes_role(&event.r#type) {
                    superseded = true;
                }
            }
            let Some(last) = page.events.last() else {
                break;
            };
            // Never past the end: a role change after it came after the role
            // read, and the long-poll applies it.
            progress.role_read_log_end = progress.role_read_log_end.max(last.id.min(end.next));
        }
        progress.role_read_log_end = progress.role_read_log_end.max(end.next);
        Ok(superseded)
    }

    /// Applies one event about this failover's database. An event older than
    /// the last role read changes no role, because that read holds it; a
    /// refusal among them is still reported.
    fn apply_event(&self, event: &Event, older_than_role_read: bool) {
        if older_than_role_read && event.r#type != EventType::DENIED {
            return;
        }
        if event.r#type == EventType::ROLE_CHANGED {
            let role = event.role.as_deref().filter(|role| !role.is_empty()).unwrap_or(&event.message);
            if !role.is_empty() {
                self.change_role(Role::new(role));
            }
        } else if event.r#type == EventType::DRAINING {
            // The daemon drains: it stops being the origin before it releases
            // the lock, so the app stops writing while no machine may take
            // over yet.
            self.change_role(Role::UNKNOWN);
        } else if event.r#type == EventType::DENIED {
            if !older_than_role_read {
                self.change_role(Role::UNKNOWN);
            }
            self.notify(FailoverNotice::Denied(event.message.clone()));
        }
    }

    /// Makes the role unknown when the daemon did not answer or following
    /// ends, reports the failed read, and waits out the backoff, doubling it;
    /// while the role is known, the wait ends when the confirmation limit
    /// passes. Returns false when following ends: stopped, or `closed` or
    /// `sdk_unsupported`.
    async fn retry_after_error(&self, error: Error, progress: &mut FollowProgress) -> bool {
        if self.stop.is_cancelled() {
            // Stopped, or the client closed: no role notice.
            self.clear_role();
            return false;
        }
        let closed = error.code() == ErrorCode::CLOSED;
        let ending = closed || error.code() == ErrorCode::SDK_UNSUPPORTED;
        if closed {
            self.clear_role();
        } else if ending || unanswered(&error) {
            self.lose_role(progress);
        }
        self.notify(FailoverNotice::Error(error));
        if ending {
            return false;
        }
        let mut delay = progress.delay;
        if self.current_role() != Role::UNKNOWN {
            let confirmation_left =
                (progress.confirmed + ROLE_CONFIRMATION_LIMIT).saturating_duration_since(Instant::now());
            delay = delay.min(confirmation_left);
        }
        tokio::select! {
            () = self.stop.cancelled() => {
                self.clear_role();
                return false;
            }
            () = tokio::time::sleep(delay) => {}
        }
        progress.delay = (progress.delay * 2).min(MAXIMUM_RETRY_DELAY);
        true
    }
}
