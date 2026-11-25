//! `backups().once()`: one foreground `litebackup backup` job, as the command runs it.

use std::path::Path;
use std::time::Duration;

use crate::error::{Error, ErrorCode, Result};
use crate::models::Durability;
use crate::wire;

/// How many of the job's file diagnostics a failed backup's message lists.
const MAXIMUM_FILE_DIAGNOSTICS: usize = 20;
/// How long a job has to exit after SIGTERM, when its client closes, before
/// it gets SIGKILL.
pub(crate) const STOP_GRACE: Duration = Duration::from_secs(15);

/// What one job backs up and how.
pub(crate) struct BackupJob<'a> {
    pub(crate) source: &'a Path,
    pub(crate) session: Option<&'a str>,
    pub(crate) name: Option<&'a str>,
    pub(crate) prefix: Option<&'a str>,
    pub(crate) durability: Option<&'a Durability>,
    pub(crate) timeout: Option<Duration>,
}

/// The command line of one foreground job.
pub(crate) fn backup_command(binary: &Path, job: &BackupJob<'_>) -> Result<Vec<String>> {
    if job.source.as_os_str().is_empty() {
        return Err(Error::invalid_argument("backup needs a path"));
    }
    if job.name.is_some() && job.prefix.is_some() {
        return Err(Error::invalid_argument("choose a name or a prefix"));
    }
    if let Some(name) = job.name
        && (name.is_empty() || name.ends_with('/'))
    {
        return Err(Error::invalid_argument("expected an exact name"));
    }
    let durability = checked_durability(job.durability)?.unwrap_or(Durability::SERVER);
    let mut command = vec![
        binary.to_string_lossy().into_owned(),
        "backup".to_owned(),
        "--stdout-format".to_owned(),
        "json".to_owned(),
        "--durability".to_owned(),
        durability.as_str().to_owned(),
    ];
    if let Some(session) = job.session.filter(|session| !session.is_empty()) {
        command.extend(["--session".to_owned(), session.to_owned()]);
    }
    if let Some(timeout) = job.timeout.filter(|timeout| !timeout.is_zero()) {
        command.extend(["--timeout".to_owned(), format!("{}ms", wire::millis(timeout))]);
    }
    command.extend(["--".to_owned(), wire::absolute_path(job.source)?]);
    if let Some(name) = job.name {
        command.push(name.to_owned());
    }
    if let Some(prefix) = job.prefix.filter(|prefix| !prefix.is_empty()) {
        command.push(if prefix.ends_with('/') { prefix.to_owned() } else { format!("{prefix}/") });
    }
    Ok(command)
}

/// Checks a durability an offload or a backup waits for: server or cloud.
pub(crate) fn checked_durability(durability: Option<&Durability>) -> Result<Option<Durability>> {
    match durability {
        None => Ok(None),
        Some(durability) if *durability == Durability::SERVER || *durability == Durability::CLOUD => {
            Ok(Some(durability.clone()))
        }
        Some(_) => Err(Error::invalid_argument("offload durability must be server or cloud")),
    }
}

/// The job's structured error on one line of its error output, or `None`
/// for a log line.
fn reported_failure(line: &str) -> Option<(String, String)> {
    let parsed: serde_json::Value = serde_json::from_str(line).ok()?;
    let code = parsed.get("code")?.as_str()?;
    let message = parsed.get("message")?.as_str()?;
    Some((code.to_owned(), message.to_owned()))
}

/// The job's final structured report, followed by each file diagnostic it
/// printed before it, or its text.
pub(crate) fn backup_failure(error_output: &str, exit: &str) -> Error {
    let mut final_report = None;
    let mut diagnostics: Vec<String> = Vec::new();
    for line in error_output.trim().lines() {
        let Some((code, message)) = reported_failure(line) else {
            continue;
        };
        // A file reported twice is listed once, so the limit counts distinct files.
        if diagnostics.len() < MAXIMUM_FILE_DIAGNOSTICS && !diagnostics.contains(&message) {
            diagnostics.push(message.clone());
        }
        final_report = Some((code, message));
    }
    let Some((code, message)) = final_report else {
        let text = error_output.trim();
        let message = if text.is_empty() { format!("foreground backup failed with {exit}") } else { text.to_owned() };
        return Error::new("backup_failed", message);
    };
    let mut listed = vec![message.clone()];
    let mut full_message = message;
    for diagnostic in diagnostics {
        if !listed.contains(&diagnostic) {
            full_message.push('\n');
            full_message.push_str(&diagnostic);
            listed.push(diagnostic);
        }
    }
    Error::new(code, full_message)
}

pub(crate) fn timed_out_backup() -> Error {
    Error::new(ErrorCode::TIMEOUT, "the foreground backup did not finish within its timeout")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_prefix_ends_with_a_slash() {
        let job = BackupJob {
            source: Path::new("/data"),
            session: Some("work"),
            name: None,
            prefix: Some("tenants"),
            durability: None,
            timeout: Some(Duration::from_millis(1500)),
        };
        let command = backup_command(Path::new("/bin/litebackup"), &job).ok();
        let expected = "/bin/litebackup backup --stdout-format json --durability server --session work --timeout 1500ms -- /data tenants/";
        assert_eq!(command.map(|command| command.join(" ")).as_deref(), Some(expected));
    }

    #[test]
    fn the_final_report_leads_its_file_diagnostics() {
        let output = "log line\n{\"code\":\"busy\",\"message\":\"a.sqlite is busy\"}\n{\"code\":\"busy\",\"message\":\"2 files failed\"}\n";
        let error = backup_failure(output, "exit status 1");
        assert_eq!((error.code(), error.message()), ("busy", "2 files failed\na.sqlite is busy"));
        assert_eq!(backup_failure("", "signal 9").message(), "foreground backup failed with signal 9");
    }
}
