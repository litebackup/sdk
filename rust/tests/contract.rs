//! The language-neutral contract fixtures in `../testdata/contract`, which
//! the Go, TypeScript, Python, Ruby, and PHP SDKs run too, against the fake
//! daemon: requests, errors, version negotiation, pagination, and events.
//! Operations map to the Rust API by hand: `group.method` is
//! `client.group().method(..)`, the first argument positional, the rest an
//! options struct, durations as `Duration`, times as `jiff::Timestamp`, and
//! `{"$path": ...}` as a `PathBuf`.
#![allow(clippy::expect_used, clippy::unwrap_used, reason = "tests assert with expect")]

mod support;

use std::collections::BTreeMap;
use std::path::PathBuf;

use futures_util::StreamExt as _;
use litebackup::{
    AcquireOptions, BackupOptions, Client, Durability, Error, EventsOptions, OffloadOptions, PageOptions,
    PrefixReplicaOptions, ReleaseOptions, ReplicaOptions, RestoreOptions, RevertOptions, ShutdownOptions,
    VersionsOptions, WatchOptions, WriteProtection,
};
use serde_json::{Map, Value, json};
use support::{Answer, FakeDaemon, client_to, duration, fixture_names, read_fixture, skip_reason};

/// A fixture's arguments, read by the type each one has.
struct Arguments<'a>(&'a Map<String, Value>);

impl Arguments<'_> {
    fn text(&self, key: &str) -> Option<String> {
        self.0.get(key).and_then(Value::as_str).map(str::to_owned)
    }

    /// A required text argument; empty when the fixture leaves it out, as a
    /// caller passing nothing would.
    fn required_text(&self, key: &str) -> String {
        self.text(key).unwrap_or_default()
    }

    /// A path: a string, or `{"$path": ...}` as a `PathBuf`.
    fn path(&self, key: &str) -> Option<PathBuf> {
        match self.0.get(key)? {
            Value::String(path) => Some(PathBuf::from(path)),
            Value::Object(path) => path.get("$path").and_then(Value::as_str).map(PathBuf::from),
            _ => None,
        }
    }

    fn flag(&self, key: &str) -> bool {
        self.0.get(key).and_then(Value::as_bool).unwrap_or(false)
    }

    fn integer(&self, key: &str) -> Option<i64> {
        self.0.get(key).and_then(Value::as_i64)
    }

    fn count(&self, key: &str) -> Option<u32> {
        self.integer(key).map(|value| u32::try_from(value).expect("a count"))
    }

    fn duration(&self, key: &str) -> Option<std::time::Duration> {
        self.text(key).map(|text| duration(&text))
    }

    fn time(&self, key: &str) -> Option<jiff::Timestamp> {
        self.text(key).map(|text| text.parse().expect("an RFC 3339 time"))
    }

    fn durability(&self, key: &str) -> Option<Durability> {
        self.text(key).map(Durability::new)
    }
}

/// Runs a fixture's operation and returns its answer as JSON.
async fn invoke(client: &Client, operation: &str, args: &Map<String, Value>) -> Result<Value, Error> {
    let args = Arguments(args);
    let name = args.required_text("name");
    let answer = match operation {
        "backups.start" => {
            let mut options = BackupOptions::default();
            options.name = args.text("name");
            options.prefix = args.text("prefix");
            options.maximum_local_databases = args.count("maximum_local_databases");
            options.offload_durability = args.durability("offload_durability");
            options.offload_rollback_journal = args.flag("offload_rollback_journal");
            to_json(client.backups().start(args.path("source").unwrap_or_default(), options).await?)
        }
        "backups.stop" => to_json(client.backups().stop(&name).await?),
        "rules.list" => to_json(client.rules().list().await?),
        "rules.stop" => to_json(client.rules().stop(&args.required_text("id")).await?),
        "databases.get" => to_json(client.databases().get(&name).await?),
        "databases.page" => to_json(client.databases().page(page_options(&args)).await?),
        "databases.sync" => to_json(client.databases().sync(&name).await?),
        "databases.writer" => to_json(client.databases().writer(&name).await?),
        "databases.offload" => {
            let mut options = OffloadOptions::default();
            options.durability = args.durability("durability");
            to_json(client.databases().offload(&name, options).await?)
        }
        "databases.restore" => {
            let mut options = RestoreOptions::default();
            options.version = args.integer("version");
            options.time = args.time("time");
            options.umask = args.count("umask");
            to_json(client.databases().restore(&name, args.path("path").unwrap_or_default(), options).await?)
        }
        "databases.revert" => {
            let mut options = RevertOptions::default();
            options.version = args.integer("version");
            options.time = args.time("time");
            to_json(client.databases().revert(&name, options).await?)
        }
        "databases.acquire" => {
            let mut options = AcquireOptions::default();
            options.path = args.path("path");
            options.maintenance = args.flag("maintenance");
            options.offload_on_release = args.flag("offload_on_release");
            options.offload_durability = args.durability("offload_durability");
            options.machine_label = args.text("machine_label");
            options.timeout = args.duration("timeout");
            options.lock_timeout = args.duration("lock_timeout");
            options.umask = args.count("umask");
            options.discard_local = args.flag("discard_local");
            to_json(client.databases().acquire(&name, options).await?)
        }
        "databases.release" => {
            let mut options = ReleaseOptions::default();
            options.durability = args.durability("durability");
            options.offload = args.flag("offload");
            options.timeout = args.duration("timeout");
            to_json(client.databases().release(&name, options).await?)
        }
        "databases.versions" => to_json(client.databases().versions(&name, versions_options(&args)).await?),
        "replicas.start" => {
            let mut options = ReplicaOptions::default();
            options.path = args.path("path");
            options.write_protection = args.text("write_protection").map(WriteProtection::new);
            options.timeout = args.duration("timeout");
            to_json(client.replicas().start(&name, options).await?)
        }
        "replicas.start_prefix" => {
            let mut options = PrefixReplicaOptions::default();
            options.write_protection = args.text("write_protection").map(WriteProtection::new);
            let directory = args.path("directory").unwrap_or_default();
            to_json(client.replicas().start_prefix(&args.required_text("prefix"), directory, options).await?)
        }
        "replicas.list" => to_json(client.replicas().list().await?),
        "replicas.stop" => to_json(client.replicas().stop(&name).await?),
        "replicas.stop_prefix" => {
            let directory = args.path("directory").unwrap_or_default();
            to_json(client.replicas().stop_prefix(&args.required_text("prefix"), directory).await?)
        }
        "events.page" => {
            let mut options = EventsOptions::default();
            options.after = args.integer("after").unwrap_or(0);
            options.wait = args.duration("wait");
            options.limit = args.count("limit");
            to_json(client.events().page(options).await?)
        }
        "session.status" => to_json(client.session().status(page_options(&args)).await?),
        "session.shutdown" => {
            let mut options = ShutdownOptions::default();
            options.durability = args.durability("durability");
            options.timeout = args.duration("timeout");
            to_json(client.session().shutdown(options).await?)
        }
        unknown => panic!("no Rust call for the fixture operation {unknown}"),
    };
    Ok(answer)
}

fn page_options(args: &Arguments<'_>) -> PageOptions {
    let mut options = PageOptions::default();
    options.after = args.text("after");
    options.limit = args.count("limit");
    options
}

fn versions_options(args: &Arguments<'_>) -> VersionsOptions {
    let mut options = VersionsOptions::default();
    options.after = args.integer("after");
    options.limit = args.count("limit");
    options
}

fn to_json(answer: impl serde::Serialize) -> Value {
    serde_json::to_value(answer).expect("the answer as JSON")
}

/// The items a listing or a watch yields, up to `limit`, and the error that
/// ended it.
async fn listed_items(
    client: &Client,
    operation: &str,
    args: &Map<String, Value>,
    limit: Option<usize>,
) -> (Vec<Value>, Option<Error>) {
    let arguments = Arguments(args);
    let stream = match operation {
        "databases.all" => client.databases().all().map(|item| item.map(to_json)).boxed(),
        "databases.versions" => client
            .databases()
            .all_versions(&arguments.required_text("name"), versions_options(&arguments))
            .map(|item| item.map(to_json))
            .boxed(),
        "events.watch" => {
            let mut options = WatchOptions::default();
            options.after = arguments.integer("after").unwrap_or(0);
            options.wait = arguments.duration("wait");
            client.events().watch(options).map(|item| item.map(to_json)).boxed()
        }
        unknown => panic!("no Rust listing for the fixture operation {unknown}"),
    };
    let mut stream = stream.take(limit.unwrap_or(usize::MAX));
    let mut items = Vec::new();
    while let Some(item) = stream.next().await {
        match item {
            Ok(item) => items.push(item),
            Err(error) => return (items, Some(error)),
        }
    }
    (items, None)
}

/// A fixture's query as a map of strings.
fn query_map(query: Option<&Value>) -> BTreeMap<String, String> {
    query
        .and_then(Value::as_object)
        .map(|query| {
            query
                .iter()
                .map(|(key, value)| (key.clone(), value.as_str().expect("query values are text").to_owned()))
                .collect()
        })
        .unwrap_or_default()
}

/// The path an operation's answer is for, from the fixture's request.
fn request_path(fixture: &Map<String, Value>) -> Option<String> {
    fixture.get("request")?.get("path")?.as_str().map(str::to_owned)
}

#[tokio::test]
async fn request_fixtures_send_the_expected_request() {
    for name in fixture_names("requests") {
        let fixture = read_fixture("requests", &name);
        if let Some(reason) = skip_reason(&fixture) {
            eprintln!("skipping requests/{name}: {reason}");
            continue;
        }
        let mut answers = Vec::new();
        if let Some(path) = request_path(&fixture) {
            answers.push(Answer::json(&path, &fixture.get("response").cloned().unwrap_or_else(|| json!({}))));
        }
        let daemon = FakeDaemon::start(answers);
        let client = client_to(&daemon).await;
        let operation = fixture["operation"].as_str().expect("an operation");
        let args = fixture.get("args").and_then(Value::as_object).cloned().unwrap_or_default();
        let called = invoke(&client, operation, &args).await;
        let requests = daemon.requests();
        if let Some(expected) = fixture.get("error") {
            let error = called.expect_err(&format!("requests/{name} should fail"));
            assert_eq!(error.code(), expected["code"].as_str().expect("a code"), "requests/{name}: {error}");
            assert!(requests.is_empty(), "requests/{name} sent {requests:?}");
            continue;
        }
        called.unwrap_or_else(|error| panic!("requests/{name} failed: {error}"));
        assert_eq!(requests.len(), 1, "requests/{name} sent {requests:?}");
        let expected = &fixture["request"];
        let sent = &requests[0];
        assert_eq!(sent.method, expected["method"].as_str().expect("a method"), "requests/{name}");
        assert_eq!(Some(sent.path.clone()), request_path(&fixture), "requests/{name}");
        assert_eq!(sent.query, query_map(expected.get("query")), "requests/{name}");
        let body = expected.get("body").cloned().unwrap_or(Value::Null);
        assert_eq!(sent.body, body, "requests/{name}");
        assert_eq!(sent.sdk_version, format!("rust/{}", litebackup::VERSION), "requests/{name}");
    }
}

#[tokio::test]
async fn error_fixtures_decode_the_daemons_answer() {
    for name in fixture_names("errors") {
        let fixture = read_fixture("errors", &name);
        let response = &fixture["response"];
        let status = u16::try_from(response["status"].as_u64().expect("a status")).expect("an HTTP status");
        let answer = Answer::text(
            "/v1/backup-rules/list",
            status,
            response["content_type"].as_str().expect("a content type"),
            response["body"].as_str().expect("a body"),
        );
        let daemon = FakeDaemon::start(vec![answer]);
        let client = client_to(&daemon).await;
        let operation = fixture["operation"].as_str().expect("an operation");
        let error = invoke(&client, operation, &Map::new()).await.expect_err(&format!("errors/{name} should fail"));
        let expected = &fixture["expect"];
        assert_eq!(error.code(), expected["code"].as_str().expect("a code"), "errors/{name}");
        assert_eq!(error.message(), expected["message"].as_str().expect("a message"), "errors/{name}");
        assert_eq!(u64::from(error.status()), expected["status"].as_u64().expect("a status"), "errors/{name}");
        assert_eq!(&Value::Object(error.details().clone()), &expected["details"], "errors/{name}");
    }
}

#[tokio::test]
async fn version_fixtures_accept_or_refuse_the_daemon() {
    for name in fixture_names("version") {
        let fixture = read_fixture("version", &name);
        let daemon = FakeDaemon::start(Vec::new());
        daemon.set_client_version(fixture["client_version"].as_str());
        let connected = Client::builder().socket_path(&daemon.socket_path).connect().await;
        if fixture["accepted"].as_bool().expect("accepted") {
            connected.unwrap_or_else(|error| panic!("version/{name} was refused: {error}"));
            continue;
        }
        let error = connected.expect_err(&format!("version/{name} was accepted"));
        assert_eq!(error.code(), fixture["expect"]["code"].as_str().expect("a code"), "version/{name}");
        assert_eq!(
            u64::from(error.status()),
            fixture["expect"]["status"].as_u64().expect("a status"),
            "version/{name}"
        );
        assert!(error.message().contains(litebackup::MINIMUM_DAEMON_VERSION), "version/{name}: {error}");
    }
}

/// Runs the fixtures of a directory whose operations yield items over
/// several exchanges: pagination and events.
async fn check_exchange_fixtures(directory: &str) {
    for name in fixture_names(directory) {
        let fixture = read_fixture(directory, &name);
        if let Some(reason) = skip_reason(&fixture) {
            eprintln!("skipping {directory}/{name}: {reason}");
            continue;
        }
        let exchanges = fixture["exchanges"].as_array().expect("exchanges");
        let answers = exchanges
            .iter()
            .map(|exchange| {
                let request = &exchange["request"];
                let response = &exchange["response"];
                let status = u16::try_from(response["status"].as_u64().expect("a status")).expect("an HTTP status");
                Answer::status(request["path"].as_str().expect("a path"), status, &response["body"])
                    .query(query_map(request.get("query")))
                    .uses(1)
            })
            .collect();
        let daemon = FakeDaemon::start(answers);
        let client = client_to(&daemon).await;
        let operation = fixture["operation"].as_str().expect("an operation");
        let args = fixture.get("args").and_then(Value::as_object).cloned().unwrap_or_default();
        let limit =
            fixture.get("item_limit").and_then(Value::as_u64).map(|limit| usize::try_from(limit).expect("a limit"));
        let (items, error) = listed_items(&client, operation, &args, limit).await;
        let key = fixture["item_key"].as_str().expect("an item key");
        let keys: Vec<Value> = items.iter().map(|item| item.get(key).cloned().unwrap_or(Value::Null)).collect();
        let expected = &fixture["expect"];
        assert_eq!(&Value::Array(keys), &expected["items"], "{directory}/{name}");
        match (expected.get("error"), error) {
            (Some(expected_error), Some(error)) => {
                assert_eq!(error.code(), expected_error["code"].as_str().expect("a code"), "{directory}/{name}");
            }
            (None, None) => {}
            (expected_error, error) => panic!("{directory}/{name}: expected {expected_error:?}, got {error:?}"),
        }
        let requests = daemon.requests();
        assert_eq!(requests.len(), exchanges.len(), "{directory}/{name} sent {requests:?}");
        for (sent, exchange) in requests.iter().zip(exchanges) {
            assert_eq!(sent.path, exchange["request"]["path"].as_str().expect("a path"), "{directory}/{name}");
            assert_eq!(sent.query, query_map(exchange["request"].get("query")), "{directory}/{name}");
        }
    }
}

#[tokio::test]
async fn pagination_fixtures_follow_the_cursor() {
    check_exchange_fixtures("pagination").await;
}

#[tokio::test]
async fn event_fixtures_follow_the_log() {
    check_exchange_fixtures("events").await;
}
