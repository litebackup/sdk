"""``backups.once()``: one foreground ``litebackup backup`` job, as the CLI runs it."""

from __future__ import annotations

import json

from ._wire import Duration, PathSource, absolute_path, seconds
from .errors import ErrorCode, LitebackupError, invalid_argument


def backup_command(
    binary: str, source: PathSource, *, session: str | None, name: str | None, prefix: str | None, wait: str, timeout: Duration | None
) -> list[str]:
    """The litebackup command line of one foreground job."""
    if not source:
        raise invalid_argument("backup needs a path")
    if name is not None and prefix is not None:
        raise invalid_argument("choose a name or a prefix")
    if name is not None and (not name or name.endswith("/")):
        raise invalid_argument("expected an exact name")
    command = [binary, "backup", "--stdout-format", "json", "--wait", wait]
    if session:
        command += ["--session", session]
    timeout_seconds = seconds(timeout, "the backup timeout")
    if timeout_seconds:
        command += ["--timeout", f"{round(timeout_seconds * 1000)}ms"]
    command += ["--", absolute_path(source)]
    if name is not None:
        command.append(name)
    if prefix:
        command.append(prefix if prefix.endswith("/") else prefix + "/")
    return command


def backup_failure(stderr: str, returncode: int) -> LitebackupError:
    """The job's final structured outcome, with its first file diagnostic, or its text."""
    outcome: dict[str, str] | None = None
    first_message: str | None = None
    for line in stderr.strip().splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("code"), str) and isinstance(parsed.get("message"), str):
            if first_message is None:
                first_message = parsed["message"]
            outcome = parsed
    if outcome is None:
        return LitebackupError("backup_failed", stderr.strip() or f"foreground backup failed with exit status {returncode}")
    message = outcome["message"]
    if first_message and first_message != message:
        message += "\n" + first_message
    return LitebackupError(outcome["code"], message)


def cancelled_backup() -> LitebackupError:
    return LitebackupError(ErrorCode.CANCELLED, "the backup was cancelled")
