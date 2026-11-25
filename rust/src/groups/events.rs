//! `client.events()` and `client.session()`.

use std::collections::VecDeque;
use std::time::Duration;

use futures_core::Stream;

use crate::client::Client;
use crate::error::{Error, Result};
use crate::groups::databases::PageOptions;
use crate::models::{Durability, Event, EventList, ShutdownResult, Status};
use crate::options::option_setters;
use crate::wire::{self, Body, Call, EVENT_WAIT, FIRST_RETRY_DELAY, MAXIMUM_RETRY_DELAY};

/// Selects a page of events: [`Events::page`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct EventsOptions {
    /// Returns events with IDs greater than this one; -1 returns none and,
    /// as `next`, where the log ends, to follow events from now on.
    pub after: i64,
    /// Waits up to this long for the first new event.
    pub wait: Option<Duration>,
    /// Returns at most this many events.
    pub limit: Option<u32>,
}

option_setters!(EventsOptions {
    after: i64 => after,
    wait: Duration => Some(wait),
    limit: u32 => Some(limit),
});

/// Where [`Events::watch`] starts and how long each long-poll waits.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct WatchOptions {
    /// Follows events after this ID; -1 starts at the end of the log.
    pub after: i64,
    /// How long each long-poll waits; 30 seconds when unset.
    pub wait: Option<Duration>,
}

option_setters!(WatchOptions {
    after: i64 => after,
    wait: Duration => Some(wait),
});

/// Options of [`Session::shutdown`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct ShutdownOptions {
    /// How far every final sync must reach: server, cloud, or
    /// [`Durability::NONE`] to return without waiting.
    pub durability: Option<Durability>,
    /// Bounds the shutdown.
    pub timeout: Option<Duration>,
}

option_setters!(ShutdownOptions {
    durability: Durability => Some(durability),
    timeout: Duration => Some(timeout),
});

/// The daemon's event log; [`Client::events`].
#[derive(Debug, Clone, Copy)]
pub struct Events<'c> {
    client: &'c Client,
}

impl<'c> Events<'c> {
    pub(crate) fn new(client: &'c Client) -> Self {
        Self { client }
    }

    /// One page of events, oldest first, waiting up to `wait` for the first
    /// new one.
    ///
    /// # Errors
    ///
    /// `cursor_expired` for a cursor older than the oldest event the daemon
    /// keeps.
    pub async fn page(&self, options: EventsOptions) -> Result<EventList> {
        let mut query = Vec::new();
        if options.after != 0 {
            query.push(("after", options.after.to_string()));
        }
        let wait = options.wait.filter(|wait| !wait.is_zero());
        if let Some(wait) = wait {
            query.push(("wait_millis", wire::millis(wait).to_string()));
        }
        if let Some(limit) = options.limit.filter(|limit| *limit > 0) {
            query.push(("limit", limit.to_string()));
        }
        self.client.call(Call::get("/v1/events/list", query).with_timeout(wait)).await
    }

    /// Events, oldest first, by long-polling as the stream is read, until
    /// the caller drops it. A failed long-poll is tried again from the same
    /// cursor, with backoff from 250 ms up to 10 s; `sdk_unsupported`,
    /// `closed`, `invalid_argument`, and `cursor_expired` (the cursor is
    /// older than the oldest event the daemon keeps; choose a new one, -1 for
    /// the log's end) end the stream with that error.
    #[allow(clippy::needless_pass_by_value, reason = "every call takes its options by value")]
    pub fn watch(&self, options: WatchOptions) -> impl Stream<Item = Result<Event>> + Send + 'c {
        let client = self.client;
        let wait = options.wait.filter(|wait| !wait.is_zero()).unwrap_or(EVENT_WAIT);
        let watch =
            WatchState { cursor: options.after, buffered: VecDeque::new(), delay: FIRST_RETRY_DELAY, ended: false };
        futures_util::stream::unfold(watch, move |mut watch| async move {
            loop {
                if let Some(event) = watch.buffered.pop_front() {
                    return Some((Ok(event), watch));
                }
                if watch.ended {
                    return None;
                }
                let page = Events::new(client).page(EventsOptions::default().after(watch.cursor).wait(wait)).await;
                match page {
                    Ok(page) => {
                        watch.delay = FIRST_RETRY_DELAY;
                        for event in &page.events {
                            watch.cursor = watch.cursor.max(event.id);
                        }
                        watch.cursor = watch.cursor.max(page.next);
                        watch.buffered.extend(page.events);
                    }
                    Err(error) if wire::ends_watch(&error) => {
                        watch.ended = true;
                        return Some((Err(error), watch));
                    }
                    Err(error) => {
                        report_retry(&error);
                        tokio::time::sleep(watch.delay).await;
                        watch.delay = (watch.delay * 2).min(MAXIMUM_RETRY_DELAY);
                    }
                }
            }
        })
    }
}

/// Where [`Events::watch`] is.
struct WatchState {
    cursor: i64,
    buffered: VecDeque<Event>,
    delay: Duration,
    ended: bool,
}

/// Reports a long-poll a watch tries again, through `tracing` when enabled.
fn report_retry(error: &Error) {
    #[cfg(feature = "tracing")]
    tracing::warn!(code = error.code(), message = error.message(), "an event long-poll failed; trying again");
    #[cfg(not(feature = "tracing"))]
    let _ = error;
}

/// The session; [`Client::session`].
#[derive(Debug, Clone, Copy)]
pub struct Session<'c> {
    client: &'c Client,
}

impl<'c> Session<'c> {
    pub(crate) fn new(client: &'c Client) -> Self {
        Self { client }
    }

    /// The session, its server, and one page of the databases on this machine.
    ///
    /// # Errors
    ///
    /// `invalid_argument` for a limit outside 1 to 1000.
    pub async fn status(&self, options: PageOptions) -> Result<Status> {
        let query = vec![("after", options.after.unwrap_or_default()), ("limit", wire::page_limit(options.limit)?)];
        self.client.call(Call::get("/v1/status/show", query)).await
    }

    /// Syncs every database a final time, releases every write lock, ends the
    /// session, and stops the daemon. Each database it could not sync or
    /// release is in `failures`, with its code, and keeps any write lock it
    /// holds; the session and the daemon then stay. An empty `failures` is the
    /// only complete shutdown. [`Client::close`] never does this.
    ///
    /// # Errors
    ///
    /// The daemon's error, or `timeout` past the timeout.
    pub async fn shutdown(&self, options: ShutdownOptions) -> Result<ShutdownResult> {
        let body = Body::new()
            .optional_text("durability", options.durability.as_ref().map(Durability::as_str))
            .millis("timeout_millis", options.timeout);
        self.client.call(Call::post("/v1/session/shutdown", body).with_timeout(options.timeout)).await
    }
}
