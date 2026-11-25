//! The contract fixtures whose behavior is crate-private: telling credentials
//! apart, and finding the binary. The integration tests run the rest.

use std::os::unix::fs::PermissionsExt as _;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::binary::{INSTALL_COMMAND, PATH_ENVIRONMENT, litebackup_binary};
use crate::credential::credential_type;
use crate::environment::Environment;

fn fixture_directory(directory: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../testdata/contract").join(directory)
}

fn read_fixture(path: &Path) -> Value {
    serde_json::from_str(&std::fs::read_to_string(path).expect("the fixture")).expect("the fixture's JSON")
}

#[test]
fn credential_fixtures_tell_api_keys_from_join_tokens() {
    let mut paths: Vec<PathBuf> = std::fs::read_dir(fixture_directory("credentials"))
        .expect("the credentials fixtures")
        .map(|entry| entry.expect("a fixture").path())
        .collect();
    paths.sort();
    assert_ne!(paths.len(), 0, "no credential fixtures");
    for path in paths {
        let fixture = read_fixture(&path);
        let api_key = fixture["api_key"].as_str().expect("an api_key");
        let checked = credential_type(Some(api_key));
        if let Some(expected) = fixture.get("expect") {
            let error = checked.expect_err(&format!("{} should be refused", path.display()));
            assert_eq!(error.code(), expected["code"].as_str().expect("a code"), "{}", path.display());
            let secret = api_key.trim().split('@').next().unwrap_or_default();
            if !secret.is_empty() {
                assert!(!error.message().contains(secret), "{} repeats the credential", path.display());
            }
            continue;
        }
        let found = checked.unwrap_or_else(|error| panic!("{}: {error}", path.display()));
        assert_eq!(
            found.map(super::credential::CredentialType::as_str),
            fixture["type"].as_str(),
            "{}",
            path.display()
        );
    }
}

/// A file with the content, made executable.
fn executable_file(path: &Path, content: &str) {
    std::fs::create_dir_all(path.parent().expect("a directory")).expect("the file's directory");
    std::fs::write(path, content).expect("the file");
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).expect("its mode");
}

#[test]
fn a_missing_override_is_binary_not_found_without_falling_back() {
    let fixture = read_fixture(&fixture_directory("binary").join("override-missing.json"));
    let directory = tempfile::tempdir().expect("a temporary directory");
    let missing = directory.path().join(fixture["missing_file"].as_str().expect("missing_file"));
    // A binary on PATH is never the fallback for an override that names no file.
    executable_file(&directory.path().join("bin/litebackup"), "#!/bin/sh\n");
    let path_variable = directory.path().join("bin");
    for override_source in fixture["overrides"].as_array().expect("overrides") {
        let mut environment = Environment::from_variables([("PATH", path_variable.as_os_str())]);
        let option = match override_source.as_str() {
            Some("option") => Some(missing.as_path()),
            Some("environment") => {
                environment.set_variable(PATH_ENVIRONMENT, &missing);
                None
            }
            unknown => panic!("an override kind: {unknown:?}"),
        };
        let error = litebackup_binary(&environment, option).expect_err("a missing override");
        assert_eq!(error.code(), fixture["code"].as_str().expect("code"));
        assert!(error.message().contains(fixture["message_contains"].as_str().expect("message_contains")));
        assert!(error.message().contains(&missing.display().to_string()), "{error}");
    }
}

#[test]
fn an_override_behind_a_locked_directory_is_found() {
    if rustix::process::geteuid().is_root() {
        return;
    }
    let fixture = read_fixture(&fixture_directory("binary").join("override-missing.json"));
    let directory = tempfile::tempdir().expect("a temporary directory");
    let denied = directory.path().join(fixture["denied_file"].as_str().expect("denied_file"));
    executable_file(&denied, "#!/bin/sh\n");
    let locked = denied.parent().expect("its directory").to_path_buf();
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).expect("locking it");
    let found = litebackup_binary(&Environment::default(), Some(&denied));
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o755)).expect("unlocking it");
    assert_eq!(found.ok(), Some(denied));
}

#[test]
fn the_installed_command_is_found_on_path_then_in_local_bin() {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let on_path = directory.path().join("bin/litebackup");
    let local = directory.path().join(".local/bin/litebackup");
    executable_file(&on_path, "#!/bin/sh\n");
    executable_file(&local, "#!/bin/sh\n");
    let home = directory.path().as_os_str();
    let environment =
        |path_variable: &Path| Environment::from_variables([("PATH", path_variable.as_os_str()), ("HOME", home)]);
    assert_eq!(litebackup_binary(&environment(&directory.path().join("bin")), None).ok(), Some(on_path));
    assert_eq!(litebackup_binary(&environment(Path::new("/nonexistent")), None).ok(), Some(local.clone()));
    std::fs::remove_file(&local).expect("removing it");
    let error = litebackup_binary(&environment(Path::new("/nonexistent")), None).expect_err("no binary");
    assert_eq!(error.code(), "binary_not_found");
    assert!(error.message().contains(INSTALL_COMMAND));
}
