//! Running a litebackup command: `litebackup launch` and foreground backups.

use std::path::Path;
use std::process::{ExitStatus, Stdio};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt as _};
use tokio::process::{Child, Command};

use crate::binary;
use crate::environment::Environment;
use crate::error::{Error, ErrorCode, Result};

/// How long a finished command's output pipes may stay open, in a process it
/// started such as the daemon, before reading stops.
pub(crate) const DRAIN_TIME: Duration = Duration::from_secs(1);

/// What a command wrote to one pipe, kept as it arrives so a pipe still open
/// when reading stops loses nothing read before.
#[derive(Debug, Clone, Default)]
pub(crate) struct CapturedOutput {
    bytes: Arc<Mutex<Vec<u8>>>,
}

impl CapturedOutput {
    /// Reads a pipe to its end in a task of its own.
    fn read_from(pipe: Option<impl AsyncRead + Unpin + Send + 'static>) -> (Self, Option<tokio::task::JoinHandle<()>>) {
        let output = Self::default();
        let reader = pipe.map(|mut pipe| {
            let bytes = Arc::clone(&output.bytes);
            tokio::spawn(async move {
                let mut chunk = [0_u8; 8192];
                while let Ok(read) = pipe.read(&mut chunk).await {
                    if read == 0 {
                        break;
                    }
                    bytes.lock().unwrap_or_else(PoisonError::into_inner).extend_from_slice(&chunk[..read]);
                }
            })
        });
        (output, reader)
    }

    /// The text read so far.
    pub(crate) fn text(&self) -> String {
        String::from_utf8_lossy(&self.bytes.lock().unwrap_or_else(PoisonError::into_inner)).into_owned()
    }
}

/// A running command and what it writes.
pub(crate) struct ChildCommand {
    pub(crate) child: Child,
    pub(crate) output: CapturedOutput,
    pub(crate) error_output: CapturedOutput,
    readers: Vec<tokio::task::JoinHandle<()>>,
}

impl ChildCommand {
    /// Starts a command with exactly `environment`, empty values included,
    /// and its standard output read or discarded. A command that cannot start
    /// is `binary_not_found` when its file is missing and `launch_failed` when
    /// it is there but cannot run. Dropping it kills the command.
    pub(crate) fn start(command: &[String], environment: &Environment, read_output: bool) -> Result<Self> {
        let (program, arguments) = command.split_first().ok_or_else(|| Error::invalid_argument("an empty command"))?;
        let mut process = Command::new(program);
        process
            .args(arguments)
            .env_clear()
            .envs(environment.variables())
            .stdin(Stdio::null())
            .stdout(if read_output { Stdio::piped() } else { Stdio::null() })
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        let mut child = process.spawn().map_err(|error| {
            // Not found for a file that is there means its #! interpreter or
            // its loader is missing; any other error, such as a busy or
            // forbidden file, says itself.
            if error.kind() == std::io::ErrorKind::NotFound {
                binary::start_failure(Path::new(program), &format!("it could not be started: {error}"))
            } else {
                Error::new(ErrorCode::LAUNCH_FAILED, format!("starting {program} failed: {error}")).with_source(error)
            }
        })?;
        let (output, output_reader) = CapturedOutput::read_from(child.stdout.take());
        let (error_output, error_reader) = CapturedOutput::read_from(child.stderr.take());
        let readers = output_reader.into_iter().chain(error_reader).collect();
        Ok(Self { child, output, error_output, readers })
    }

    /// Waits for the command to exit, then up to [`DRAIN_TIME`] for its pipes.
    pub(crate) async fn finish(&mut self) -> std::io::Result<ExitStatus> {
        let status = self.child.wait().await?;
        self.drain().await;
        Ok(status)
    }

    /// Reads what is left in the pipes for up to [`DRAIN_TIME`].
    pub(crate) async fn drain(&mut self) {
        let readers = std::mem::take(&mut self.readers);
        let aborts: Vec<_> = readers.iter().map(tokio::task::JoinHandle::abort_handle).collect();
        let all = futures_util::future::join_all(readers);
        if tokio::time::timeout(DRAIN_TIME, all).await.is_err() {
            for abort in aborts {
                abort.abort();
            }
        }
    }

    /// Kills the command and waits for it.
    pub(crate) async fn kill(&mut self) {
        let _ = self.child.kill().await;
    }

    /// Sends SIGTERM.
    pub(crate) fn terminate(&self) {
        if let Some(raw) = self.child.id().and_then(|id| i32::try_from(id).ok())
            && let Some(pid) = rustix::process::Pid::from_raw(raw)
        {
            let _ = rustix::process::kill_process(pid, rustix::process::Signal::TERM);
        }
    }
}

/// How a command ended, for an error message: its exit status or its signal.
pub(crate) fn exit_description(status: ExitStatus) -> String {
    use std::os::unix::process::ExitStatusExt as _;
    match (status.code(), status.signal()) {
        (Some(code), _) => format!("exit status {code}"),
        (None, Some(signal)) => format!("signal {signal}"),
        (None, None) => "an unknown exit".to_owned(),
    }
}
