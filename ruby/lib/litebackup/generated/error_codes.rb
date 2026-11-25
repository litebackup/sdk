# frozen_string_literal: true

# Code generated from client-openapi.yaml. DO NOT EDIT.

module Litebackup
  # The socket API spec's error codes; errors.rb's ErrorCode takes them.
  module SpecErrorCodes
    BUSY = "busy"
    CANCELLED = "cancelled"
    CHANGED = "changed"
    CLIENT_UNSUPPORTED = "client_unsupported"
    CONFLICT = "conflict"
    CURSOR_EXPIRED = "cursor_expired"
    DRAIN_REQUIRED = "drain_required"
    FILE_EXISTS = "file_exists"
    FORBIDDEN = "forbidden"
    INTEGRITY = "integrity"
    INTERNAL = "internal"
    INVALID_ARGUMENT = "invalid_argument"
    LAUNCH_FAILED = "launch_failed"
    LIMIT_EXCEEDED = "limit_exceeded"
    LOCK_LOST = "lock_lost"
    LOCK_UNAVAILABLE = "lock_unavailable"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    NOT_CONFIGURED = "not_configured"
    NOT_DURABLE = "not_durable"
    NOT_FOUND = "not_found"
    PLAN_LIMIT = "plan_limit"
    RULE_DATABASE = "rule_database"
    RULE_EXISTS = "rule_exists"
    SDK_UNSUPPORTED = "sdk_unsupported"
    STILL_OPEN = "still_open"
    TIMEOUT = "timeout"
    UNAUTHENTICATED = "unauthenticated"
    UNAVAILABLE = "unavailable"
    WRONG_SERVER = "wrong_server"
  end
end
