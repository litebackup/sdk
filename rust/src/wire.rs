//! Request building and answer decoding, apart from the transport: every
//! operation builds a [`Call`], the request to send and how long the daemon
//! may take, and its answer decodes into a generated model.

use std::fmt::Write as _;
use std::path::{Component, Path};
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde_json::{Map, Value};

use crate::error::{Error, ErrorCode, Result};
use crate::{MINIMUM_DAEMON_VERSION, VERSION};

/// Sent with every call: `rust/<version>`.
pub(crate) const SDK_VERSION_HEADER: &str = "litebackup-sdk-version";
/// The daemon's version, on every answer.
pub(crate) const CLIENT_VERSION_HEADER: &str = "litebackup-client-version";
/// Added to a call's server-side timeout for its client-side deadline, so the
/// daemon's own `timeout` answer arrives first.
pub(crate) const DEADLINE_GRACE: Duration = Duration::from_secs(30);
/// How long an event long-poll waits unless the caller sets its wait.
pub(crate) const EVENT_WAIT: Duration = Duration::from_secs(30);
/// The first backoff of a watcher's failed long-poll, doubling up to
/// [`MAXIMUM_RETRY_DELAY`].
pub(crate) const FIRST_RETRY_DELAY: Duration = Duration::from_millis(250);
/// The longest backoff of a watcher's failed long-poll.
pub(crate) const MAXIMUM_RETRY_DELAY: Duration = Duration::from_secs(10);
/// Limits one answer's body.
pub(crate) const RESPONSE_LIMIT: usize = 64 << 20;
/// The page size listings read when the caller sets none, and the largest.
pub(crate) const DEFAULT_PAGE_LIMIT: u32 = 100;
pub(crate) const MAXIMUM_PAGE_LIMIT: u32 = 1000;

/// The value of [`SDK_VERSION_HEADER`].
pub(crate) fn sdk_version_value() -> String {
    format!("rust/{VERSION}")
}

/// How a call is sent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Method {
    Get,
    Post,
}

impl Method {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Get => "GET",
            Self::Post => "POST",
        }
    }
}

/// One request: its method, path, query, JSON body, and the server-side
/// timeout that gives it a client-side deadline.
#[derive(Debug, Clone)]
pub(crate) struct Call {
    pub(crate) method: Method,
    pub(crate) path: &'static str,
    pub(crate) query: Vec<(&'static str, String)>,
    pub(crate) body: Option<Map<String, Value>>,
    pub(crate) server_timeout: Option<Duration>,
}

impl Call {
    pub(crate) fn get(path: &'static str, query: Vec<(&'static str, String)>) -> Self {
        Self { method: Method::Get, path, query, body: None, server_timeout: None }
    }

    pub(crate) fn post(path: &'static str, body: Body) -> Self {
        Self { method: Method::Post, path, query: Vec::new(), body: Some(body.0), server_timeout: None }
    }

    /// Gives the call a client-side deadline of the server-side timeout plus
    /// [`DEADLINE_GRACE`]; none leaves it without a deadline.
    pub(crate) fn with_timeout(mut self, server_timeout: Option<Duration>) -> Self {
        self.server_timeout = server_timeout.filter(|timeout| !timeout.is_zero());
        self
    }

    /// The client-side deadline, measured from when the call is made.
    pub(crate) fn deadline(&self) -> Option<Duration> {
        self.server_timeout.map(|timeout| timeout.saturating_add(DEADLINE_GRACE))
    }

    /// The path and its query, percent-encoded.
    pub(crate) fn target(&self) -> String {
        let mut target = self.path.to_owned();
        for (index, (key, value)) in self.query.iter().enumerate() {
            target.push(if index == 0 { '?' } else { '&' });
            target.push_str(&percent_encode(key));
            target.push('=');
            target.push_str(&percent_encode(value));
        }
        target
    }
}

/// Percent-encodes a query key or value, keeping only unreserved characters.
fn percent_encode(text: &str) -> String {
    let mut encoded = String::with_capacity(text.len());
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(char::from(byte));
        } else {
            let _ = write!(encoded, "%{byte:02X}");
        }
    }
    encoded
}

/// A request body that leaves the daemon's defaults off the wire: only
/// settings that differ from the default are sent.
#[derive(Debug, Default)]
pub(crate) struct Body(Map<String, Value>);

impl Body {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    /// A text field sent as it is, even empty, as a required name is.
    pub(crate) fn text(mut self, key: &str, value: &str) -> Self {
        self.0.insert(key.to_owned(), Value::from(value));
        self
    }

    /// A text field sent unless it is unset or empty.
    pub(crate) fn optional_text(mut self, key: &str, value: Option<&str>) -> Self {
        if let Some(value) = value.filter(|value| !value.is_empty()) {
            self.0.insert(key.to_owned(), Value::from(value));
        }
        self
    }

    /// A flag sent only when it is true.
    pub(crate) fn flag(mut self, key: &str, value: bool) -> Self {
        if value {
            self.0.insert(key.to_owned(), Value::Bool(true));
        }
        self
    }

    /// A duration sent in whole milliseconds unless it is unset or zero.
    pub(crate) fn millis(mut self, key: &str, value: Option<Duration>) -> Self {
        if let Some(duration) = value.filter(|duration| !duration.is_zero()) {
            self.0.insert(key.to_owned(), Value::from(millis(duration)));
        }
        self
    }

    /// A number sent unless it is unset.
    pub(crate) fn integer(mut self, key: &str, value: Option<i64>) -> Self {
        if let Some(value) = value {
            self.0.insert(key.to_owned(), Value::from(value));
        }
        self
    }

    /// Any value, sent as it is.
    pub(crate) fn value(mut self, key: &str, value: Value) -> Self {
        self.0.insert(key.to_owned(), value);
        self
    }
}

/// A duration in whole milliseconds, as the API takes it.
pub(crate) fn millis(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}

/// Resolves a path against the working directory and cleans it lexically,
/// removing `.` and `..` segments; the daemon needs absolute paths. A path
/// that is not UTF-8 cannot go into a request and is `invalid_argument`.
pub(crate) fn absolute_path(path: &Path) -> Result<String> {
    let joined = if path.is_absolute() {
        path.to_path_buf()
    } else {
        let directory = std::env::current_dir().map_err(|error| {
            Error::invalid_argument(format!(
                "the working directory cannot be read, so {} cannot be resolved: {error}",
                path.display()
            ))
        })?;
        directory.join(path)
    };
    let mut segments: Vec<&str> = Vec::new();
    for component in joined.components() {
        match component {
            Component::Normal(segment) => segments.push(
                segment
                    .to_str()
                    .ok_or_else(|| Error::invalid_argument(format!("{} is not a UTF-8 path", path.display())))?,
            ),
            Component::ParentDir => {
                segments.pop();
            }
            Component::RootDir | Component::CurDir | Component::Prefix(_) => {}
        }
    }
    Ok(format!("/{}", segments.join("/")))
}

/// Whether a path argument holds something.
pub(crate) fn supplied(path: Option<&Path>) -> Option<&Path> {
    path.filter(|path| !path.as_os_str().is_empty())
}

/// A time as the API takes it: UTC with `Z`, and fractional seconds only
/// when they are not zero, trailing zeros removed.
pub(crate) fn wire_time(time: jiff::Timestamp) -> String {
    time.to_string()
}

/// Orders dotted numeric versions, ignoring any `-suffix`; parts that are
/// not numbers count as zero.
pub(crate) fn compare_versions(left: &str, right: &str) -> std::cmp::Ordering {
    fn parts(version: &str) -> Vec<u64> {
        let release = version.split('-').next().unwrap_or_default();
        release.split('.').map(|part| part.parse().unwrap_or(0)).collect()
    }
    let (left_parts, right_parts) = (parts(left), parts(right));
    for index in 0..left_parts.len().max(right_parts.len()) {
        let left_part = left_parts.get(index).copied().unwrap_or(0);
        let right_part = right_parts.get(index).copied().unwrap_or(0);
        if left_part != right_part {
            return left_part.cmp(&right_part);
        }
    }
    std::cmp::Ordering::Equal
}

/// Refuses a daemon older than [`MINIMUM_DAEMON_VERSION`], or one that does
/// not report its version, saying how to replace it.
pub(crate) fn check_daemon_version(client_version: &str, session: Option<&str>) -> Result<()> {
    if !client_version.is_empty()
        && compare_versions(client_version, MINIMUM_DAEMON_VERSION) != std::cmp::Ordering::Less
    {
        return Ok(());
    }
    let reported = if client_version.is_empty() {
        "does not report its version".to_owned()
    } else {
        format!("is version {client_version}")
    };
    let drain = session
        .map_or_else(|| "litebackup drain".to_owned(), |session| format!("litebackup drain --session {session}"));
    Err(Error::new(
        ErrorCode::DAEMON_UNSUPPORTED,
        format!(
            "the litebackup daemon {reported}, and this SDK needs {MINIMUM_DAEMON_VERSION} or later. \
             Stop it with `{drain}` so the next call starts the new binary, \
             or restart the service if `litebackup install service` runs it"
        ),
    ))
}

/// The error of a non-200 answer: a JSON object's code verbatim, its message,
/// and its other fields as details; anything else is `http_<status>` with
/// the trimmed text, or a code-less object's message.
pub(crate) fn response_error(status: u16, body: &[u8]) -> Error {
    let text = String::from_utf8_lossy(body);
    let fallback = || Error::new(format!("http_{status}"), text.trim().to_owned()).with_status(status);
    let Ok(Value::Object(mut fields)) = serde_json::from_str::<Value>(&text) else {
        return fallback();
    };
    let code = fields.remove("code");
    let message = match fields.remove("message") {
        Some(Value::String(message)) => Some(message),
        _ => None,
    };
    match code {
        Some(Value::String(code)) if !code.is_empty() => {
            Error::new(code, message.unwrap_or_default()).with_status(status).with_details(fields)
        }
        _ => match message {
            Some(message) => Error::new(format!("http_{status}"), message).with_status(status),
            None => fallback(),
        },
    }
}

/// Decodes a 200 answer, or fails with `invalid_response`.
pub(crate) fn decode_answer<T: DeserializeOwned>(path: &str, body: &[u8]) -> Result<T> {
    serde_json::from_slice(body).map_err(|error| {
        Error::new(ErrorCode::INVALID_RESPONSE, format!("decoding {path}: {error}")).with_status(200).with_source(error)
    })
}

/// A page limit the daemon accepts: from 1 to 1000, or 100 when unset.
pub(crate) fn page_limit(limit: Option<u32>) -> Result<String> {
    match limit {
        None => Ok(DEFAULT_PAGE_LIMIT.to_string()),
        Some(limit) if (1..=MAXIMUM_PAGE_LIMIT).contains(&limit) => Ok(limit.to_string()),
        Some(_) => Err(Error::invalid_argument("the limit must be from 1 to 1000")),
    }
}

/// Errors that end an event watch instead of being retried.
pub(crate) fn ends_watch(error: &Error) -> bool {
    [
        ErrorCode::SDK_UNSUPPORTED,
        ErrorCode::CLOSED,
        ErrorCode::CANCELLED,
        ErrorCode::INVALID_ARGUMENT,
        ErrorCode::CURSOR_EXPIRED,
    ]
    .contains(&error.code())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cmp::Ordering;

    #[test]
    fn versions_compare_by_number_and_ignore_suffixes() {
        assert_eq!(compare_versions("1.10.0", "1.9.9"), Ordering::Greater);
        assert_eq!(compare_versions("1.1.0-rc.1", "1.1.0"), Ordering::Equal);
        assert_eq!(compare_versions("1.1", "1.1.0"), Ordering::Equal);
        assert_eq!(compare_versions("1.x.0", "1.0.0"), Ordering::Equal);
    }

    #[test]
    fn paths_are_absolute_and_cleaned() {
        assert_eq!(absolute_path(Path::new("/a/./b/../c//d")).ok().as_deref(), Some("/a/c/d"));
        assert_eq!(absolute_path(Path::new("/../..")).ok().as_deref(), Some("/"));
    }

    #[test]
    fn queries_are_percent_encoded() {
        let call = Call::get("/v1/databases/status", vec![("name", "agents/42 a.sqlite".to_owned())]);
        assert_eq!(call.target(), "/v1/databases/status?name=agents%2F42%20a.sqlite");
    }

    #[test]
    fn errors_keep_unknown_codes_and_fall_back_to_the_status() {
        let error = response_error(503, br#"{"code":"bucket_unreachable","message":"m","extra":1}"#);
        assert_eq!((error.code(), error.message(), error.status()), ("bucket_unreachable", "m", 503));
        assert_eq!(error.details().get("extra"), Some(&Value::from(1)));
        let error = response_error(502, b"  <html>bad gateway</html>\n");
        assert_eq!((error.code(), error.message()), ("http_502", "<html>bad gateway</html>"));
        let error = response_error(500, br#"{"message":"no code"}"#);
        assert_eq!((error.code(), error.message()), ("http_500", "no code"));
    }
}
