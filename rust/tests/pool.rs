//! Pools beyond one lease: reuse, lost locks, durability waits, the cleanup
//! timeout, idle release, panics and dropped calls, a pool dropped without
//! close, and closes that cannot confirm.
#![allow(clippy::expect_used, clippy::unwrap_used, reason = "tests assert with expect")]

mod support;

use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use litebackup::{Adapter, BoxError, CloseError, Durability, ErrorCode, LeaseError, LeaseOptions, PoolOptions};
use serde_json::{Value, json};
use support::{Answer, FakeDaemon, client_to};

const NAME: &str = "agents/42.sqlite";

/// How the test adapter closes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Closing {
    Succeeds,
    FailsWithTheConnection,
    FailsUnknown,
}

/// An adapter that counts opens and closes and takes `close_time` to close.
#[derive(Clone)]
struct CountingAdapter {
    opened: Arc<AtomicUsize>,
    closed: Arc<AtomicUsize>,
    closing: Arc<Mutex<Closing>>,
    close_time: Duration,
}

impl CountingAdapter {
    fn new() -> Self {
        Self {
            opened: Arc::default(),
            closed: Arc::default(),
            closing: Arc::new(Mutex::new(Closing::Succeeds)),
            close_time: Duration::ZERO,
        }
    }

    fn close_as(&self, closing: Closing) {
        *self.closing.lock().unwrap() = closing;
    }
}

impl Adapter for CountingAdapter {
    type Connection = u32;

    fn open(&self, _path: &Path) -> impl Future<Output = Result<u32, BoxError>> + Send {
        std::future::ready(Ok(u32::try_from(self.opened.fetch_add(1, Ordering::SeqCst)).unwrap()))
    }

    async fn close(&self, connection: u32) -> Result<(), CloseError<u32>> {
        tokio::time::sleep(self.close_time).await;
        let closing = *self.closing.lock().unwrap();
        match closing {
            Closing::Succeeds => {
                self.closed.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }
            Closing::FailsWithTheConnection => Err(CloseError::retry("the statement is still open", connection)),
            Closing::FailsUnknown => Err(CloseError::unknown("the driver lost track")),
        }
    }
}

fn database_status(acquired: bool, server: i64, cloud: i64) -> Value {
    json!({"name": NAME, "path": "/var/lib/app/agents/42.sqlite", "role": "origin", "state": "synced",
           "acquired": acquired, "synced_server": {"version": server}, "synced_cloud": {"version": cloud}})
}

/// A daemon that acquires, syncs, reads, and releases successfully.
fn leasing_daemon() -> FakeDaemon {
    FakeDaemon::start(vec![
        Answer::json("/v1/databases/acquire", &json!({"database": database_status(true, 3, 3), "decision": "created"})),
        Answer::json("/v1/databases/sync", &database_status(true, 3, 3)),
        Answer::json("/v1/databases/status", &database_status(true, 3, 3)),
        Answer::json("/v1/databases/release", &json!({"database": database_status(false, 3, 3)})),
    ])
}

fn paths(daemon: &FakeDaemon) -> Vec<String> {
    daemon.requests().iter().map(|request| request.path.trim_start_matches("/v1/databases/").to_owned()).collect()
}

/// The release bodies without `timeout_millis`, which real time shortens.
fn releases_without_timeout(daemon: &FakeDaemon) -> Vec<Value> {
    release_bodies(daemon)
        .into_iter()
        .map(|mut body| {
            body.as_object_mut().expect("a JSON object").remove("timeout_millis");
            body
        })
        .collect()
}

fn release_bodies(daemon: &FakeDaemon) -> Vec<Value> {
    daemon
        .requests()
        .into_iter()
        .filter(|request| request.path == "/v1/databases/release")
        .map(|request| request.body)
        .collect()
}

async fn lease_once(pool: &litebackup::Pool<CountingAdapter>, options: LeaseOptions) -> Result<(), LeaseError<String>> {
    pool.lease(NAME, options, async |_connection| Ok(())).await
}

#[tokio::test]
async fn a_lease_keeps_its_lock_and_connection_between_calls() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let adapter = CountingAdapter::new();
    let pool = client.databases().pool(adapter.clone(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    assert_eq!(paths(&daemon), ["acquire", "status"]);
    assert_eq!(adapter.opened.load(Ordering::SeqCst), 1);
    pool.close().await.unwrap();
    assert_eq!(paths(&daemon), ["acquire", "status", "release"]);
    assert_eq!(adapter.closed.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn a_lost_lock_fails_the_call_and_the_next_one_acquires_again() {
    let daemon = FakeDaemon::start(vec![
        Answer::json("/v1/databases/acquire", &json!({"database": database_status(true, 3, 3)})),
        Answer::json("/v1/databases/status", &database_status(false, 3, 3)).uses(1),
        Answer::json("/v1/databases/release", &json!({})),
    ]);
    let client = client_to(&daemon).await;
    let adapter = CountingAdapter::new();
    let pool = client.databases().pool(adapter.clone(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    let error = lease_once(&pool, LeaseOptions::default()).await.expect_err("the lock was lost");
    assert_eq!(error.code(), Some(ErrorCode::LOCK_LOST));
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    assert_eq!(paths(&daemon), ["acquire", "status", "acquire"]);
    assert_eq!(adapter.opened.load(Ordering::SeqCst), 2);
    pool.close().await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn cloud_durability_waits_until_the_bucket_holds_the_version() {
    let daemon = FakeDaemon::start(vec![
        Answer::json("/v1/databases/acquire", &json!({"database": database_status(true, 3, 2)})),
        Answer::json("/v1/databases/sync", &database_status(true, 5, 3)),
        Answer::json("/v1/databases/status", &database_status(true, 5, 4)).uses(2),
        Answer::json("/v1/databases/status", &database_status(true, 5, 5)),
        Answer::json("/v1/databases/release", &json!({})),
    ]);
    let client = client_to(&daemon).await;
    let pool = client.databases().pool(CountingAdapter::new(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default().durability(Durability::CLOUD)).await.unwrap();
    assert_eq!(paths(&daemon), ["acquire", "sync", "status", "status", "status"]);
    pool.close().await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn a_slow_close_shortens_the_release_timeout_to_the_cleanup_time_left() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let mut adapter = CountingAdapter::new();
    adapter.close_time = Duration::from_secs(4);
    let options = PoolOptions::default().cleanup_timeout(Duration::from_secs(10));
    let pool = client.databases().pool(adapter, options).unwrap();
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    pool.close().await.unwrap();
    assert_eq!(release_bodies(&daemon), [json!({"name": NAME, "durability": "server", "timeout_millis": 6000})]);
}

#[tokio::test(start_paused = true)]
async fn an_idle_lease_is_released_with_its_offload() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let pool = client.databases().pool(CountingAdapter::new(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default().offload(true)).await.unwrap();
    tokio::time::sleep(Duration::from_secs(50)).await;
    assert_eq!(release_bodies(&daemon).len(), 0, "released before its idle release");
    tokio::time::sleep(Duration::from_secs(30)).await;
    assert_eq!(
        release_bodies(&daemon),
        [json!({"name": NAME, "durability": "server", "offload": true, "timeout_millis": 600_000})]
    );
    pool.close().await.unwrap();
    assert_eq!(release_bodies(&daemon).len(), 1);
}

#[tokio::test]
async fn a_panicking_call_keeps_the_lease_and_offloads_nothing() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let pool = client.databases().pool(CountingAdapter::new(), PoolOptions::default()).unwrap();
    let panicking = pool.clone();
    let panicked = tokio::spawn(async move {
        let options = LeaseOptions::default().offload(true);
        panicking
            .lease(NAME, options, async |_connection| -> Result<(), String> {
                tokio::task::yield_now().await;
                panic!("the work panics after it suspended")
            })
            .await
    })
    .await;
    assert!(panicked.is_err_and(|failure| failure.is_panic()));
    pool.close().await.unwrap();
    assert_eq!(releases_without_timeout(&daemon), [json!({"name": NAME, "durability": "server"})]);
}

#[tokio::test]
async fn a_call_dropped_while_its_work_waits_offloads_nothing() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let pool = client.databases().pool(CountingAdapter::new(), PoolOptions::default()).unwrap();
    let started = Arc::new(tokio::sync::Notify::new());
    let work_started = Arc::clone(&started);
    let options = LeaseOptions::default().offload(true);
    let lease = pool.lease(NAME, options, async |_connection| -> Result<(), String> {
        work_started.notify_one();
        std::future::pending::<()>().await;
        Ok(())
    });
    tokio::select! {
        _ = lease => panic!("the work never ends"),
        () = started.notified() => {}
    }
    pool.close().await.unwrap();
    assert_eq!(releases_without_timeout(&daemon), [json!({"name": NAME, "durability": "server"})]);
}

#[tokio::test]
async fn a_pool_dropped_without_close_closes_then_releases_without_offload() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let adapter = CountingAdapter::new();
    let pool = client.databases().pool(adapter.clone(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default().offload(true)).await.unwrap();
    drop(pool);
    for _ in 0..200 {
        if !release_bodies(&daemon).is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(adapter.closed.load(Ordering::SeqCst), 1);
    let released = release_bodies(&daemon);
    assert_eq!(released.len(), 1);
    assert_eq!(released[0].get("offload"), None);
}

#[tokio::test]
async fn a_dropped_pool_whose_close_fails_releases_nothing() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let adapter = CountingAdapter::new();
    let pool = client.databases().pool(adapter.clone(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    adapter.close_as(Closing::FailsWithTheConnection);
    drop(pool);
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(release_bodies(&daemon).len(), 0);
}

#[tokio::test]
async fn a_close_that_keeps_the_connection_is_tried_again_by_release() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let adapter = CountingAdapter::new();
    let pool = client.databases().pool(adapter.clone(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    adapter.close_as(Closing::FailsWithTheConnection);
    let error = pool.release(NAME).await.expect_err("the close failed");
    assert_eq!(error.lease_cleanup().map(|cleanup| cleanup.step.as_str()), Some("close"));
    assert_eq!(error.details().get("acquired"), Some(&Value::Bool(true)));
    assert_eq!(release_bodies(&daemon).len(), 0);
    adapter.close_as(Closing::Succeeds);
    pool.release(NAME).await.unwrap();
    assert_eq!(release_bodies(&daemon).len(), 1);
    pool.close().await.unwrap();
}

#[tokio::test]
async fn after_a_close_that_could_not_tell_only_an_explicit_release_releases() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let adapter = CountingAdapter::new();
    let pool = client.databases().pool(adapter.clone(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    adapter.close_as(Closing::FailsUnknown);
    let error = pool.close().await.expect_err("the close failed");
    assert_eq!(error.errors.len(), 1);
    assert_eq!(error.errors[0].lease_cleanup().map(|cleanup| cleanup.step.as_str()), Some("close"));
    assert_eq!(release_bodies(&daemon).len(), 0);
    pool.release(NAME).await.unwrap();
    assert_eq!(release_bodies(&daemon).len(), 1);
}

#[tokio::test]
async fn a_lease_after_a_failed_release_reopens_its_connection() {
    let daemon = FakeDaemon::start(vec![
        Answer::json("/v1/databases/acquire", &json!({"database": database_status(true, 3, 3)})),
        Answer::status("/v1/databases/release", 503, &json!({"code": "unavailable", "message": "no server"})).uses(1),
        Answer::json("/v1/databases/status", &database_status(true, 3, 3)),
        Answer::json("/v1/databases/release", &json!({})),
    ]);
    let client = client_to(&daemon).await;
    let adapter = CountingAdapter::new();
    let pool = client.databases().pool(adapter.clone(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    let error = pool.release(NAME).await.expect_err("the release failed");
    assert_eq!(error.lease_cleanup().map(|cleanup| cleanup.step.as_str()), Some("release"));
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    assert_eq!(adapter.opened.load(Ordering::SeqCst), 2);
    assert_eq!(paths(&daemon), ["acquire", "release", "status"]);
    pool.close().await.unwrap();
}

#[tokio::test]
async fn close_awaits_a_running_call_and_denies_new_ones() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let pool = client.databases().pool(CountingAdapter::new(), PoolOptions::default()).unwrap();
    let (release_work, work_released) = tokio::sync::oneshot::channel::<()>();
    let started = Arc::new(tokio::sync::Notify::new());
    let running = {
        let pool = pool.clone();
        let started = Arc::clone(&started);
        tokio::spawn(async move {
            pool.lease(NAME, LeaseOptions::default(), async |_connection| -> Result<(), String> {
                started.notify_one();
                let _ = work_released.await;
                Ok(())
            })
            .await
        })
    };
    started.notified().await;
    let closing = {
        let pool = pool.clone();
        tokio::spawn(async move { pool.close().await })
    };
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(!closing.is_finished(), "close returned while a call ran");
    let denied = lease_once(&pool, LeaseOptions::default()).await.expect_err("the pool is closing");
    assert_eq!(denied.code(), Some(ErrorCode::CLOSED));
    release_work.send(()).unwrap();
    running.await.unwrap().unwrap();
    closing.await.unwrap().unwrap();
    assert_eq!(release_bodies(&daemon).len(), 1);
}

#[tokio::test]
async fn a_lease_denies_a_durability_besides_server_and_cloud() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let pool = client.databases().pool(CountingAdapter::new(), PoolOptions::default()).unwrap();
    let error =
        lease_once(&pool, LeaseOptions::default().durability(Durability::new("disk"))).await.expect_err("denied");
    assert_eq!(error.code(), Some(ErrorCode::INVALID_ARGUMENT));
    assert!(daemon.requests().is_empty());
}

/// An adapter whose opens wait until the test lets them finish.
#[derive(Clone)]
struct GatedAdapter {
    gate: Arc<tokio::sync::Semaphore>,
    open_started: Arc<tokio::sync::Notify>,
    closed: Arc<AtomicUsize>,
}

impl GatedAdapter {
    fn new() -> Self {
        Self { gate: Arc::new(tokio::sync::Semaphore::new(0)), open_started: Arc::default(), closed: Arc::default() }
    }
}

impl Adapter for GatedAdapter {
    type Connection = ();

    async fn open(&self, _path: &Path) -> Result<(), BoxError> {
        self.open_started.notify_one();
        let _permit = self.gate.acquire().await.map_err(|error| Box::new(error) as BoxError)?;
        Ok(())
    }

    fn close(&self, (): ()) -> impl Future<Output = Result<(), CloseError<()>>> + Send {
        self.closed.fetch_add(1, Ordering::SeqCst);
        std::future::ready(Ok(()))
    }
}

#[tokio::test]
async fn a_lease_dropped_while_it_opens_is_closed_before_it_is_released() {
    let daemon = leasing_daemon();
    let client = client_to(&daemon).await;
    let adapter = GatedAdapter::new();
    let pool = client.databases().pool(adapter.clone(), PoolOptions::default()).unwrap();
    let open_started = Arc::clone(&adapter.open_started);
    tokio::select! {
        _ = pool.lease(NAME, LeaseOptions::default(), async |()| -> Result<(), String> { Ok(()) }) => {
            panic!("the open finished before its gate opened")
        }
        () = open_started.notified() => {}
    }
    let closing = {
        let pool = pool.clone();
        tokio::spawn(async move { pool.close().await })
    };
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(release_bodies(&daemon).len(), 0, "released while the open still ran");
    adapter.gate.add_permits(1);
    closing.await.unwrap().unwrap();
    assert_eq!(adapter.closed.load(Ordering::SeqCst), 1, "what the open opened was not closed");
    assert_eq!(releases_without_timeout(&daemon), [json!({"name": NAME, "durability": "server"})]);
}

/// A daemon whose status reads of the retained lease wait a minute.
fn slow_status_daemon() -> FakeDaemon {
    FakeDaemon::start(vec![
        Answer::json("/v1/databases/acquire", &json!({"database": database_status(true, 3, 3)})),
        Answer::json("/v1/databases/status", &database_status(true, 3, 3)).delay(Duration::from_secs(60)),
        Answer::json("/v1/databases/release", &json!({})),
    ])
}

#[tokio::test]
async fn a_retained_lease_dropped_during_its_status_read_offloads_nothing() {
    let daemon = slow_status_daemon();
    let client = client_to(&daemon).await;
    let pool = client.databases().pool(CountingAdapter::new(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default().offload(true)).await.unwrap();
    let second_lease = lease_once(&pool, LeaseOptions::default().offload(true));
    assert!(tokio::time::timeout(Duration::from_millis(100), second_lease).await.is_err(), "the status read answered");
    pool.close().await.unwrap();
    assert_eq!(releases_without_timeout(&daemon), [json!({"name": NAME, "durability": "server"})]);
}

#[tokio::test]
async fn cancelling_during_the_status_read_ends_the_call_without_its_work() {
    let daemon = slow_status_daemon();
    let client = client_to(&daemon).await;
    let pool = client.databases().pool(CountingAdapter::new(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    let token = litebackup::CancellationToken::new();
    let cancelling = token.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(50)).await;
        cancelling.cancel();
    });
    let worked = Arc::new(AtomicUsize::new(0));
    let work_calls = Arc::clone(&worked);
    let options = LeaseOptions::default().cancellation(token);
    let cancelled = tokio::time::timeout(
        Duration::from_secs(5),
        pool.lease(NAME, options, async |_connection| -> Result<(), String> {
            work_calls.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }),
    )
    .await
    .expect("the cancellation ended the wait");
    assert_eq!(cancelled.expect_err("cancelled").code(), Some(ErrorCode::CANCELLED));
    assert_eq!(worked.load(Ordering::SeqCst), 0, "the work ran after the cancellation");
    pool.close().await.unwrap();
}

#[tokio::test]
async fn a_lost_lease_whose_close_failed_blocks_calls_until_a_close_succeeds() {
    let daemon = FakeDaemon::start(vec![
        Answer::json("/v1/databases/acquire", &json!({"database": database_status(true, 3, 3)})),
        Answer::json("/v1/databases/status", &database_status(false, 3, 3)),
        Answer::json("/v1/databases/release", &json!({})),
    ]);
    let client = client_to(&daemon).await;
    let adapter = CountingAdapter::new();
    let pool = client.databases().pool(adapter.clone(), PoolOptions::default()).unwrap();
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    adapter.close_as(Closing::FailsWithTheConnection);
    let lost = lease_once(&pool, LeaseOptions::default()).await.expect_err("the lock was lost");
    assert_eq!(lost.code(), Some(ErrorCode::LOCK_LOST));
    let blocked = lease_once(&pool, LeaseOptions::default()).await.expect_err("the lost connection is open");
    assert_eq!(blocked.code(), Some(ErrorCode::LEASE_CLEANUP_FAILED));
    let denied = pool.use_database(NAME, async |_connection| -> Result<(), String> { Ok(()) }).await;
    assert_eq!(denied.expect_err("no work on a lost lease").code(), Some(ErrorCode::LEASE_CLEANUP_FAILED));
    adapter.close_as(Closing::Succeeds);
    lease_once(&pool, LeaseOptions::default()).await.unwrap();
    assert_eq!(paths(&daemon).iter().filter(|path| *path == "acquire").count(), 2, "the next lease acquires again");
    pool.close().await.unwrap();
}

/// A daemon whose database is a rollback-journal one that a rule on this
/// machine offloads.
fn rule_daemon() -> FakeDaemon {
    let status = json!({"name": NAME, "path": "/var/lib/app/agents/42.sqlite", "role": "origin", "state": "synced",
                        "acquired": false, "failover": false, "backup_rule_id": "r1", "journal_mode": "rollback"});
    let rule =
        json!({"id": "r1", "path": "/var/lib/app", "maximum_local_databases": 2, "offload_rollback_journal": true});
    FakeDaemon::start(vec![
        Answer::json("/v1/databases/status", &status),
        Answer::json("/v1/backup-rules/list", &json!({"rules": [rule]})),
    ])
}

#[tokio::test]
async fn a_rollback_journal_database_closes_after_a_call_that_was_dropped() {
    let daemon = rule_daemon();
    let client = client_to(&daemon).await;
    let adapter = CountingAdapter::new();
    let pool = client.databases().pool(adapter.clone(), PoolOptions::default()).unwrap();
    pool.use_database(NAME, async |_connection| -> Result<(), String> { Ok(()) }).await.unwrap();
    assert_eq!(adapter.closed.load(Ordering::SeqCst), 1, "a finished call closes it");
    let started = Arc::new(tokio::sync::Notify::new());
    let work_started = Arc::clone(&started);
    tokio::select! {
        _ = pool.use_database(NAME, async |_connection| -> Result<(), String> {
            work_started.notify_one();
            std::future::pending::<()>().await;
            Ok(())
        }) => panic!("the work never ends"),
        () = started.notified() => {}
    }
    for _ in 0..100 {
        if adapter.closed.load(Ordering::SeqCst) == 2 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(adapter.closed.load(Ordering::SeqCst), 2, "the dropped call left the database open");
    pool.close().await.unwrap();
}
