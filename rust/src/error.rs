//! The one error type every call returns, and the codes worth matching.

use std::borrow::Cow;
use std::fmt;

use serde_json::{Map, Value};

use crate::generated::error_codes as codes;

/// A boxed error from outside this crate, such as an adapter's or an I/O
/// error, kept as an [`Error`]'s source.
pub type BoxError = Box<dyn std::error::Error + Send + Sync + 'static>;

/// What every call of this crate returns.
pub type Result<T, E = Error> = std::result::Result<T, E>;

/// Codes this SDK raises itself, with status 0, and the daemon's codes worth
/// matching against [`Error::code`]. A code the daemon answers with that is
/// not listed here is kept verbatim.
///
/// ```
/// # fn check(error: &litebackup::Error) {
/// if error.code() == litebackup::ErrorCode::LOCK_UNAVAILABLE {
///     // Another machine writes the database; try again later.
/// }
/// # }
/// ```
#[derive(Debug)]
pub enum ErrorCode {}

impl ErrorCode {
    /// An argument failed local validation; nothing was sent.
    pub const INVALID_ARGUMENT: &'static str = codes::INVALID_ARGUMENT;
    /// No litebackup binary was found to start the daemon or a foreground
    /// backup, or the explicit binary path names no file.
    pub const BINARY_NOT_FOUND: &'static str = "binary_not_found";
    /// `litebackup launch` ran and failed.
    pub const LAUNCH_FAILED: &'static str = codes::LAUNCH_FAILED;
    /// `litebackup launch` refused the client's credential, which differs from
    /// the one the session took its write locks with; a shutdown with the
    /// current credential releases them first.
    pub const DRAIN_REQUIRED: &'static str = codes::DRAIN_REQUIRED;
    /// The socket refused the connection or closed it mid-call.
    pub const CONNECTION_FAILED: &'static str = "connection_failed";
    /// The daemon is older than [`MINIMUM_DAEMON_VERSION`](crate::MINIMUM_DAEMON_VERSION).
    pub const DAEMON_UNSUPPORTED: &'static str = "daemon_unsupported";
    /// A lease's cancellation token was cancelled; the daemon may still
    /// finish the operation.
    pub const CANCELLED: &'static str = codes::CANCELLED;
    /// The client, or the pool, was closed.
    pub const CLOSED: &'static str = "closed";
    /// The daemon answered 200 with a body this SDK cannot decode.
    pub const INVALID_RESPONSE: &'static str = "invalid_response";
    /// A pool could not close a leased database or release it; see
    /// [`Error::lease_cleanup`].
    pub const LEASE_CLEANUP_FAILED: &'static str = "lease_cleanup_failed";
    /// The file a restore or an acquire would write, or a journal file
    /// beside it, already exists.
    pub const FILE_EXISTS: &'static str = codes::FILE_EXISTS;
    /// A backup rule on this machine owns the database and its write lock,
    /// so it cannot be released; stop its backup instead.
    pub const RULE_DATABASE: &'static str = codes::RULE_DATABASE;
    /// The path already has a backup rule with different settings;
    /// `details()["rule"]` is the saved rule.
    pub const RULE_EXISTS: &'static str = codes::RULE_EXISTS;
    /// Another machine writes the database.
    pub const LOCK_UNAVAILABLE: &'static str = codes::LOCK_UNAVAILABLE;
    /// This machine lost the database's write lock, as a pool's lease finds
    /// when another machine broke it.
    pub const LOCK_LOST: &'static str = codes::LOCK_LOST;
    /// Neither the server nor this machine has the database.
    pub const NOT_FOUND: &'static str = codes::NOT_FOUND;
    /// A process still has the database open, so it cannot be released.
    pub const STILL_OPEN: &'static str = codes::STILL_OPEN;
    /// The operation's timeout ran out, on the daemon or, with status 0, in
    /// this SDK; the daemon may still finish it.
    pub const TIMEOUT: &'static str = codes::TIMEOUT;
    /// This SDK is older than the daemon accepts.
    pub const SDK_UNSUPPORTED: &'static str = codes::SDK_UNSUPPORTED;
    /// The server needs a newer daemon than this machine runs.
    pub const CLIENT_UNSUPPORTED: &'static str = codes::CLIENT_UNSUPPORTED;
    /// Another job or a continuous backup has a foreground backup's writer lock.
    pub const BUSY: &'static str = codes::BUSY;
    /// An events cursor is older than the oldest event the daemon keeps; a
    /// watch ends with it, and the caller picks a new cursor.
    pub const CURSOR_EXPIRED: &'static str = codes::CURSOR_EXPIRED;
    /// The local file holds writes the server lacks, and was left untouched.
    pub const CONFLICT: &'static str = codes::CONFLICT;
}

/// Every failure of this crate: a code, a message, the HTTP status the
/// daemon answered with (0 for errors this SDK raised itself), and the other
/// fields of the daemon's answer.
///
/// A code the daemon sends is kept verbatim, including codes this SDK has
/// never heard of; [`ErrorCode`] names those worth matching.
pub struct Error {
    inner: Box<ErrorFields>,
}

struct ErrorFields {
    code: Cow<'static, str>,
    message: String,
    status: u16,
    details: Map<String, Value>,
    source: Option<BoxError>,
    lease_cleanup: Option<LeaseCleanupError>,
}

impl Error {
    /// An error with a code and a message, status 0, and no details.
    pub(crate) fn new(code: impl Into<Cow<'static, str>>, message: impl Into<String>) -> Self {
        Self {
            inner: Box::new(ErrorFields {
                code: code.into(),
                message: message.into(),
                status: 0,
                details: Map::new(),
                source: None,
                lease_cleanup: None,
            }),
        }
    }

    /// `invalid_argument`: an argument failed local validation.
    pub(crate) fn invalid_argument(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::INVALID_ARGUMENT, message)
    }

    /// `closed`: the client was closed.
    pub(crate) fn closed() -> Self {
        Self::new(ErrorCode::CLOSED, "the client is closed")
    }

    pub(crate) fn with_status(mut self, status: u16) -> Self {
        self.inner.status = status;
        self
    }

    pub(crate) fn with_details(mut self, details: Map<String, Value>) -> Self {
        self.inner.details = details;
        self
    }

    pub(crate) fn with_source(mut self, source: impl Into<BoxError>) -> Self {
        self.inner.source = Some(source.into());
        self
    }

    pub(crate) fn with_lease_cleanup(mut self, cleanup: LeaseCleanupError) -> Self {
        self.inner.lease_cleanup = Some(cleanup);
        self
    }

    /// The error's code, such as `lock_unavailable`; compare it with the
    /// [`ErrorCode`] constants.
    #[must_use]
    pub fn code(&self) -> &str {
        &self.inner.code
    }

    /// What went wrong, in words meant for people.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.inner.message
    }

    /// The HTTP status the daemon answered with, or 0 for an error this SDK
    /// raised itself, as when the daemon did not answer.
    #[must_use]
    pub fn status(&self) -> u16 {
        self.inner.status
    }

    /// The other fields of the daemon's error answer under their wire names,
    /// such as `rule` with `rule_exists`, or the step of a pool's cleanup.
    #[must_use]
    pub fn details(&self) -> &Map<String, Value> {
        &self.inner.details
    }

    /// For `lease_cleanup_failed`, which step of a pool's cleanup failed and
    /// what it left as it was.
    #[must_use]
    pub fn lease_cleanup(&self) -> Option<&LeaseCleanupError> {
        self.inner.lease_cleanup.as_ref()
    }

    /// Whether the error's code is `code`.
    #[must_use]
    pub fn has_code(&self, code: &str) -> bool {
        self.code() == code
    }
}

impl fmt::Debug for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut debug = formatter.debug_struct("Error");
        debug.field("code", &self.code()).field("message", &self.message()).field("status", &self.status());
        if !self.details().is_empty() {
            debug.field("details", self.details());
        }
        if let Some(source) = &self.inner.source {
            debug.field("source", source);
        }
        debug.finish()
    }
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code(), self.message())
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.inner.source.as_ref().map(|source| &**source as &(dyn std::error::Error + 'static))
    }
}

/// The step of a pool's cleanup that failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum LeaseCleanupStep {
    /// Closing the database's connection failed, so it was not released: it
    /// stays acquired by this machine with its file as it is. Close every
    /// connection, then call the pool's `release` again.
    Close,
    /// The release itself failed, as with `still_open` or an unreachable
    /// server. The file is intact; call the pool's `release` again before
    /// another machine needs the database.
    Release,
}

impl LeaseCleanupStep {
    /// The step as the error's details name it: `close` or `release`.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Close => "close",
            Self::Release => "release",
        }
    }
}

impl fmt::Display for LeaseCleanupStep {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// What a `lease_cleanup_failed` [`Error`] left as it was: the step that
/// failed, and the database and file it was for. The [`Error`]'s source is
/// the failure itself.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct LeaseCleanupError {
    /// The step that failed.
    pub step: LeaseCleanupStep,
    /// The database's name.
    pub name: String,
    /// The database's local file.
    pub path: std::path::PathBuf,
}

impl fmt::Display for LeaseCleanupError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "the {} step of {}'s cleanup failed", self.step, self.name)
    }
}

impl std::error::Error for LeaseCleanupError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_error_stays_one_pointer_wide() {
        assert_eq!(size_of::<Error>(), size_of::<usize>());
    }

    #[test]
    fn display_shows_the_code_and_the_message() {
        let error = Error::new("lock_lost", "this machine lost the write lock");
        assert_eq!(error.to_string(), "lock_lost: this machine lost the write lock");
        assert_eq!(error.status(), 0);
        assert!(error.has_code(ErrorCode::LOCK_LOST));
    }
}
