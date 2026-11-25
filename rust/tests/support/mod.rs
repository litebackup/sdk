//! What the integration tests share: the fake daemon and the contract
//! fixtures in `../testdata/contract`, which every SDK runs.
#![allow(dead_code, reason = "each test binary uses part of the support module")]

pub mod daemon;

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{Map, Value};

#[allow(unused_imports, reason = "each test binary uses part of the support module")]
pub use daemon::{Answer, FakeDaemon};

/// The contract fixtures' directory.
pub fn fixture_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../testdata/contract")
}

/// The names of a fixture directory's files, without `.json`, sorted.
pub fn fixture_names(directory: &str) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(fixture_root().join(directory))
        .expect("the fixture directory")
        .filter_map(|entry| {
            let name = entry.ok()?.file_name().into_string().ok()?;
            name.strip_suffix(".json").map(str::to_owned)
        })
        .collect();
    names.sort();
    names
}

/// The test process's working directory, which `{cwd}` names; the tests
/// never change it.
pub fn working_directory() -> String {
    std::env::current_dir().expect("the working directory").to_string_lossy().into_owned()
}

/// This process's umask, read from `/proc/self/status` apart from how the
/// SDK reads it.
pub fn umask_now() -> u32 {
    let status = std::fs::read_to_string("/proc/self/status").expect("/proc/self/status");
    let line = status.lines().find_map(|line| line.strip_prefix("Umask:")).expect("a Umask line");
    u32::from_str_radix(line.trim(), 8).expect("an octal umask")
}

/// A fixture, with `{cwd}` and `"{umask}"` replaced.
pub fn read_fixture(directory: &str, name: &str) -> Map<String, Value> {
    let path = fixture_root().join(directory).join(format!("{name}.json"));
    let text = std::fs::read_to_string(&path).expect("the fixture");
    let cwd = serde_json::to_string(&working_directory()).expect("the working directory as JSON");
    let text = text.replace("{cwd}", &cwd[1..cwd.len() - 1]).replace("\"{umask}\"", &umask_now().to_string());
    match serde_json::from_str(&text).expect("the fixture's JSON") {
        Value::Object(fixture) => fixture,
        _ => panic!("{} is not an object", path.display()),
    }
}

/// Why a fixture skips Rust, when it does.
pub fn skip_reason(fixture: &Map<String, Value>) -> Option<String> {
    fixture.get("skip")?.get("rust")?.as_str().map(str::to_owned)
}

/// A fixture duration such as `1500ms`, `90s`, or `2m`.
pub fn duration(text: &str) -> Duration {
    let split = text.find(|character: char| character.is_ascii_alphabetic()).expect("a unit");
    let (number, unit) = text.split_at(split);
    let value: f64 = number.parse().expect("a number of units");
    assert!(value >= 0.0, "Rust durations are never negative: {text}");
    let seconds = match unit {
        "ms" => value / 1000.0,
        "s" => value,
        "m" => value * 60.0,
        "h" => value * 3600.0,
        _ => panic!("a duration unit: {text}"),
    };
    Duration::from_secs_f64(seconds)
}

/// A client of the fake daemon's socket.
pub async fn client_to(daemon: &FakeDaemon) -> litebackup::Client {
    litebackup::Client::builder().socket_path(&daemon.socket_path).connect().await.expect("a client of the fake daemon")
}
