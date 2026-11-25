//! Finding the litebackup binary. The crate bundles none, as the binary is
//! over the crates.io size limit, and nothing is downloaded at runtime.

use std::io::Read as _;
use std::path::{Path, PathBuf};

use crate::environment::Environment;
use crate::error::{Error, ErrorCode, Result};

/// Names the litebackup binary, overriding the search.
pub(crate) const PATH_ENVIRONMENT: &str = "LITEBACKUP_PATH";

/// The one line that installs the litebackup command on Linux and macOS for
/// whoever runs it.
pub(crate) const INSTALL_COMMAND: &str = "curl -fsSL https://litebackup.com/cli.sh | bash";

/// Whether a file is there and this user may run it.
fn executable(path: &Path) -> bool {
    path.is_file() && rustix::fs::access(path, rustix::fs::Access::EXEC_OK).is_ok()
}

/// Finds the litebackup binary: the explicit path, then `LITEBACKUP_PATH`,
/// then `litebackup` on `PATH`, then `~/.local/bin/litebackup`. Fails with
/// `binary_not_found` and the install command when there is none, or when
/// the explicit path or `LITEBACKUP_PATH` names no file; a file that is there
/// but cannot run fails later, as `launch_failed`.
pub(crate) fn litebackup_binary(environment: &Environment, binary_path: Option<&Path>) -> Result<PathBuf> {
    let explicit = binary_path
        .filter(|path| !path.as_os_str().is_empty())
        .map(Path::to_path_buf)
        .or_else(|| environment.non_empty(PATH_ENVIRONMENT).map(PathBuf::from));
    if let Some(explicit) = explicit {
        if missing_path(&explicit) {
            return Err(Error::new(
                ErrorCode::BINARY_NOT_FOUND,
                format!(
                    "the litebackup binary {} does not exist; set the client's binary path or {PATH_ENVIRONMENT} to it, \
                     or install it with: {INSTALL_COMMAND}",
                    explicit.display()
                ),
            ));
        }
        return Ok(explicit);
    }
    if let Some(found) = environment
        .get("PATH")
        .into_iter()
        .flat_map(std::env::split_paths)
        .filter(|directory| !directory.as_os_str().is_empty())
        .map(|directory| directory.join("litebackup"))
        .find(|candidate| executable(candidate))
    {
        return Ok(found);
    }
    let local = environment.home().join(".local/bin/litebackup");
    if executable(&local) {
        return Ok(local);
    }
    Err(Error::new(
        ErrorCode::BINARY_NOT_FOUND,
        format!(
            "the litebackup command is not installed; set {PATH_ENVIRONMENT} or install it with: {INSTALL_COMMAND}"
        ),
    ))
}

/// Whether nothing is at path: it, or a directory on its way, does not
/// exist. A path behind a directory this user may not enter is not missing.
pub(crate) fn missing_path(path: &Path) -> bool {
    if std::fs::metadata(path).is_ok() {
        return false;
    }
    // The lookup fails for a missing file and for one behind a directory
    // this user may not enter; only the first has a missing step on its way.
    let mut directory = path;
    while let Some(parent) = directory.parent() {
        let parent = if parent.as_os_str().is_empty() { Path::new(".") } else { parent };
        if let Ok(metadata) = std::fs::metadata(parent) {
            // A file where a directory should be is missing too.
            return !metadata.is_dir() || rustix::fs::access(parent, rustix::fs::Access::EXEC_OK).is_ok();
        }
        if parent == directory {
            break;
        }
        directory = parent;
    }
    true
}

/// Why starting the binary failed when it is there: it cannot run, because
/// the interpreter its `#!` line names, or its dynamic loader, is not on this
/// machine, or because it is not executable. The message names that line
/// rather than repeating the file's path as if it were missing.
pub(crate) fn start_failure_message(binary: &Path, reason: &str) -> String {
    if missing_path(binary) {
        return format!("starting {} failed: {reason}", binary.display());
    }
    let unusable = format!("starting {} failed: it cannot run because", binary.display());
    match script_interpreter_line(binary) {
        Some(line) => format!("{unusable} the interpreter its first line names is not on this machine: {line}"),
        None => format!("{unusable} its interpreter or dynamic loader is not on this machine"),
    }
}

/// The `#!` line that starts the file, or `None` when it starts with none or
/// cannot be read.
fn script_interpreter_line(path: &Path) -> Option<String> {
    let mut start = Vec::with_capacity(256);
    std::fs::File::open(path).ok()?.take(256).read_to_end(&mut start).ok()?;
    let text = String::from_utf8_lossy(&start);
    let first_line = text.split('\n').next().unwrap_or_default();
    first_line.starts_with("#!").then(|| first_line.trim().to_owned())
}

/// `binary_not_found` when the binary is missing, `launch_failed` when it is
/// there but cannot run.
pub(crate) fn start_failure(binary: &Path, reason: &str) -> Error {
    let code = if missing_path(binary) { ErrorCode::BINARY_NOT_FOUND } else { ErrorCode::LAUNCH_FAILED };
    Error::new(code, start_failure_message(binary, reason))
}
