//! Registering an app's SQLite databases as continuous backups when it
//! starts: at deploy, and in each long-running worker.
//!
//! ```no_run
//! # async fn example() -> litebackup::Result<()> {
//! use litebackup::startup::{DatabaseBackup, register_backups};
//!
//! let client = litebackup::Client::builder().connect().await?;
//! register_backups(&client, &[DatabaseBackup::new("/var/lib/app/main.sqlite")]).await?;
//! client.close();
//! # Ok(())
//! # }
//! ```

use std::path::{Path, PathBuf};

use crate::client::Client;
use crate::error::{Error, ErrorCode, Result};
use crate::groups::BackupOptions;
use crate::models::BackupRule;

/// One database to back up, and its backup's options.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct DatabaseBackup {
    /// The database file.
    pub path: PathBuf,
    /// The options [`Backups::start`](crate::Backups::start) gets.
    pub options: BackupOptions,
}

impl DatabaseBackup {
    /// The backup of the file at `path`, with the default options.
    pub fn new(path: impl AsRef<Path>) -> Self {
        Self { path: path.as_ref().to_path_buf(), options: BackupOptions::default() }
    }

    /// The backup with these options.
    #[must_use]
    pub fn options(mut self, options: BackupOptions) -> Self {
        self.options = options;
        self
    }
}

/// Whether a path names an SQLite memory database, which has no file to back up.
fn memory_database(path: &Path) -> bool {
    let text = path.to_string_lossy();
    text == ":memory:" || text.starts_with("file::memory:") || text.contains("mode=memory")
}

/// Starts the backup of each database, in order, and returns the saved
/// rules. Starting a path again with the same settings returns its saved
/// rule, so call this on every start, at deploy and in each worker. A
/// database file that does not exist yet is created empty, the empty
/// database SQLite would create on first connect, when its directory
/// exists, and never over a file another process created first. It never
/// shuts the daemon down.
///
/// # Errors
///
/// `rule_exists` for a path saved with other settings, with the saved rule in
/// `details()["rule"]`, and `invalid_argument` for a memory database or a
/// missing directory; each names the database.
pub async fn register_backups(client: &Client, backups: &[DatabaseBackup]) -> Result<Vec<BackupRule>> {
    let mut rules = Vec::with_capacity(backups.len());
    for backup in backups {
        if memory_database(&backup.path) {
            return Err(Error::invalid_argument(format!(
                "{} is a memory database, which has no file to back up",
                backup.path.display()
            )));
        }
        create_empty_database(&backup.path)?;
        let rule = client.backups().start(&backup.path, backup.options.clone()).await.map_err(|error| {
            let message = format!("registering the backup of {} failed: {}", backup.path.display(), error.message());
            Error::new(error.code().to_owned(), message)
                .with_status(error.status())
                .with_details(error.details().clone())
                .with_source(error)
        })?;
        rules.push(rule);
    }
    Ok(rules)
}

/// Creates an empty database file when there is none, only in a directory
/// that exists, and never over a file another process created first.
fn create_empty_database(path: &Path) -> Result<()> {
    let absolute = PathBuf::from(crate::wire::absolute_path(path)?);
    if let Some(directory) = absolute.parent()
        && !directory.is_dir()
    {
        return Err(Error::new(
            ErrorCode::INVALID_ARGUMENT,
            format!("the directory of {} does not exist", absolute.display()),
        ));
    }
    match std::fs::OpenOptions::new().write(true).create_new(true).open(&absolute) {
        Ok(_) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(()),
        Err(error) => Err(Error::invalid_argument(format!("creating {} failed: {error}", absolute.display()))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn memory_databases_have_no_file() {
        assert!(memory_database(Path::new(":memory:")));
        assert!(memory_database(Path::new("file::memory:?cache=shared")));
        assert!(memory_database(Path::new("file:app?mode=memory")));
        assert!(!memory_database(Path::new("/var/lib/app/main.sqlite")));
    }
}
