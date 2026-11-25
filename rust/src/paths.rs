//! Session names and sockets, resolved exactly as the `litebackup` command does.

use std::os::unix::fs::{DirBuilderExt, MetadataExt};
use std::path::{Path, PathBuf};

use crate::environment::Environment;
use crate::error::{Error, Result};

/// Records a `litebackup install service` install; root and the service's
/// user use its socket.
const SYSTEM_CONFIGURATION_PATH: &str = "/etc/litebackup/client.json";
const DEFAULT_SESSION: &str = "default";

/// This process's user ID.
pub(crate) fn user_id() -> u32 {
    rustix::process::getuid().as_raw()
}

/// The runtime directory and user ID a service configuration file names.
fn runtime_directory(path: &Path) -> Option<(PathBuf, Option<u32>)> {
    let text = std::fs::read_to_string(path).ok()?;
    let configuration: serde_json::Value = serde_json::from_str(&text).ok()?;
    let directory = configuration.get("runtime_directory")?.as_str().filter(|directory| !directory.is_empty())?;
    let uid = configuration.get("uid").and_then(serde_json::Value::as_u64).and_then(|uid| u32::try_from(uid).ok());
    Some((PathBuf::from(directory), uid))
}

/// Whether a session name is one the command accepts: `[a-z0-9._-]`,
/// starting with a letter or digit.
fn valid_session(name: &str) -> bool {
    let mut bytes = name.bytes();
    bytes.next().is_some_and(|first| first.is_ascii_lowercase() || first.is_ascii_digit())
        && bytes.all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-'))
}

/// The session name and its socket path.
///
/// Where `litebackup install service` ran as root, root and the service's
/// user use the service's socket; where a user installed a service of their
/// own, that user uses the directory it saved. Everyone else uses
/// `$XDG_RUNTIME_DIR/litebackup/<session>/client.sock`, or a per-user
/// directory under the temporary directory, which must be this user's alone.
pub(crate) fn session_socket(session: Option<&str>, environment: &Environment) -> Result<(String, PathBuf)> {
    session_socket_with(session, environment, Path::new(SYSTEM_CONFIGURATION_PATH))
}

pub(crate) fn session_socket_with(
    session: Option<&str>,
    environment: &Environment,
    system_configuration: &Path,
) -> Result<(String, PathBuf)> {
    let name = session
        .filter(|session| !session.is_empty())
        .or_else(|| environment.text("LITEBACKUP_SESSION"))
        .unwrap_or(DEFAULT_SESSION)
        .to_owned();
    if !valid_session(&name) {
        return Err(Error::invalid_argument(format!(
            "session names use [a-z0-9._-] and start with a letter or digit, got {name:?}"
        )));
    }
    let socket = |directory: &Path| directory.join(&name).join("client.sock");
    let uid = user_id();
    if let Some((directory, service_uid)) = runtime_directory(system_configuration)
        && (uid == 0 || service_uid == Some(uid))
    {
        return Ok((name.clone(), socket(&directory)));
    }
    let home = environment.home();
    if let Some((directory, _)) = runtime_directory(&home.join(".config/litebackup/service.json")) {
        return Ok((name.clone(), socket(&directory)));
    }
    let runtime_root = if let Some(runtime) = environment.non_empty("XDG_RUNTIME_DIR") {
        PathBuf::from(runtime)
    } else {
        let temporary = environment.non_empty("TMPDIR").map_or_else(std::env::temp_dir, PathBuf::from);
        let root = temporary.join(format!("litebackup-{uid}"));
        private_runtime_directory(&root, uid)?;
        root
    };
    Ok((name.clone(), socket(&runtime_root.join("litebackup"))))
}

/// Makes sure the per-user runtime directory used without `XDG_RUNTIME_DIR`
/// is this user's own. The temporary directory is shared, so another local
/// user could create it first and answer as the daemon. It is created with
/// mode 0700 when missing; one that is a symlink, is not a directory, belongs
/// to another user, or is open to anyone else is refused, as the command
/// refuses it.
fn private_runtime_directory(directory: &Path, uid: u32) -> Result<()> {
    if let Err(error) = std::fs::DirBuilder::new().mode(0o700).create(directory)
        && error.kind() != std::io::ErrorKind::AlreadyExists
    {
        return Err(Error::invalid_argument(format!("creating {} failed: {error}", directory.display())));
    }
    // No permission bit for the group or anyone else.
    let shared_bits = 0o077;
    let own = std::fs::symlink_metadata(directory)
        .is_ok_and(|metadata| metadata.is_dir() && metadata.uid() == uid && metadata.mode() & shared_bits == 0);
    if own {
        return Ok(());
    }
    Err(Error::invalid_argument(format!(
        "{} is not a directory of this user's alone; remove it or set XDG_RUNTIME_DIR to this user's own directory",
        directory.display()
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_names_follow_the_command() {
        assert!(valid_session("default"));
        assert!(valid_session("a.b-c_9"));
        assert!(!valid_session("Default"));
        assert!(!valid_session("-a"));
        assert!(!valid_session(""));
    }

    #[test]
    fn the_runtime_directory_holds_the_session_socket() {
        let environment = Environment::from_variables([("XDG_RUNTIME_DIR", "/run/user/7"), ("HOME", "/nonexistent")]);
        let resolved = session_socket_with(Some("work"), &environment, Path::new("/nonexistent/client.json"));
        assert_eq!(resolved.ok(), Some(("work".to_owned(), PathBuf::from("/run/user/7/litebackup/work/client.sock"))));
    }
}
