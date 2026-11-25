//! The rusqlite and sqlx adapters on real SQLite files, and registering
//! backups at startup against the fake daemon.
#![allow(clippy::expect_used, clippy::unwrap_used, reason = "tests assert with expect")]
#![cfg(all(feature = "rusqlite", feature = "sqlx"))]

mod support;

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use litebackup::integrations::rusqlite::RusqliteAdapter;
use litebackup::integrations::sqlx::SqlxAdapter;
use litebackup::startup::{DatabaseBackup, register_backups};
use litebackup::{Adapter, BackupOptions, ErrorCode, LeaseOptions, PoolOptions};
use serde_json::json;
use support::{Answer, FakeDaemon, client_to};

/// A database file with one table, in a new temporary directory.
fn database() -> (tempfile::TempDir, PathBuf) {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("main.sqlite");
    let connection = rusqlite::Connection::open(&path).unwrap();
    connection.execute_batch("CREATE TABLE notes (body TEXT NOT NULL)").unwrap();
    (directory, path)
}

fn note_count(path: &Path) -> i64 {
    let connection = rusqlite::Connection::open(path).unwrap();
    connection.query_row("SELECT count(*) FROM notes", [], |columns| columns.get(0)).unwrap()
}

#[tokio::test]
async fn rusqlite_opens_only_an_existing_file() {
    let directory = tempfile::tempdir().unwrap();
    let missing = directory.path().join("missing.sqlite");
    assert!(RusqliteAdapter::default().open(&missing).await.is_err());
    assert!(!missing.exists(), "opening created the file");
}

#[tokio::test]
async fn rusqlite_close_rolls_back_and_never_commits() {
    let (_directory, path) = database();
    let adapter = RusqliteAdapter::default();
    let connection = adapter.open(&path).await.unwrap();
    connection.call(|db| db.execute("INSERT INTO notes (body) VALUES ('kept by commit')", [])).await.unwrap();
    connection.call(|db| db.execute_batch("BEGIN; INSERT INTO notes (body) VALUES ('rolled back');")).await.unwrap();
    adapter.close(connection).await.unwrap();
    assert_eq!(note_count(&path), 1);
}

#[tokio::test]
async fn a_panic_in_rusqlite_work_reaches_the_caller_and_the_connection_stays() {
    let (_directory, path) = database();
    let adapter = RusqliteAdapter::default();
    let connection = Arc::new(adapter.open(&path).await.unwrap());
    let panicking = Arc::clone(&connection);
    let panicked =
        tokio::spawn(async move { panicking.call(|_db| -> rusqlite::Result<()> { panic!("the work panics") }).await })
            .await;
    assert!(panicked.is_err_and(|failure| failure.is_panic()));
    let count =
        connection.call(|db| db.query_row("SELECT count(*) FROM notes", [], |columns| columns.get::<_, i64>(0))).await;
    assert_eq!(count.unwrap(), 0);
    let connection = Arc::into_inner(connection).unwrap();
    adapter.close(connection).await.unwrap();
}

#[tokio::test]
async fn rusqlite_setup_runs_on_each_connection() {
    let (_directory, path) = database();
    let adapter = RusqliteAdapter::default().setup(|db| db.pragma_update(None, "user_version", 7));
    let connection = adapter.open(&path).await.unwrap();
    let version =
        connection.call(|db| db.query_row("PRAGMA user_version", [], |columns| columns.get::<_, i64>(0))).await;
    assert_eq!(version.unwrap(), 7);
    adapter.close(connection).await.unwrap();
}

#[tokio::test(flavor = "current_thread")]
async fn slow_rusqlite_work_leaves_a_current_thread_runtime_running() {
    let (_directory, path) = database();
    let adapter = RusqliteAdapter::default();
    let connection = adapter.open(&path).await.unwrap();
    let ticks = Arc::new(AtomicUsize::new(0));
    let counting = Arc::clone(&ticks);
    let ticker = tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_millis(10)).await;
            counting.fetch_add(1, Ordering::SeqCst);
        }
    });
    connection
        .call(|_db| {
            std::thread::sleep(Duration::from_millis(200));
            Ok(())
        })
        .await
        .unwrap();
    ticker.abort();
    assert!(ticks.load(Ordering::SeqCst) >= 5, "the runtime stalled while SQLite worked");
    adapter.close(connection).await.unwrap();
}

#[tokio::test]
async fn sqlx_opens_only_an_existing_file_and_rolls_back_on_close() {
    let directory = tempfile::tempdir().unwrap();
    let missing = directory.path().join("missing.sqlite");
    assert!(SqlxAdapter::default().open(&missing).await.is_err());
    assert!(!missing.exists(), "opening created the file");
    let (_directory, path) = database();
    let adapter = SqlxAdapter::default();
    let mut connection = adapter.open(&path).await.unwrap();
    sqlx::query("INSERT INTO notes (body) VALUES ('kept by commit')").execute(&mut connection).await.unwrap();
    sqlx::raw_sql("BEGIN; INSERT INTO notes (body) VALUES ('rolled back');").execute(&mut connection).await.unwrap();
    adapter.close(connection).await.unwrap();
    assert_eq!(note_count(&path), 1);
    let connection = adapter.open(&path).await.unwrap();
    adapter.close(connection).await.unwrap();
}

/// A daemon that leases `name` at `path`.
fn leasing_daemon(name: &str, path: &Path) -> FakeDaemon {
    let database = json!({"name": name, "path": path, "role": "origin", "state": "synced", "acquired": true});
    FakeDaemon::start(vec![
        Answer::json("/v1/databases/acquire", &json!({"database": database})),
        Answer::json("/v1/databases/release", &json!({})),
    ])
}

#[tokio::test]
async fn pools_lease_through_both_adapters() {
    let (_directory, path) = database();
    let daemon = leasing_daemon("main.sqlite", &path);
    let client = client_to(&daemon).await;
    let options = || LeaseOptions::default().path(&path);
    let rusqlite_pool = client.databases().pool(RusqliteAdapter::default(), PoolOptions::default()).unwrap();
    rusqlite_pool
        .lease("main.sqlite", options(), async |db| {
            db.call(|db| db.execute("INSERT INTO notes (body) VALUES ('through rusqlite')", [])).await
        })
        .await
        .unwrap();
    rusqlite_pool.close().await.unwrap();
    let sqlx_pool = client.databases().pool(SqlxAdapter::default(), PoolOptions::default()).unwrap();
    sqlx_pool
        .lease("main.sqlite", options(), async |db| {
            sqlx::query("INSERT INTO notes (body) VALUES (?)").bind("through sqlx").execute(&mut *db).await
        })
        .await
        .unwrap();
    sqlx_pool.close().await.unwrap();
    assert_eq!(note_count(&path), 2);
}

fn saved_rule(path: &Path) -> serde_json::Value {
    json!({"id": "01a1-rule", "path": path, "name": "main.sqlite", "prefix": "", "watch": "inotify",
           "maximum_local_databases": 0, "offload_durability": "server", "offload_rollback_journal": false,
           "failover": false})
}

#[tokio::test]
async fn startup_creates_a_missing_database_and_starts_its_backup() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("main.sqlite");
    let daemon = FakeDaemon::start(vec![Answer::json("/v1/backup-rules/start", &saved_rule(&path))]);
    let client = client_to(&daemon).await;
    let backup = DatabaseBackup::new(&path).options(BackupOptions::default().name("main.sqlite"));
    let rules = register_backups(&client, &[backup]).await.unwrap();
    assert_eq!(rules.len(), 1);
    assert_eq!(rules[0].id, "01a1-rule");
    assert!(path.exists(), "the empty database was not created");
    let start = &daemon.requests()[0];
    assert_eq!(start.body, json!({"path": path, "name": "main.sqlite"}));
}

#[tokio::test]
async fn startup_denies_memory_databases_missing_directories_and_changed_rules() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("main.sqlite");
    let exists =
        json!({"code": "rule_exists", "message": "the path has a rule with other settings", "rule": saved_rule(&path)});
    let daemon = FakeDaemon::start(vec![Answer::status("/v1/backup-rules/start", 409, &exists)]);
    let client = client_to(&daemon).await;
    let memory = register_backups(&client, &[DatabaseBackup::new(":memory:")]).await.expect_err("a memory database");
    assert_eq!(memory.code(), ErrorCode::INVALID_ARGUMENT);
    let nowhere = directory.path().join("missing/main.sqlite");
    let missing = register_backups(&client, &[DatabaseBackup::new(&nowhere)]).await.expect_err("no directory");
    assert_eq!(missing.code(), ErrorCode::INVALID_ARGUMENT);
    assert!(daemon.requests().is_empty());
    let changed = register_backups(&client, &[DatabaseBackup::new(&path)]).await.expect_err("changed settings");
    assert_eq!((changed.code(), changed.status()), (ErrorCode::RULE_EXISTS, 409));
    assert!(changed.message().contains(&path.display().to_string()), "{changed}");
    assert_eq!(changed.details()["rule"]["id"], "01a1-rule");
}
