"""The one error type the SDK raises, and its codes."""

from __future__ import annotations

from typing import Any, Final, Literal

from ._generated import error_codes as _spec_codes


class ErrorCode:
    """Codes the SDK raises itself, with status 0, and the daemon codes worth matching.

    Codes the daemon answers with that are not listed here are kept verbatim.
    """

    INVALID_ARGUMENT = _spec_codes.INVALID_ARGUMENT
    """An argument failed local validation; nothing was sent."""
    BINARY_NOT_FOUND = "binary_not_found"
    """No litebackup binary was found to start the daemon or a foreground backup."""
    LAUNCH_FAILED = _spec_codes.LAUNCH_FAILED
    """``litebackup launch`` ran and failed."""
    DRAIN_REQUIRED = _spec_codes.DRAIN_REQUIRED
    """``litebackup launch`` refused api_key, a credential other than the one the session's client took its write locks with.

    ``session.shutdown()`` with the current credential releases them first.
    """
    CONNECTION_FAILED = "connection_failed"
    """The socket refused the connection or closed it mid-call."""
    DAEMON_UNSUPPORTED = "daemon_unsupported"
    """The daemon is older than MINIMUM_DAEMON_VERSION."""
    CANCELLED = _spec_codes.CANCELLED
    """The wait was cancelled; the daemon may still finish the operation.

    Async calls let asyncio.CancelledError propagate unchanged instead.
    """
    CLOSED = "closed"
    """The client was closed."""
    INVALID_RESPONSE = "invalid_response"
    """The daemon answered 200 with a body the SDK cannot decode."""
    LEASE_CLEANUP_FAILED = "lease_cleanup_failed"
    """A pool could not close or release a leased database; the error is a LeaseCleanupError."""
    FILE_EXISTS = _spec_codes.FILE_EXISTS
    """The file a restore or an acquire would write, or a journal file beside it, already exists."""
    RULE_DATABASE = _spec_codes.RULE_DATABASE
    """A backup rule on this machine keeps the database and its write lock, so it cannot be released; stop its backup instead."""
    RULE_EXISTS = _spec_codes.RULE_EXISTS
    """The path already has a backup rule with other settings; ``details["rule"]`` is the saved rule."""
    LOCK_UNAVAILABLE = _spec_codes.LOCK_UNAVAILABLE
    """Another machine writes the database."""
    LOCK_LOST = _spec_codes.LOCK_LOST
    """This machine lost the database's write lock, as a pool's lease does when another machine broke it."""
    NOT_FOUND = _spec_codes.NOT_FOUND
    STILL_OPEN = _spec_codes.STILL_OPEN
    """A process still has the database open, so it cannot be released."""
    TIMEOUT = _spec_codes.TIMEOUT
    """The operation's timeout ran out."""
    SDK_UNSUPPORTED = _spec_codes.SDK_UNSUPPORTED
    """This SDK is older than the daemon accepts."""
    BUSY = _spec_codes.BUSY
    """Another job or a continuous backup holds a foreground backup's writer lock."""
    CURSOR_EXPIRED = _spec_codes.CURSOR_EXPIRED
    """An events cursor is older than the oldest event the daemon keeps; a watch ends with it, and the caller picks a new cursor."""


class LitebackupError(Exception):
    """Every failure the SDK raises: the daemon's answer, or an error the SDK raised itself with status 0.

    ``details`` holds the answer's fields besides code and message under their wire
    names, such as ``rule`` for ``rule_exists`` from ``backups.start()``. For
    ``lease_cleanup_failed`` it holds step, name, path, and acquired or durability.
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


LeaseCleanupStep = Literal["close", "release"]
LEASE_CLEANUP_CLOSE: Final[LeaseCleanupStep] = "close"
"""Closing the handle failed, so the database was not released and stays acquired by this machine."""
LEASE_CLEANUP_RELEASE: Final[LeaseCleanupStep] = "release"
"""The release itself failed; the file is intact."""


class LeaseCleanupError(LitebackupError):
    """A failed release of a leased database, with code ``lease_cleanup_failed``; ``__cause__`` is why.

    ``step`` is ``"close"`` when closing the handle failed: the database was not
    released and stays acquired by this machine with its file as it is; close every
    connection, then call ``pool.release(name)`` again. It is ``"release"`` when the
    release itself failed, as with still_open or an unreachable server: the file is
    intact; call ``pool.release(name)`` again before another machine needs it.
    """

    step: LeaseCleanupStep
    name: str

    def __init__(self, step: LeaseCleanupStep, name: str, message: str, details: dict[str, Any]) -> None:
        super().__init__(ErrorCode.LEASE_CLEANUP_FAILED, message, details=details)
        self.step = step
        self.name = name


def invalid_argument(message: str) -> LitebackupError:
    """The error for a failed local check."""
    return LitebackupError(ErrorCode.INVALID_ARGUMENT, message)
