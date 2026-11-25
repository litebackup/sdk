//! Failovers against the fake daemon, on paused time: the role the start
//! answered, role changes, refusals, lost and stale roles, the role reads
//! after the daemon answers again, expired cursors, stop, and close.
#![allow(clippy::expect_used, clippy::unwrap_used, reason = "tests assert with expect")]

mod support;

use std::collections::BTreeMap;
use std::time::Duration;

use futures_util::StreamExt as _;
use litebackup::{Client, ErrorCode, Failover, FailoverNotice, FailoverNotices, FailoverOptions, Role};
use serde_json::{Value, json};
use support::{Answer, FakeDaemon, client_to};

const NAME: &str = "main.sqlite";
const EVENTS: &str = "/v1/events/list";

fn query(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs.iter().map(|(key, value)| ((*key).to_owned(), (*value).to_owned())).collect()
}

/// The long-poll after a cursor.
fn long_poll(after: i64) -> BTreeMap<String, String> {
    query(&[("after", &after.to_string()), ("wait_millis", "10000"), ("limit", "1000")])
}

/// The read of the log's end.
fn log_end() -> BTreeMap<String, String> {
    query(&[("after", "-1")])
}

fn events(events: &[Value], next: i64) -> Value {
    json!({"events": events, "next": next})
}

fn role_changed(id: i64, role: &str) -> Value {
    json!({"id": id, "name": NAME, "type": "role_changed", "role": role, "message": role})
}

fn status(role: &str, state: &str) -> Value {
    json!({"name": NAME, "path": "/var/lib/app/main.sqlite", "role": role, "state": state, "failover": true})
}

/// The answers every failover test starts with: the log's end at 5 and a
/// start as `role`.
fn started_as(role: &str) -> Vec<Answer> {
    vec![
        Answer::json(EVENTS, &events(&[], 5)).query(log_end()).uses(1),
        Answer::json("/v1/failovers/start", &json!({"name": NAME, "path": "/var/lib/app/main.sqlite", "role": role})),
    ]
}

/// A long-poll that waits its 10 s and brings nothing, answering every
/// long-poll the script does not.
fn quiet_long_polls() -> Answer {
    Answer::json(EVENTS, &events(&[], 0)).delay(Duration::from_secs(10))
}

async fn start(daemon: &FakeDaemon) -> (Client, Failover, FailoverNotices) {
    let client = client_to(daemon).await;
    let failover = client.failovers().start("/var/lib/app/main.sqlite", FailoverOptions::default()).await.unwrap();
    let notices = failover.notices().expect("the notices");
    (client, failover, notices)
}

/// The next notice, within a minute of paused time.
async fn next_notice(notices: &mut FailoverNotices) -> FailoverNotice {
    tokio::time::timeout(Duration::from_secs(60), notices.next()).await.expect("a notice in time").expect("a notice")
}

fn notice_role(notice: &FailoverNotice) -> Option<&str> {
    match notice {
        FailoverNotice::Role(role) => Some(role.as_str()),
        _ => None,
    }
}

fn error_code(notice: &FailoverNotice) -> Option<&str> {
    match notice {
        FailoverNotice::Error(error) => Some(error.code()),
        _ => None,
    }
}

#[tokio::test(start_paused = true)]
async fn the_start_role_comes_first_then_each_change() {
    let mut answers = started_as("replica");
    answers.push(Answer::json(EVENTS, &events(&[role_changed(6, "origin")], 6)).query(long_poll(5)).uses(1));
    answers.push(quiet_long_polls());
    let daemon = FakeDaemon::start(answers);
    let (_client, failover, mut notices) = start(&daemon).await;
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("replica"));
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("origin"));
    assert!(failover.is_origin());
    assert_eq!(failover.name(), NAME);
    assert!(failover.notices().is_none(), "the notices are handed out once");
}

#[tokio::test(start_paused = true)]
async fn a_denial_makes_the_role_unknown_and_is_reported() {
    let mut answers = started_as("origin");
    let denied = json!({"id": 6, "name": NAME, "type": "denied", "message": "the file holds writes the server lacks"});
    answers.push(Answer::json(EVENTS, &events(&[denied], 6)).query(long_poll(5)).uses(1));
    answers.push(Answer::json("/v1/databases/status", &status("origin", "conflicted")));
    answers.push(quiet_long_polls());
    let daemon = FakeDaemon::start(answers);
    let (_client, failover, mut notices) = start(&daemon).await;
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("origin"));
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("unknown"));
    let FailoverNotice::Denied(message) = next_notice(&mut notices).await else { panic!("not a refusal") };
    assert_eq!(message, "the file holds writes the server lacks");
    assert_eq!(failover.role(), Role::UNKNOWN);
}

#[tokio::test(start_paused = true)]
async fn a_read_the_daemon_did_not_answer_makes_the_role_unknown_until_it_is_read_again() {
    let mut answers = started_as("origin");
    // The first long-poll's connection closes twice: the GET's one retry too.
    answers.push(Answer::json(EVENTS, &Value::Null).query(long_poll(5)).hang_up().uses(2));
    answers.push(Answer::json(EVENTS, &events(&[], 7)).query(log_end()).uses(2));
    answers.push(Answer::json("/v1/databases/status", &status("origin", "synced")));
    answers.push(quiet_long_polls());
    let daemon = FakeDaemon::start(answers);
    let (_client, failover, mut notices) = start(&daemon).await;
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("origin"));
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("unknown"));
    assert_eq!(error_code(&next_notice(&mut notices).await), Some(ErrorCode::CONNECTION_FAILED));
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("origin"));
    assert!(failover.is_origin());
    let reads: Vec<(String, BTreeMap<String, String>)> =
        daemon.requests().into_iter().map(|request| (request.path, request.query)).skip(2).take(5).collect();
    assert_eq!(
        reads,
        [
            (EVENTS.to_owned(), long_poll(5)),
            (EVENTS.to_owned(), long_poll(5)),
            (EVENTS.to_owned(), log_end()),
            ("/v1/databases/status".to_owned(), query(&[("name", NAME)])),
            (EVENTS.to_owned(), log_end()),
        ]
    );
}

#[tokio::test(start_paused = true)]
async fn an_origin_the_daemon_stops_confirming_reads_as_unknown_after_15_seconds() {
    let mut answers = started_as("origin");
    answers.push(Answer::json(EVENTS, &events(&[], 5)).delay(Duration::from_secs(600)));
    let daemon = FakeDaemon::start(answers);
    let (_client, failover, mut notices) = start(&daemon).await;
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("origin"));
    tokio::time::sleep(Duration::from_secs(14)).await;
    assert!(failover.is_origin());
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert_eq!(failover.role(), Role::UNKNOWN);
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("unknown"));
    assert_eq!(error_code(&next_notice(&mut notices).await), Some(ErrorCode::TIMEOUT));
}

#[tokio::test(start_paused = true)]
async fn stop_makes_the_role_unknown_and_stops_the_failover() {
    let mut answers = started_as("origin");
    answers.push(quiet_long_polls());
    answers.push(Answer::json("/v1/backup-rules/stop", &json!({})));
    let daemon = FakeDaemon::start(answers);
    let (_client, failover, mut notices) = start(&daemon).await;
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("origin"));
    failover.stop().await.unwrap();
    assert_eq!(failover.role(), Role::UNKNOWN);
    let stop = daemon.requests().into_iter().find(|request| request.path == "/v1/backup-rules/stop").expect("a stop");
    assert_eq!(stop.body, json!({"name": NAME}));
    assert!(tokio::time::timeout(Duration::from_secs(30), notices.next()).await.is_err(), "a notice after stop");
}

#[tokio::test(start_paused = true)]
async fn closing_the_client_makes_the_role_unknown_without_a_role_notice() {
    let mut answers = started_as("origin");
    answers.push(quiet_long_polls());
    let daemon = FakeDaemon::start(answers);
    let (client, failover, mut notices) = start(&daemon).await;
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("origin"));
    client.close();
    assert_eq!(failover.role(), Role::UNKNOWN);
    while let Ok(Some(notice)) = tokio::time::timeout(Duration::from_secs(30), notices.next()).await {
        assert_eq!(notice_role(&notice), None, "a role notice after close: {notice:?}");
    }
}

#[tokio::test(start_paused = true)]
async fn an_expired_cursor_resumes_from_the_log_end_after_reading_the_role() {
    let mut answers = started_as("replica");
    let expired = json!({"code": "cursor_expired", "message": "events after 5 are no longer kept"});
    answers.push(Answer::status(EVENTS, 410, &expired).query(long_poll(5)).uses(1));
    answers.push(Answer::json(EVENTS, &events(&[], 40)).query(log_end()));
    answers.push(Answer::json("/v1/databases/status", &status("origin", "synced")));
    answers.push(Answer::json(EVENTS, &events(&[], 40)).query(long_poll(40)).delay(Duration::from_secs(10)));
    let daemon = FakeDaemon::start(answers);
    let (_client, failover, mut notices) = start(&daemon).await;
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("replica"));
    assert_eq!(error_code(&next_notice(&mut notices).await), Some(ErrorCode::CURSOR_EXPIRED));
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("origin"));
    assert!(failover.is_origin());
}

#[tokio::test(start_paused = true)]
async fn sdk_unsupported_ends_following_with_the_role_unknown() {
    let mut answers = started_as("origin");
    let unsupported = json!({"code": "sdk_unsupported", "message": "upgrade the SDK"});
    answers.push(Answer::status(EVENTS, 426, &unsupported).query(long_poll(5)));
    let daemon = FakeDaemon::start(answers);
    let (_client, failover, mut notices) = start(&daemon).await;
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("origin"));
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("unknown"));
    assert_eq!(error_code(&next_notice(&mut notices).await), Some(ErrorCode::SDK_UNSUPPORTED));
    tokio::time::sleep(Duration::from_secs(60)).await;
    assert_eq!(failover.role(), Role::UNKNOWN);
    let long_polls = daemon.requests().iter().filter(|request| request.query == long_poll(5)).count();
    assert_eq!(long_polls, 1, "following went on after sdk_unsupported");
}

#[tokio::test(start_paused = true)]
async fn a_role_read_raced_by_a_role_change_is_read_again() {
    let mut answers = started_as("origin");
    let draining = json!({"id": 6, "name": NAME, "type": "draining", "message": ""});
    answers.push(Answer::json(EVENTS, &events(&[draining], 6)).query(long_poll(5)).uses(1));
    // The role read after the drain: the log ends at 10, the status says
    // replica, and by the second look the log holds a role change at 11.
    answers.push(Answer::json(EVENTS, &events(&[], 10)).query(log_end()).uses(1));
    answers.push(Answer::json("/v1/databases/status", &status("replica", "synced")).uses(1));
    answers.push(Answer::json(EVENTS, &events(&[], 11)).query(log_end()));
    answers.push(
        Answer::json(EVENTS, &events(&[role_changed(11, "origin")], 11))
            .query(query(&[("after", "10"), ("limit", "1000")]))
            .uses(1),
    );
    answers.push(Answer::json("/v1/databases/status", &status("origin", "synced")));
    answers.push(quiet_long_polls());
    let daemon = FakeDaemon::start(answers);
    let (_client, failover, mut notices) = start(&daemon).await;
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("origin"));
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("unknown"));
    assert_eq!(notice_role(&next_notice(&mut notices).await), Some("origin"), "the raced replica read was trusted");
    assert!(failover.is_origin());
    let status_reads = daemon.requests().iter().filter(|request| request.path == "/v1/databases/status").count();
    assert_eq!(status_reads, 2);
}

#[tokio::test(start_paused = true)]
async fn discard_local_is_sent_with_the_start() {
    let mut answers = started_as("origin");
    answers.push(quiet_long_polls());
    let daemon = FakeDaemon::start(answers);
    let client = client_to(&daemon).await;
    let options = FailoverOptions::default().discard_local(true).name("main");
    client.failovers().start("/var/lib/app/main.sqlite", options).await.unwrap();
    let start = daemon.requests().into_iter().find(|request| request.path == "/v1/failovers/start").expect("a start");
    assert_eq!(start.body, json!({"path": "/var/lib/app/main.sqlite", "name": "main", "discard_local": true}));
}

#[tokio::test(flavor = "current_thread", start_paused = true)]
async fn closing_before_the_follower_starts_sends_no_notice() {
    let mut answers = started_as("origin");
    answers.push(quiet_long_polls());
    let daemon = FakeDaemon::start(answers);
    let (client, failover, mut notices) = start(&daemon).await;
    client.close();
    assert_eq!(failover.role(), Role::UNKNOWN);
    while let Ok(Some(notice)) = tokio::time::timeout(Duration::from_secs(30), notices.next()).await {
        assert_eq!(notice_role(&notice), None, "a role notice after close: {notice:?}");
    }
}
