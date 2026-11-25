//! The contract's pool lease fixtures (`../testdata/contract/lifecycle`):
//! which steps run, the daemon calls a lease makes, and the errors the lease
//! and closing the pool raise, cancellation included.
#![allow(clippy::expect_used, clippy::unwrap_used, reason = "tests assert with expect")]

mod support;

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use litebackup::{Adapter, BoxError, CancellationToken, CloseError, Durability, LeaseError, LeaseOptions, PoolOptions};
use serde_json::{Map, Value, json};
use support::{Answer, FakeDaemon, client_to, duration, fixture_names, read_fixture, skip_reason};

/// An adapter step's own error, which a fixture names by its source.
#[derive(Debug, PartialEq, Eq)]
struct StepError(&'static str);

impl std::fmt::Display for StepError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{} failed", self.0)
    }
}

impl std::error::Error for StepError {}

/// The steps the adapter and the work ran, with when, and the path opened.
#[derive(Debug, Default)]
struct Steps {
    steps: Vec<(&'static str, Instant)>,
    open_path: Option<PathBuf>,
}

/// An adapter whose open and close fail when the fixture says so.
#[derive(Clone)]
struct StepAdapter {
    failing: Arc<Vec<String>>,
    steps: Arc<Mutex<Steps>>,
}

impl StepAdapter {
    fn step(&self, step: &'static str) {
        self.steps.lock().unwrap().steps.push((step, Instant::now()));
    }

    fn fails(&self, step: &str) -> bool {
        self.failing.iter().any(|failing| failing == step)
    }
}

impl Adapter for StepAdapter {
    type Connection = ();

    fn open(&self, path: &Path) -> impl Future<Output = Result<(), BoxError>> + Send {
        self.step("open");
        self.steps.lock().unwrap().open_path = Some(path.to_path_buf());
        std::future::ready(if self.fails("open") { Err(Box::new(StepError("open")) as BoxError) } else { Ok(()) })
    }

    fn close(&self, connection: ()) -> impl Future<Output = Result<(), CloseError<()>>> + Send {
        self.step("close");
        std::future::ready(if self.fails("close") {
            Err(CloseError::retry(StepError("close"), connection))
        } else {
            Ok(())
        })
    }
}

/// The daemon's answers: each database step, failing when the fixture says so.
fn answers(fixture: &Map<String, Value>, name: &str, failing: &[String]) -> Vec<Answer> {
    let bodies = [
        ("acquire", fixture["acquire_response"].clone()),
        (
            "sync",
            json!({"name": name, "path": "/x", "role": "origin", "state": "synced", "acquired": true,
                   "synced_server": {"version": 3}, "synced_cloud": {"version": 3}}),
        ),
        (
            "release",
            json!({"database": {"name": name, "path": "/x", "role": "origin", "state": "synced"},
                   "checkpointed": false, "vacuumed": false, "offloaded": false}),
        ),
    ];
    bodies
        .into_iter()
        .map(|(step, body)| {
            let path = format!("/v1/databases/{step}");
            if failing.iter().any(|failing| failing == step) {
                Answer::status(&path, 503, &json!({"code": "unavailable", "message": format!("{step} failed")}))
            } else {
                Answer::json(&path, &body)
            }
        })
        .collect()
}

/// The lease's errors as the fixtures list them: an open failure grouped with
/// its failed release is two.
enum Raised<'a> {
    Work(&'a StepError),
    Open(&'a BoxError),
    Litebackup(&'a litebackup::Error),
}

fn flatten(error: &LeaseError<StepError>) -> Vec<Raised<'_>> {
    match error {
        LeaseError::Work(error) => vec![Raised::Work(error)],
        LeaseError::Open { error, release_failure } => {
            let mut raised = vec![Raised::Open(error)];
            raised.extend(release_failure.iter().map(Raised::Litebackup));
            raised
        }
        LeaseError::Litebackup(error) => vec![Raised::Litebackup(error)],
        unknown => panic!("an unknown lease error: {unknown:?}"),
    }
}

fn check_raised(context: &str, raised: &[Raised<'_>], expected: &[Value]) {
    assert_eq!(raised.len(), expected.len(), "{context}: {} raised, expected {expected:?}", raised.len());
    for (actual, expected) in raised.iter().zip(expected) {
        match expected.get("source").and_then(Value::as_str) {
            Some("work") => {
                assert!(matches!(actual, Raised::Work(StepError("work"))), "{context}: not the work's error");
            }
            Some("open") => {
                let Raised::Open(error) = actual else { panic!("{context}: not the open error") };
                assert_eq!(error.downcast_ref::<StepError>(), Some(&StepError("open")), "{context}");
            }
            _ => {
                let Raised::Litebackup(error) = actual else { panic!("{context}: not a Litebackup error") };
                check_litebackup_error(context, error, expected);
            }
        }
    }
}

fn check_litebackup_error(context: &str, error: &litebackup::Error, expected: &Value) {
    assert_eq!(error.code(), expected["code"].as_str().expect("a code"), "{context}: {error}");
    if let Some(details) = expected.get("details") {
        assert_eq!(&Value::Object(error.details().clone()), details, "{context}");
    }
    if let Some(step) = expected.get("step").and_then(Value::as_str) {
        assert_eq!(error.lease_cleanup().map(|cleanup| cleanup.step.as_str()), Some(step), "{context}");
    }
    let source = std::error::Error::source(error);
    match expected.get("cause").and_then(Value::as_str) {
        Some("close") => {
            assert_eq!(
                source.and_then(|source| source.downcast_ref::<StepError>()),
                Some(&StepError("close")),
                "{context}"
            );
        }
        Some("release") => {
            let cause =
                source.and_then(|source| source.downcast_ref::<litebackup::Error>()).expect("the release's error");
            assert_eq!(cause.code(), "unavailable", "{context}");
        }
        _ => {}
    }
}

/// The release gets what closing left of the cleanup timeout, so a
/// `timeout_millis` up to a second short of the fixture's reads as it.
fn with_spent_cleanup(mut body: Value, expected: &Value) -> Value {
    if let (Some(sent), Some(wanted)) = (body["timeout_millis"].as_i64(), expected["timeout_millis"].as_i64())
        && (0..=1000).contains(&(wanted - sent))
    {
        body["timeout_millis"] = Value::from(wanted);
    }
    body
}

async fn check_lifecycle(name: &str) {
    let fixture = read_fixture("lifecycle", name);
    if let Some(reason) = skip_reason(&fixture) {
        eprintln!("skipping lifecycle/{name}: {reason}");
        return;
    }
    let failing: Vec<String> = fixture["fail"]
        .as_array()
        .expect("fail")
        .iter()
        .map(|step| step.as_str().expect("a step").to_owned())
        .collect();
    let lease = &fixture["lease"];
    let database = lease["name"].as_str().expect("a name");
    let daemon = FakeDaemon::start(answers(&fixture, database, &failing));
    let client = client_to(&daemon).await;
    let adapter = StepAdapter { failing: Arc::new(failing.clone()), steps: Arc::default() };
    let mut pool_options = PoolOptions::default();
    pool_options.cleanup_timeout = lease.get("cleanup_timeout").and_then(Value::as_str).map(duration);
    let cancel = fixture.get("cancel").and_then(Value::as_str);
    let token = CancellationToken::new();
    let mut options = LeaseOptions::default();
    options.path = lease.get("path").and_then(Value::as_str).map(PathBuf::from);
    options.durability = lease.get("durability").and_then(Value::as_str).map(Durability::new);
    options.offload = lease.get("offload").and_then(Value::as_bool).unwrap_or(false);
    options.require_existing = lease.get("require_existing").and_then(Value::as_bool).unwrap_or(false);
    options.cancellation = Some(token.clone());
    if cancel == Some("before") {
        token.cancel();
    }
    let lease_error;
    let mut close_errors = Vec::new();
    match client.databases().pool(adapter.clone(), pool_options) {
        Err(error) => lease_error = Some(LeaseError::Litebackup(error)),
        Ok(pool) => {
            let work_adapter = adapter.clone();
            let leased = pool
                .lease(database, options, async |_connection| {
                    work_adapter.step("work");
                    if cancel.is_some_and(|cancel| cancel.starts_with("work")) {
                        token.cancel();
                    }
                    if work_adapter.fails("work") || cancel == Some("work") {
                        return Err(StepError("work"));
                    }
                    Ok(())
                })
                .await;
            lease_error = leased.err();
            if let Err(error) = pool.close().await {
                close_errors = error.errors;
            }
        }
    }
    let expect = &fixture["expect"];
    let steps_seen = adapter.steps.lock().unwrap();
    let mut timeline = steps_seen.steps.clone();
    for request in daemon.requests() {
        if let Some(step) = request.path.strip_prefix("/v1/databases/") {
            let step: &'static str = Box::leak(step.to_owned().into_boxed_str());
            timeline.push((step, request.arrived));
        }
    }
    timeline.sort_by_key(|(_, time)| *time);
    let steps: Vec<Value> = timeline.iter().map(|(step, _)| Value::from(*step)).collect();
    assert_eq!(Value::Array(steps), expect["steps"], "lifecycle/{name}");
    if let Some(open_path) = expect.get("open_path").and_then(Value::as_str) {
        assert_eq!(steps_seen.open_path.as_deref(), Some(Path::new(open_path)), "lifecycle/{name}");
    }
    if let Some(created) = expect.get("created_file").and_then(Value::as_str) {
        assert!(Path::new(created).exists(), "lifecycle/{name}: {created} was not created");
    }
    if let Some(missing) = expect.get("missing_file").and_then(Value::as_str) {
        assert!(!Path::new(missing).exists(), "lifecycle/{name}: {missing} was created");
    }
    for request in daemon.requests() {
        let expected = match request.path.as_str() {
            "/v1/databases/acquire" => expect.get("acquire_body"),
            "/v1/databases/release" => expect.get("release_body"),
            _ => None,
        };
        if let Some(expected) = expected.filter(|expected| expected.is_object()) {
            assert_eq!(
                &with_spent_cleanup(request.body.clone(), expected),
                expected,
                "lifecycle/{name} {}",
                request.path
            );
        }
    }
    let raised = lease_error.as_ref().map(flatten).unwrap_or_default();
    check_raised(&format!("lifecycle/{name} lease"), &raised, expect["raised"].as_array().expect("raised"));
    let closed: Vec<Raised<'_>> = close_errors.iter().map(Raised::Litebackup).collect();
    check_raised(&format!("lifecycle/{name} close"), &closed, expect["close_raised"].as_array().expect("close_raised"));
}

#[tokio::test]
async fn lifecycle_fixtures_lease_close_and_release() {
    let names = fixture_names("lifecycle");
    assert_ne!(names.len(), 0, "no lifecycle fixtures");
    for name in names {
        // Each fixture's relative path resolves against the working
        // directory, so its file is removed before and after.
        let lease_file = PathBuf::from("agents/42.sqlite");
        let _ = std::fs::remove_file(&lease_file);
        check_lifecycle(&name).await;
        let _ = std::fs::remove_file(&lease_file);
    }
    let _ = std::fs::remove_dir("agents");
}
