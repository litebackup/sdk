// Code generated from client-openapi.yaml. DO NOT EDIT.

//! The socket API spec's error codes; `ErrorCode` in `error.rs` takes them.
#![allow(dead_code, reason = "ErrorCode names the codes callers match; the daemon's other codes arrive as strings")]

pub(crate) const BUSY: &str = "busy";
pub(crate) const CANCELLED: &str = "cancelled";
pub(crate) const CHANGED: &str = "changed";
pub(crate) const CLIENT_UNSUPPORTED: &str = "client_unsupported";
pub(crate) const CONFLICT: &str = "conflict";
pub(crate) const CURSOR_EXPIRED: &str = "cursor_expired";
pub(crate) const DRAIN_REQUIRED: &str = "drain_required";
pub(crate) const FILE_EXISTS: &str = "file_exists";
pub(crate) const FORBIDDEN: &str = "forbidden";
pub(crate) const INTEGRITY: &str = "integrity";
pub(crate) const INTERNAL: &str = "internal";
pub(crate) const INVALID_ARGUMENT: &str = "invalid_argument";
pub(crate) const LAUNCH_FAILED: &str = "launch_failed";
pub(crate) const LIMIT_EXCEEDED: &str = "limit_exceeded";
pub(crate) const LOCK_LOST: &str = "lock_lost";
pub(crate) const LOCK_UNAVAILABLE: &str = "lock_unavailable";
pub(crate) const METHOD_NOT_ALLOWED: &str = "method_not_allowed";
pub(crate) const NOT_CONFIGURED: &str = "not_configured";
pub(crate) const NOT_DURABLE: &str = "not_durable";
pub(crate) const NOT_FOUND: &str = "not_found";
pub(crate) const PLAN_LIMIT: &str = "plan_limit";
pub(crate) const RATE_LIMITED: &str = "rate_limited";
pub(crate) const RULE_DATABASE: &str = "rule_database";
pub(crate) const RULE_EXISTS: &str = "rule_exists";
pub(crate) const SDK_UNSUPPORTED: &str = "sdk_unsupported";
pub(crate) const STILL_OPEN: &str = "still_open";
pub(crate) const TIMEOUT: &str = "timeout";
pub(crate) const UNAUTHENTICATED: &str = "unauthenticated";
pub(crate) const UNAVAILABLE: &str = "unavailable";
pub(crate) const WRONG_SERVER: &str = "wrong_server";
