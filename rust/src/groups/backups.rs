//! `client.backups()` and `client.rules()`.

use std::path::Path;
use std::time::Duration;

use serde_json::Value;

use crate::binary;
use crate::child::{ChildCommand, exit_description};
use crate::client::Client;
use crate::credential::API_KEY_ENVIRONMENT;
use crate::error::{Error, Result};
use crate::foreground::{self, BackupJob, STOP_GRACE};
use crate::models::{BackupList, BackupRule, Durability, StopBackupResult};
use crate::options::option_setters;
use crate::wire::{self, Body, Call, DEADLINE_GRACE};

/// Options of [`Backups::start`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct BackupOptions {
    /// The remote name of a single file; its file name without it.
    pub name: Option<String>,
    /// A namespace for every database a directory or glob finds; choose a
    /// name or a prefix, not both.
    pub prefix: Option<String>,
    /// Keep at most this many of the rule's databases on this machine,
    /// offloading the idle rest; 0 or none keeps them all.
    pub maximum_local_databases: Option<u32>,
    /// What an offload of the rule's databases waits for: the server (the
    /// default) or the bucket too ([`Durability::CLOUD`]).
    pub offload_durability: Option<Durability>,
    /// Lets the rule offload a rollback-journal database too, on the word
    /// that nothing keeps it open while idle.
    pub offload_rollback_journal: bool,
}

option_setters!(BackupOptions {
    name: impl Into<String> => Some(name.into()),
    prefix: impl Into<String> => Some(prefix.into()),
    maximum_local_databases: u32 => Some(maximum_local_databases),
    offload_durability: Durability => Some(offload_durability),
    offload_rollback_journal: bool => offload_rollback_journal,
});

/// Options of [`Backups::once`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct OnceOptions {
    /// The remote name of a single file; its file name without it.
    pub name: Option<String>,
    /// A namespace for every database a directory or glob finds.
    pub prefix: Option<String>,
    /// What the job waits for before it exits: the server (the default) or
    /// the bucket too.
    pub durability: Option<Durability>,
    /// How long the job may take; one still running 30 seconds after it ran
    /// out is stopped with SIGTERM, then SIGKILL 15 seconds later, and the call
    /// fails with `timeout`.
    pub timeout: Option<Duration>,
}

option_setters!(OnceOptions {
    name: impl Into<String> => Some(name.into()),
    prefix: impl Into<String> => Some(prefix.into()),
    durability: Durability => Some(durability),
    timeout: Duration => Some(timeout),
});

/// Continuous backups and one-shot backup jobs; [`Client::backups`].
#[derive(Debug, Clone, Copy)]
pub struct Backups<'c> {
    client: &'c Client,
}

impl<'c> Backups<'c> {
    pub(crate) fn new(client: &'c Client) -> Self {
        Self { client }
    }

    /// Saves a continuous backup rule for a file, directory, or glob, and
    /// returns once the daemon saved it. Starting the same path again with the
    /// same settings returns the saved rule, so call it on every start.
    ///
    /// # Errors
    ///
    /// `rule_exists`, with the saved rule in `details()["rule"]`, for a path
    /// saved with different settings, and `invalid_argument` for an empty
    /// source or a name and a prefix together.
    pub async fn start(&self, source: impl AsRef<Path>, options: BackupOptions) -> Result<BackupRule> {
        let source = source.as_ref();
        if source.as_os_str().is_empty() {
            return Err(Error::invalid_argument("backup needs a path"));
        }
        let name = options.name.as_deref().filter(|name| !name.is_empty());
        let prefix = options.prefix.as_deref().filter(|prefix| !prefix.is_empty());
        if name.is_some() && prefix.is_some() {
            return Err(Error::invalid_argument("choose a name or a prefix"));
        }
        let offload_durability = foreground::checked_durability(options.offload_durability.as_ref())?
            .filter(|durability| *durability != Durability::SERVER);
        let body = Body::new()
            .text("path", &wire::absolute_path(source)?)
            .optional_text("name", name)
            .optional_text("prefix", prefix)
            .integer(
                "maximum_local_databases",
                options.maximum_local_databases.filter(|maximum| *maximum > 0).map(i64::from),
            )
            .optional_text("offload_durability", offload_durability.as_ref().map(Durability::as_str))
            .flag("offload_rollback_journal", options.offload_rollback_journal);
        self.client.call(Call::post("/v1/backup-rules/start", body)).await
    }

    /// Runs one foreground backup job and returns once it exits, after the
    /// chosen destination acknowledged it. Dropping the returned future
    /// kills the job; closing the client stops it with SIGTERM, then
    /// SIGKILL 15 seconds later, and the call fails with `closed`.
    ///
    /// # Errors
    ///
    /// The job's own code, such as `busy`, with its file diagnostics;
    /// `timeout` past the timeout; `binary_not_found` or `launch_failed` when
    /// the binary cannot start; and `invalid_argument` for a client of an
    /// explicit socket path, which has no named session.
    pub async fn once(&self, source: impl AsRef<Path>, options: OnceOptions) -> Result<()> {
        self.client.check_open()?;
        let settings = self.client.settings();
        if settings.explicit_socket {
            return Err(Error::invalid_argument(
                "a foreground backup needs a named session and cannot target a socket path",
            ));
        }
        let job = BackupJob {
            source: source.as_ref(),
            session: settings.session.as_deref(),
            name: options.name.as_deref(),
            prefix: options.prefix.as_deref(),
            durability: options.durability.as_ref(),
            timeout: options.timeout,
        };
        let binary = binary::litebackup_binary(&settings.environment, settings.binary_path.as_deref())?;
        let command = foreground::backup_command(&binary, &job)?;
        if binary::missing_path(&binary) {
            return Err(binary::start_failure(&binary, "no such file"));
        }
        let mut environment = settings.environment.clone();
        if let Some(api_key) = &settings.api_key_variable {
            environment.set_variable(API_KEY_ENVIRONMENT, api_key);
        }
        let mut backup = ChildCommand::start(&command, &environment, false)?;
        let deadline = options.timeout.filter(|timeout| !timeout.is_zero()).map(|timeout| timeout + DEADLINE_GRACE);
        let waited = async {
            match deadline {
                Some(deadline) => tokio::time::timeout(deadline, backup.child.wait()).await.ok(),
                None => Some(backup.child.wait().await),
            }
        };
        // None when the client closed first; then Some(None) past the deadline.
        let finished = tokio::select! {
            finished = waited => Some(finished),
            () = self.client.lifetime().cancelled() => None,
        };
        let Some(finished) = finished else {
            stop_gracefully(&mut backup).await;
            return Err(Error::closed());
        };
        let Some(status) = finished else {
            stop_gracefully(&mut backup).await;
            return Err(foreground::timed_out_backup());
        };
        let status = status.map_err(|error| {
            Error::new("backup_failed", format!("waiting for the foreground backup failed: {error}"))
        })?;
        backup.drain().await;
        if status.success() {
            return Ok(());
        }
        Err(foreground::backup_failure(&backup.error_output.text(), &exit_description(status)))
    }

    /// Stops the single-file backup with this exact remote name on this machine.
    ///
    /// # Errors
    ///
    /// `invalid_argument` for an empty name.
    pub async fn stop(&self, name: &str) -> Result<StopBackupResult> {
        if name.is_empty() {
            return Err(Error::invalid_argument("stopping a backup needs a name"));
        }
        self.client.call(Call::post("/v1/backup-rules/stop", Body::new().text("name", name))).await
    }
}

/// Stops a running job: SIGTERM, then SIGKILL when it has not exited within
/// [`STOP_GRACE`].
async fn stop_gracefully(backup: &mut ChildCommand) {
    backup.terminate();
    if tokio::time::timeout(STOP_GRACE, backup.child.wait()).await.is_err() {
        backup.kill().await;
    }
}

/// The backup and failover rules on this machine; [`Client::rules`].
#[derive(Debug, Clone, Copy)]
pub struct Rules<'c> {
    client: &'c Client,
}

impl<'c> Rules<'c> {
    pub(crate) fn new(client: &'c Client) -> Self {
        Self { client }
    }

    /// Every backup and failover rule on this machine.
    ///
    /// # Errors
    ///
    /// The daemon's error, or `connection_failed` when it does not answer.
    pub async fn list(&self) -> Result<Vec<BackupRule>> {
        let list: BackupList = self.client.call(Call::get("/v1/backup-rules/list", Vec::new())).await?;
        Ok(list.rules)
    }

    /// Stops a whole rule and its future listings by its saved ID.
    ///
    /// # Errors
    ///
    /// `invalid_argument` for an empty ID.
    pub async fn stop(&self, id: &str) -> Result<StopBackupResult> {
        if id.is_empty() {
            return Err(Error::invalid_argument("stopping a rule needs its ID"));
        }
        let body = Body::new().value("rule_id", Value::from(id));
        self.client.call(Call::post("/v1/backup-rules/stop", body)).await
    }
}
