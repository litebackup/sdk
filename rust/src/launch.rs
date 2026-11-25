//! Starting a session's daemon with `litebackup launch`, as the command does.
//! The launch command takes the session's launch lock, so concurrent
//! processes start one daemon. Launches are never retried.

use std::path::Path;
use std::time::Duration;

use tokio::net::UnixStream;

use crate::binary;
use crate::child::{ChildCommand, exit_description};
use crate::credential::API_KEY_ENVIRONMENT;
use crate::environment::Environment;
use crate::error::{Error, ErrorCode, Result};

/// How long `litebackup launch` may take.
const LAUNCH_TIMEOUT: Duration = Duration::from_secs(30);
/// How long checking the socket may take.
const PROBE_TIMEOUT: Duration = Duration::from_secs(1);

/// Whether something accepts connections on the socket.
async fn socket_listening(socket_path: &Path) -> bool {
    matches!(tokio::time::timeout(PROBE_TIMEOUT, UnixStream::connect(socket_path)).await, Ok(Ok(_)))
}

/// The code and message of the last error the launch command reported as
/// JSON: `drain_required` when the launch refused a new credential for a
/// client that has write locks, and `launch_failed` otherwise, with the whole
/// output when the command reported no error as JSON.
pub(crate) fn launch_refusal(output: &str) -> (&'static str, String) {
    let mut refusal = None;
    for line in output.lines() {
        let Ok(serde_json::Value::Object(diagnostic)) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        // A warning carries an event; an error does not.
        if diagnostic.contains_key("event") {
            continue;
        }
        let (Some(code), Some(message)) = (
            diagnostic.get("code").and_then(serde_json::Value::as_str),
            diagnostic.get("message").and_then(serde_json::Value::as_str).filter(|message| !message.is_empty()),
        ) else {
            continue;
        };
        let refusal_code =
            if code == ErrorCode::DRAIN_REQUIRED { ErrorCode::DRAIN_REQUIRED } else { ErrorCode::LAUNCH_FAILED };
        refusal = Some((refusal_code, message.to_owned()));
    }
    refusal.unwrap_or_else(|| (ErrorCode::LAUNCH_FAILED, output.trim().to_owned()))
}

fn launch_failed(output: &str, cause: &str) -> Error {
    let (code, refusal) = launch_refusal(output);
    let reason = if refusal.is_empty() { cause.to_owned() } else { refusal };
    Error::new(code, format!("starting Litebackup failed: {reason}"))
}

/// Starts the session daemon when nothing listens on its socket, passing the
/// client's credential option as `LITEBACKUP_API_KEY` when it has one, an
/// empty one included, which clears the environment's.
pub(crate) async fn launch_daemon(
    session: &str,
    socket_path: &Path,
    environment: &Environment,
    api_key_variable: Option<&str>,
    binary_path: Option<&Path>,
) -> Result<()> {
    if socket_listening(socket_path).await {
        return Ok(());
    }
    let binary = binary::litebackup_binary(environment, binary_path)?;
    let mut launch_environment = environment.clone();
    launch_environment.set_variable(binary::PATH_ENVIRONMENT, &binary);
    if let Some(api_key) = api_key_variable {
        launch_environment.set_variable(API_KEY_ENVIRONMENT, api_key);
    }
    let command = [
        binary.to_string_lossy().into_owned(),
        "launch".to_owned(),
        "--session".to_owned(),
        session.to_owned(),
        "--stdout-format".to_owned(),
        "json".to_owned(),
    ];
    let mut launch = ChildCommand::start(&command, &launch_environment, true)?;
    let status = match tokio::time::timeout(LAUNCH_TIMEOUT, launch.finish()).await {
        Ok(Ok(status)) => status,
        Ok(Err(error)) => return Err(launch_failed("", &format!("waiting for litebackup launch failed: {error}"))),
        Err(_) => {
            launch.kill().await;
            return Err(launch_failed(
                "",
                &format!("litebackup launch did not finish within {} s", LAUNCH_TIMEOUT.as_secs()),
            ));
        }
    };
    if status.success() {
        return Ok(());
    }
    let error_output = launch.error_output.text();
    let output = if error_output.is_empty() { launch.output.text() } else { error_output };
    Err(launch_failed(&output, &exit_description(status)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_last_reported_error_is_the_refusal() {
        let output = "{\"event\":\"warning\",\"code\":\"x\",\"message\":\"a warning\"}\n\
                      {\"code\":\"drain_required\",\"message\":\"drain first\"}\n";
        assert_eq!(launch_refusal(output), (ErrorCode::DRAIN_REQUIRED, "drain first".to_owned()));
        assert_eq!(launch_refusal("  plain text\n"), (ErrorCode::LAUNCH_FAILED, "plain text".to_owned()));
    }
}
