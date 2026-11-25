//! The client's transport, launch, and foreground backups against the fake
//! daemon and stand-in litebackup scripts: what the contract fixtures do not
//! show, such as which connection carried a call.
#![allow(clippy::expect_used, clippy::unwrap_used, reason = "tests assert with expect")]

mod support;

use std::path::{Path, PathBuf};
use std::time::Duration;

use litebackup::{Client, ErrorCode, OffloadOptions, OnceOptions};
use serde_json::{Value, json};
use support::{Answer, FakeDaemon, client_to, read_fixture};

const RULES: &str = "/v1/backup-rules/list";

fn rules_answer() -> Answer {
    Answer::json(RULES, &json!({"rules": []}))
}

#[tokio::test]
async fn calls_reuse_one_connection() {
    let daemon = FakeDaemon::start(vec![rules_answer()]);
    let client = client_to(&daemon).await;
    for _ in 0..3 {
        client.rules().list().await.expect("the rules");
    }
    let requests = daemon.all_requests();
    assert_eq!(requests.len(), 4);
    assert!(requests.iter().all(|request| request.connection == 0), "{requests:?}");
    assert_eq!(daemon.connections(), 1);
}

#[tokio::test]
async fn a_get_that_finds_its_connection_closed_is_sent_once_more_on_a_new_one() {
    let daemon = FakeDaemon::start(vec![Answer::json(RULES, &Value::Null).hang_up().uses(1), rules_answer()]);
    let client = client_to(&daemon).await;
    client.rules().list().await.expect("the retried call");
    let connections: Vec<usize> = daemon.requests().iter().map(|request| request.connection).collect();
    assert_eq!(connections, vec![0, 1]);
}

#[tokio::test]
async fn a_get_is_retried_only_once() {
    let daemon = FakeDaemon::start(vec![Answer::json(RULES, &Value::Null).hang_up()]);
    let client = client_to(&daemon).await;
    let error = client.rules().list().await.expect_err("both attempts fail");
    assert_eq!((error.code(), error.status()), (ErrorCode::CONNECTION_FAILED, 0));
    assert_eq!(daemon.requests().len(), 2);
}

#[tokio::test]
async fn a_post_is_never_sent_twice() {
    let daemon = FakeDaemon::start(vec![Answer::json("/v1/databases/offload", &Value::Null).hang_up()]);
    let client = client_to(&daemon).await;
    let error = client.databases().offload("a.sqlite", OffloadOptions::default()).await.expect_err("no answer");
    assert_eq!(error.code(), ErrorCode::CONNECTION_FAILED);
    assert_eq!(daemon.requests().len(), 1);
}

#[tokio::test(start_paused = true)]
async fn a_call_past_its_deadline_is_timeout_with_status_0() {
    let slow = Answer::json("/v1/databases/release", &json!({})).delay(Duration::from_secs(600));
    let daemon = FakeDaemon::start(vec![slow]);
    let client = client_to(&daemon).await;
    let options = litebackup::ReleaseOptions::default().timeout(Duration::from_secs(1));
    let started = tokio::time::Instant::now();
    let error = client.databases().release("a.sqlite", options).await.expect_err("past the deadline");
    assert_eq!((error.code(), error.status()), (ErrorCode::TIMEOUT, 0));
    assert_eq!(started.elapsed().as_secs(), 31, "the deadline is the timeout plus 30 s");
    assert!(error.message().contains("may still finish"), "{error}");
}

#[tokio::test]
async fn closing_ends_a_waiting_call_and_denies_later_ones() {
    let slow = Answer::json(RULES, &json!({"rules": []})).delay(Duration::from_secs(600));
    let daemon = FakeDaemon::start(vec![slow]);
    let client = client_to(&daemon).await;
    let waiting = {
        let client = client.clone();
        tokio::spawn(async move { client.rules().list().await })
    };
    while daemon.requests().is_empty() {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    client.close();
    let error = waiting.await.expect("the call's task").expect_err("closed");
    assert_eq!(error.code(), ErrorCode::CLOSED);
    assert!(client.is_closed());
    let later = client.rules().list().await.expect_err("closed");
    assert_eq!(later.code(), ErrorCode::CLOSED);
    assert_eq!(daemon.requests().len(), 1);
}

#[tokio::test]
async fn an_answer_over_64_mib_is_invalid_response() {
    let padding = "x".repeat(65 << 20);
    let daemon = FakeDaemon::start(vec![Answer::json(RULES, &json!({"rules": [], "padding": padding}))]);
    let client = client_to(&daemon).await;
    let error = client.rules().list().await.expect_err("too large");
    assert_eq!(error.code(), ErrorCode::INVALID_RESPONSE);
}

#[tokio::test]
async fn a_field_with_the_wrong_type_is_invalid_response_and_a_missing_one_reads_as_its_default() {
    let daemon = FakeDaemon::start(vec![
        Answer::json("/v1/databases/status", &json!({"name": 7})).uses(1),
        Answer::json("/v1/databases/status", &json!({"name": "a.sqlite", "role": "standby", "synced_server": null})),
    ]);
    let client = client_to(&daemon).await;
    let error = client.databases().get("a.sqlite").await.expect_err("a number for a name");
    assert_eq!((error.code(), error.status()), (ErrorCode::INVALID_RESPONSE, 200));
    let status = client.databases().get("a.sqlite").await.expect("a newer daemon's answer");
    assert_eq!(status.role.as_str(), "standby");
    assert_eq!(status.synced_server.version, 0);
    assert_eq!(status.path, "");
}

/// A stand-in litebackup: a shell script in a temporary directory.
fn stand_in(directory: &Path, script: &str) -> PathBuf {
    let path = directory.join("litebackup");
    write_executable(&path, &format!("#!/bin/sh\n{script}\n"));
    path
}

/// Writes an executable file through a child `sh`, so this process never
/// holds it open for writing: a child another test forks meanwhile would
/// inherit that descriptor, and running the file would fail as busy.
fn write_executable(path: &Path, content: &str) {
    let written = std::process::Command::new("sh")
        .args(["-c", "printf '%s' \"$1\" > \"$2\" && chmod 755 \"$2\"", "sh", content])
        .arg(path)
        .status()
        .expect("sh writes the file");
    assert!(written.success(), "writing {} failed", path.display());
}

/// The environment of a client of the session `jobs` whose socket is under
/// the directory.
fn session_environment(directory: &Path) -> Vec<(String, String)> {
    vec![
        ("PATH".to_owned(), std::env::var("PATH").unwrap_or_default()),
        ("HOME".to_owned(), directory.display().to_string()),
        ("XDG_RUNTIME_DIR".to_owned(), directory.join("run").display().to_string()),
    ]
}

/// A fake daemon on the session `jobs` socket under the directory.
fn session_daemon(directory: &Path) -> FakeDaemon {
    FakeDaemon::start_on(&directory.join("run/litebackup/jobs/client.sock"), Vec::new())
}

#[tokio::test]
async fn a_launch_that_denies_a_new_credential_is_drain_required() {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let seen = directory.path().join("seen");
    let binary = stand_in(
        directory.path(),
        &format!(
            "printf '%s|%s|%s' \"$*\" \"$LITEBACKUP_API_KEY\" \"$EMPTY\" > '{}'\n\
             echo '{{\"code\":\"drain_required\",\"message\":\"drain the session first\"}}' >&2\nexit 1",
            seen.display()
        ),
    );
    let mut environment = session_environment(directory.path());
    environment.push(("EMPTY".to_owned(), String::new()));
    let error = Client::builder()
        .session("jobs")
        .environment(environment)
        .binary_path(&binary)
        .api_key("litebackup_example@backups.example.com")
        .connect()
        .await
        .expect_err("the launch refused");
    assert_eq!(error.code(), ErrorCode::DRAIN_REQUIRED);
    assert!(error.message().contains("drain the session first"), "{error}");
    let seen = std::fs::read_to_string(seen).expect("what the stand-in saw");
    assert_eq!(seen, "launch --session jobs --stdout-format json|litebackup_example@backups.example.com|");
}

#[tokio::test]
async fn a_running_daemon_is_not_launched_again() {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let daemon = session_daemon(directory.path());
    let launch_file = directory.path().join("launched");
    let binary = stand_in(directory.path(), &format!("touch '{}'", launch_file.display()));
    let client = Client::builder()
        .session("jobs")
        .environment(session_environment(directory.path()))
        .binary_path(&binary)
        .connect()
        .await
        .expect("a client of the running daemon");
    assert_eq!(client.session_name(), Some("jobs"));
    assert!(!launch_file.exists());
    assert_eq!(daemon.all_requests().len(), 1);
}

/// A client of the session `jobs`, whose foreground backups run the stand-in.
async fn jobs_client(directory: &Path, binary: &Path) -> Client {
    Client::builder()
        .session("jobs")
        .environment(session_environment(directory))
        .binary_path(binary)
        .api_key("litebackup_join_token@backups.example.com")
        .launch(false)
        .connect()
        .await
        .expect("a client of the session")
}

#[tokio::test]
async fn a_foreground_backup_runs_the_command_with_the_credential() {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let _daemon = session_daemon(directory.path());
    let seen = directory.path().join("seen");
    let binary =
        stand_in(directory.path(), &format!("printf '%s|%s' \"$*\" \"$LITEBACKUP_API_KEY\" > '{}'", seen.display()));
    let client = jobs_client(directory.path(), &binary).await;
    let options = OnceOptions::default().prefix("tenants").timeout(Duration::from_secs(90));
    client.backups().once("/data/tenants", options).await.expect("the job");
    let seen = std::fs::read_to_string(seen).expect("what the stand-in saw");
    assert_eq!(
        seen,
        "backup --stdout-format json --durability server --session jobs --timeout 90000ms -- /data/tenants tenants/\
         |litebackup_join_token@backups.example.com"
    );
}

#[tokio::test]
async fn a_failed_foreground_backup_raises_its_reported_error() {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let _daemon = session_daemon(directory.path());
    let binary = stand_in(
        directory.path(),
        "echo 'checking' >&2\necho '{\"code\":\"busy\",\"message\":\"main.sqlite is busy\"}' >&2\nexit 1",
    );
    let client = jobs_client(directory.path(), &binary).await;
    let error = client.backups().once("/data/main.sqlite", OnceOptions::default()).await.expect_err("busy");
    assert_eq!((error.code(), error.message()), ("busy", "main.sqlite is busy"));
}

#[tokio::test]
async fn a_foreground_backup_whose_interpreter_is_missing_names_its_first_line() {
    let fixture = read_fixture("binary", "override-missing");
    let directory = tempfile::tempdir().expect("a temporary directory");
    let _daemon = session_daemon(directory.path());
    let script = directory.path().join("litebackup");
    write_executable(&script, fixture["interpreter_script"].as_str().expect("interpreter_script"));
    let client = jobs_client(directory.path(), &script).await;
    let error =
        client.backups().once(directory.path().join("a.db"), OnceOptions::default()).await.expect_err("no interpreter");
    assert_eq!(error.code(), fixture["interpreter_code"].as_str().expect("interpreter_code"));
    let expected = fixture["interpreter_message_contains"].as_str().expect("interpreter_message_contains");
    assert!(error.message().contains(expected), "{error}");
}

#[tokio::test]
async fn a_foreground_backup_whose_binary_is_under_a_file_is_binary_not_found() {
    let fixture = read_fixture("binary", "override-missing");
    let directory = tempfile::tempdir().expect("a temporary directory");
    let _daemon = session_daemon(directory.path());
    let under_file = directory.path().join(fixture["not_directory_file"].as_str().expect("not_directory_file"));
    std::fs::write(under_file.parent().expect("its directory"), "a file, not a directory").expect("the file");
    let client = jobs_client(directory.path(), &under_file).await;
    let error =
        client.backups().once(directory.path().join("a.db"), OnceOptions::default()).await.expect_err("missing");
    assert_eq!(error.code(), fixture["code"].as_str().expect("code"));
}

#[tokio::test]
async fn closing_the_client_stops_a_running_foreground_backup() {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let _daemon = session_daemon(directory.path());
    let started = directory.path().join("started");
    let binary = stand_in(directory.path(), &format!("touch '{}'\nexec sleep 600", started.display()));
    let client = jobs_client(directory.path(), &binary).await;
    let job = {
        let client = client.clone();
        tokio::spawn(async move { client.backups().once("/data/main.sqlite", OnceOptions::default()).await })
    };
    while !started.exists() {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    client.close();
    let error = tokio::time::timeout(Duration::from_secs(10), job).await.expect("stopped in time").expect("the task");
    assert_eq!(error.expect_err("closed").code(), ErrorCode::CLOSED);
}

#[tokio::test]
async fn a_socket_path_client_runs_no_foreground_backup() {
    let daemon = FakeDaemon::start(Vec::new());
    let client = client_to(&daemon).await;
    let error = client.backups().once("/data/main.sqlite", OnceOptions::default()).await.expect_err("no session");
    assert_eq!(error.code(), ErrorCode::INVALID_ARGUMENT);
}

#[tokio::test]
async fn an_empty_api_key_clears_the_inherited_one_in_child_commands() {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let _daemon = session_daemon(directory.path());
    let seen = directory.path().join("seen");
    let binary = stand_in(directory.path(), &format!("printf '[%s]' \"$LITEBACKUP_API_KEY\" > '{}'", seen.display()));
    let mut environment = session_environment(directory.path());
    environment.push(("LITEBACKUP_API_KEY".to_owned(), "litebackup_inherited@server-a.example.com".to_owned()));
    let client = Client::builder()
        .session("jobs")
        .environment(environment)
        .binary_path(&binary)
        .api_key("")
        .launch(false)
        .connect()
        .await
        .expect("a client of the session");
    client.backups().once("/data/main.sqlite", OnceOptions::default()).await.expect("the job");
    assert_eq!(std::fs::read_to_string(seen).expect("what the stand-in saw"), "[]");
}
