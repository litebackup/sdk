"""The one error type the SDK raises, and its codes."""

from __future__ import annotations

from typing import Any


class ErrorCode:
    """Codes the SDK raises itself, with status 0, and the daemon codes worth matching.

    Codes the daemon answers with that are not listed here are kept verbatim.
    """

    INVALID_ARGUMENT = "invalid_argument"
    """An argument failed local validation; nothing was sent."""
    BINARY_NOT_FOUND = "binary_not_found"
    """No litebackup binary was found to start the daemon."""
    LAUNCH_FAILED = "launch_failed"
    """``litebackup launch`` ran and failed."""
    CONNECTION_FAILED = "connection_failed"
    """The socket refused the connection or closed it mid-call."""
    INCOMPATIBLE_VERSION = "incompatible_version"
    """The daemon is older than MINIMUM_DAEMON_VERSION."""
    CANCELLED = "cancelled"
    """The wait was cancelled; the daemon may still finish the operation.

    Async calls let asyncio.CancelledError propagate unchanged instead.
    """
    CLOSED = "closed"
    """The client was closed."""
    INVALID_RESPONSE = "invalid_response"
    """The daemon answered 200 with a body the SDK cannot decode."""
    SCOPE_CLEANUP_FAILED = "scope_cleanup_failed"
    """A managed scope could not close or release its database."""
    EXISTS = "exists"
    """A restore target exists, or the path already has a backup rule with other settings."""
    IN_USE = "in_use"
    """Another machine writes the database."""
    NOT_FOUND = "not_found"
    STILL_OPEN = "still_open"
    """A process still has the database open, so it cannot be released."""
    TIMEOUT = "timeout"
    """The operation's timeout ran out."""
    SDK_UNSUPPORTED = "sdk_unsupported"
    """This SDK is older than the daemon accepts."""
    BUSY = "busy"
    """Another job or a continuous backup holds a foreground backup's writer lock."""


class LitebackupError(Exception):
    """Every failure the SDK raises: the daemon's answer, or an error the SDK raised itself with status 0.

    ``details`` holds the answer's fields besides code and message under their wire
    names, such as ``rule`` for ``exists`` from ``backups.start()``. For
    ``scope_cleanup_failed`` it holds step, name, path, and acquired or durability.
    The underlying error, when there is one, is ``__cause__``.
    """

    code: str
    message: str
    status: int
    details: dict[str, Any]

    def __init__(self, code: str, message: str, status: int = 0, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.details = details if details is not None else {}

    def __repr__(self) -> str:
        return f"LitebackupError(code={self.code!r}, message={self.message!r}, status={self.status!r}, details={self.details!r})"


def invalid_argument(message: str) -> LitebackupError:
    """The error for a failed local check."""
    return LitebackupError(ErrorCode.INVALID_ARGUMENT, message)
