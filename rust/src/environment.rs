//! The environment a client resolves paths and starts the daemon with.

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::path::PathBuf;

/// Environment variables, this process's by default. Values are kept as the
/// operating system gives them, empty ones included, and a child process the
/// client starts gets exactly these.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Environment {
    variables: BTreeMap<OsString, OsString>,
}

impl Environment {
    /// This process's environment.
    pub(crate) fn current() -> Self {
        Self { variables: std::env::vars_os().collect() }
    }

    pub(crate) fn from_variables<K, V>(variables: impl IntoIterator<Item = (K, V)>) -> Self
    where
        K: Into<OsString>,
        V: Into<OsString>,
    {
        Self { variables: variables.into_iter().map(|(key, value)| (key.into(), value.into())).collect() }
    }

    /// A variable's value, even empty.
    pub(crate) fn get(&self, name: &str) -> Option<&OsStr> {
        self.variables.get(OsStr::new(name)).map(OsString::as_os_str)
    }

    /// A variable's value when it is set and not empty.
    pub(crate) fn non_empty(&self, name: &str) -> Option<&OsStr> {
        self.get(name).filter(|value| !value.is_empty())
    }

    /// A variable's value as text when it is set, not empty, and UTF-8.
    pub(crate) fn text(&self, name: &str) -> Option<&str> {
        self.non_empty(name).and_then(OsStr::to_str)
    }

    pub(crate) fn set_variable(&mut self, name: &str, value: impl Into<OsString>) {
        self.variables.insert(OsString::from(name), value.into());
    }

    /// The home directory: `HOME`, or `/` without it.
    pub(crate) fn home(&self) -> PathBuf {
        self.non_empty("HOME").map_or_else(|| PathBuf::from("/"), PathBuf::from)
    }

    pub(crate) fn variables(&self) -> impl Iterator<Item = (&OsString, &OsString)> {
        self.variables.iter()
    }
}
