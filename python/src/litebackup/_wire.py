"""Sans-IO request building and answer decoding, shared by the sync and async clients.

Every operation is a function returning a Call: the request to send and how to
decode its answer. The transports only move bytes, so the two clients cannot
drift apart.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Generic, Literal, TypeVar, get_args
from urllib.parse import urlencode

from . import _decode
from ._generated import models
from ._umask import request_umask
from ._version import MINIMUM_DAEMON_VERSION, SDK_VERSION
from .errors import ErrorCode, LitebackupError, invalid_argument

Result = TypeVar("Result")

SDK_VERSION_HEADER = "Litebackup-SDK-Version"
CLIENT_VERSION_HEADER = "litebackup-client-version"
SDK_VERSION_VALUE = f"python/{SDK_VERSION}"
DEADLINE_GRACE_SECONDS = 30.0
"""Added to a call's server-side timeout for its client-side deadline, so the daemon's own timeout answer arrives first."""
EVENT_WAIT_SECONDS = 30.0
FIRST_RETRY_SECONDS = 0.25
MAXIMUM_RETRY_SECONDS = 10.0
RESPONSE_LIMIT = 64 << 20
"""Bounds one answer."""

Duration = float | int | timedelta
"""Seconds, or a timedelta."""
PathSource = str | os.PathLike[str]
"""A local path, relative or absolute."""
Durability = Literal["server", "cloud"]
WriteProtection = Literal["lock", "detect", "none"]

DatabaseRole = Literal["origin", "replica"]
DatabaseState = Literal["pending", "syncing", "retrying", "synced", "waiting", "offloaded", "stopped", "conflicted", "error"]
DURABILITY_SERVER: Final[Durability] = "server"
DURABILITY_CLOUD: Final[Durability] = "cloud"
ROLE_ORIGIN: Final[DatabaseRole] = "origin"
STATE_OFFLOADED: Final[DatabaseState] = "offloaded"
STATE_CONFLICTED: Final[DatabaseState] = "conflicted"
JOURNAL_MODE_ROLLBACK: Final = "rollback"
"""Wire values the SDK compares; tests check each against the generated models' literals."""


@dataclass(frozen=True, slots=True)
class Call(Generic[Result]):
    """One request and how to decode its answer."""

    method: Literal["GET", "POST"]
    path: str
    query: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    timeout_seconds: float | None = None
    """The server-side timeout; the client waits this plus DEADLINE_GRACE_SECONDS."""
    decode: Callable[[Any], Result] = field(default=lambda value: value)
    answer_deadline_seconds: float | None = None
    """A client-side deadline that replaces the one timeout_seconds gives, as a failover's role confirmation limit."""

    @property
    def target(self) -> str:
        return f"{self.path}?{urlencode(self.query)}" if self.query else self.path

    @property
    def deadline_seconds(self) -> float | None:
        if self.answer_deadline_seconds is not None:
            return self.answer_deadline_seconds
        return None if self.timeout_seconds is None else self.timeout_seconds + DEADLINE_GRACE_SECONDS

    def encoded_body(self) -> bytes | None:
        return None if self.body is None else json.dumps(self.body).encode()


def model(cls: type[Result]) -> Callable[[Any], Result]:
    """A decoder into a generated dataclass."""
    return lambda value: _decode.decode(cls, value)


def nothing(_value: Any) -> None:
    return None


def decode_answer(call: Call[Result], status: int, body: bytes) -> Result:
    """Decodes an answer, raising LitebackupError for anything but 200."""
    if status != 200:
        raise response_error(status, body)
    try:
        return call.decode(json.loads(body))
    except (ValueError, TypeError, KeyError) as error:
        raise LitebackupError(ErrorCode.INVALID_RESPONSE, f"decoding {call.path}: {error}", status) from error


def response_error(status: int, body: bytes) -> LitebackupError:
    """A non-200 answer: a JSON object's code, message, and other fields, or http_<status> with the text."""
    text = body.decode("utf-8", "replace")
    fallback = LitebackupError(f"http_{status}", text.strip(), status)
    try:
        decoded = json.loads(text)
    except ValueError:
        return fallback
    if not isinstance(decoded, dict):
        return fallback
    code = decoded.pop("code", None)
    message = decoded.pop("message", None)
    if not isinstance(code, str) or not code:
        return LitebackupError(f"http_{status}", message, status) if isinstance(message, str) else fallback
    return LitebackupError(code, message if isinstance(message, str) else "", status, decoded)


def compare_versions(left: str, right: str) -> int:
    """Orders dotted numeric versions, ignoring any -suffix; unparsable parts count as zero."""

    def parts(version: str) -> list[int]:
        numbers = []
        for part in version.split("-", 1)[0].split("."):
            try:
                numbers.append(int(part))
            except ValueError:
                numbers.append(0)
        return numbers

    left_parts, right_parts = parts(left), parts(right)
    length = max(len(left_parts), len(right_parts))
    left_parts += [0] * (length - len(left_parts))
    right_parts += [0] * (length - len(right_parts))
    return (left_parts > right_parts) - (left_parts < right_parts)


def check_daemon_version(client_version: str, session: str | None) -> None:
    """Refuses a daemon older than MINIMUM_DAEMON_VERSION, saying how to replace it."""
    if client_version and compare_versions(client_version, MINIMUM_DAEMON_VERSION) >= 0:
        return
    reported = f"is version {client_version}" if client_version else "does not report its version"
    drain = f"litebackup drain --session {session}" if session else "litebackup drain"
    raise LitebackupError(
        ErrorCode.DAEMON_UNSUPPORTED,
        f"the litebackup daemon {reported}, and this SDK needs {MINIMUM_DAEMON_VERSION} or later. "
        f"Stop it with `{drain}` so the next call starts the new binary, "
        "or restart the service if `litebackup install service` runs it",
    )


def connect_probe() -> Call[models.Status]:
    """The connect probe every SDK sends."""
    return Call("GET", "/v1/status/show", {"limit": "1"}, decode=model(models.Status))


def absolute_path(source: PathSource) -> str:
    """Resolves a path against the working directory and cleans it; the daemon needs absolute paths."""
    try:
        path = os.fspath(source)
    except TypeError:
        raise invalid_argument(f"expected a path, got {type(source).__name__}") from None
    if not isinstance(path, str):
        raise invalid_argument("expected a text path, not bytes")
    return os.path.abspath(path)


def seconds(duration: Duration | None, name: str) -> float | None:
    """A duration in seconds, or None when unset or zero, so the daemon's default applies."""
    if duration is None:
        return None
    value = duration.total_seconds() if isinstance(duration, timedelta) else float(duration)
    if value < 0 or value != value:
        raise invalid_argument(f"{name} must not be negative")
    return value or None


def millis(duration_seconds: float | None) -> int | None:
    return None if duration_seconds is None else round(duration_seconds * 1000)


def wire_time(time: datetime | str) -> str:
    """UTC with Z, fractional seconds only when they are not zero."""
    if isinstance(time, str):
        try:
            time = datetime.fromisoformat(time)
        except ValueError:
            raise invalid_argument(f"expected an ISO 8601 time, got {time!r}") from None
    if time.tzinfo is None:
        raise invalid_argument("a version time needs an aware datetime, such as datetime.now(UTC)")
    time = time.astimezone(UTC)
    text = time.strftime("%Y-%m-%dT%H:%M:%S")
    if time.microsecond:
        text += f".{time.microsecond:06d}".rstrip("0")
    return text + "Z"


def _body(**fields: Any) -> dict[str, Any]:
    """A request body without its unset fields."""
    return {key: value for key, value in fields.items() if value is not None and value is not False and value != ""}


# Operations.


def _page_limit(limit: int) -> str:
    """A page limit the daemon takes: a whole number from 1 to 1000."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise invalid_argument("the limit must be a whole number from 1 to 1000")
    return str(limit)


def status(*, after: str = "", limit: int = 100) -> Call[models.Status]:
    return Call("GET", "/v1/status/show", {"after": after, "limit": _page_limit(limit)}, decode=model(models.Status))


def databases_page(*, after: str = "", limit: int = 100) -> Call[models.DatabaseStatusList]:
    return Call("GET", "/v1/databases/list", {"after": after, "limit": _page_limit(limit)}, decode=model(models.DatabaseStatusList))


def database(name: str) -> Call[models.DatabaseStatus]:
    return Call("GET", "/v1/databases/status", {"name": name}, decode=model(models.DatabaseStatus))


def sync(name: str) -> Call[models.DatabaseStatus]:
    return Call("POST", "/v1/databases/sync", body={"name": name}, decode=model(models.DatabaseStatus))


def offload_durability_choice(durability: Durability | None) -> Durability | None:
    """Checks an offload durability and returns it, or None for the daemon's default, server."""
    if durability is not None and durability not in get_args(Durability):
        raise invalid_argument("offload durability must be server or cloud")
    return None if durability == "server" else durability


def offload(name: str, *, durability: Durability | None = None) -> Call[models.DatabaseStatus]:
    if durability is not None and durability not in get_args(Durability):
        raise invalid_argument("offload durability must be server or cloud")
    body = {"name": name} | _body(durability=durability)
    return Call("POST", "/v1/databases/offload", body=body, decode=model(models.DatabaseStatus))


def writer(name: str) -> Call[models.Writer]:
    return Call("GET", "/v1/databases/writer", {"name": name}, decode=model(models.Writer))


def versions_page(name: str, *, after: int = 0, limit: int | None = None) -> Call[models.VersionList]:
    query = {"name": name}
    if after > 0:
        query["after"] = str(after)
    if limit is not None:
        query["limit"] = str(limit)
    return Call("GET", "/v1/databases/versions", query, decode=model(models.VersionList))


def restore(
    name: str,
    *,
    path: PathSource | None,
    version: int | None = None,
    time: datetime | str | None = None,
    umask: int | None = None,
) -> Call[models.RestoreResult]:
    if not path:
        raise invalid_argument("restore needs a path to write to")
    if version is not None and time is not None:
        raise invalid_argument("restore takes a version or a time, not both")
    body: dict[str, Any] = {"name": name, "path": absolute_path(path)}
    if version is not None:
        body["version"] = version
    if time is not None:
        body["time"] = wire_time(time)
    body["umask"] = request_umask(umask)
    return Call("POST", "/v1/databases/restore", body=body, decode=model(models.RestoreResult))


def revert(name: str, *, version: int | None = None, time: datetime | str | None = None) -> Call[models.RevertResult]:
    if (version is None) == (time is None):
        raise invalid_argument("revert takes a version or a time, one of them")
    body: dict[str, Any] = {"name": name}
    if version is not None:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise invalid_argument("revert needs a positive whole version")
        body["version"] = version
    if time is not None:
        body["time"] = wire_time(time)
    return Call("POST", "/v1/databases/revert", body=body, decode=model(models.RevertResult))


def acquire(
    name: str,
    *,
    path: PathSource | None = None,
    maintenance: bool = False,
    offload_on_release: bool = False,
    offload_durability: Durability = "server",
    machine_label: str | None = None,
    timeout: Duration | None = None,
    lock_timeout: Duration | None = None,
    umask: int | None = None,
    discard_local: bool = False,
) -> Call[models.AcquireResult]:
    timeout_seconds = seconds(timeout, "the acquire timeout")
    lock_timeout_seconds = seconds(lock_timeout, "the lock timeout")
    # name is required, so it is sent even when empty and the daemon answers for it.
    body = {"name": name} | _body(
        path=absolute_path(path) if path else None,
        maintenance=maintenance,
        discard_local=discard_local,
        offload_on_release=offload_on_release,
        offload_durability=offload_durability_choice(offload_durability),
        machine_label=machine_label,
        timeout_millis=millis(timeout_seconds),
        lock_timeout_millis=millis(lock_timeout_seconds),
    )
    body["umask"] = request_umask(umask)
    # No timeout leaves the call unbounded, as the daemon applies its own.
    call_seconds = timeout_seconds + (lock_timeout_seconds or 0) if timeout_seconds else None
    return Call("POST", "/v1/databases/acquire", body=body, timeout_seconds=call_seconds, decode=model(models.AcquireResult))


def release(
    name: str, *, durability: Durability | None = None, offload: bool = False, timeout: Duration | None = None
) -> Call[models.ReleaseResult]:
    timeout_seconds = seconds(timeout, "the release timeout")
    body = {"name": name} | _body(durability=durability, offload=offload, timeout_millis=millis(timeout_seconds))
    return Call("POST", "/v1/databases/release", body=body, timeout_seconds=timeout_seconds, decode=model(models.ReleaseResult))


def start_backup(
    source: PathSource,
    *,
    name: str | None = None,
    prefix: str | None = None,
    maximum_local_databases: int | None = None,
    offload_durability: Durability = "server",
    offload_rollback_journal: bool = False,
) -> Call[models.BackupRule]:
    if source == "" or source is None:
        raise invalid_argument("backup needs a path")
    if name and prefix:
        raise invalid_argument("choose a name or a prefix")
    body = _body(
        path=absolute_path(source),
        name=name,
        prefix=prefix,
        maximum_local_databases=maximum_local_databases or None,
        offload_durability=offload_durability_choice(offload_durability),
        offload_rollback_journal=offload_rollback_journal,
    )
    return Call("POST", "/v1/backup-rules/start", body=body, decode=model(models.BackupRule))


def stop_backup(name: str) -> Call[models.StopBackupResult]:
    if not name:
        raise invalid_argument("stopping a backup needs a name")
    return Call("POST", "/v1/backup-rules/stop", body={"name": name}, decode=model(models.StopBackupResult))


def stop_rule(rule_id: str) -> Call[models.StopBackupResult]:
    if not rule_id:
        raise invalid_argument("stopping a rule needs its ID")
    return Call("POST", "/v1/backup-rules/stop", body={"rule_id": rule_id}, decode=model(models.StopBackupResult))


def list_rules() -> Call[list[models.BackupRule]]:
    return Call("GET", "/v1/backup-rules/list", decode=lambda value: _decode.decode(models.BackupList, value).rules)


def start_replica(
    name: str, *, path: PathSource | None = None, write_protection: WriteProtection | None = None, timeout: Duration | None = None
) -> Call[models.DatabaseStatus]:
    timeout_seconds = seconds(timeout, "the replica timeout")
    body = _body(
        name=name, path=absolute_path(path) if path else None, write_protection=write_protection, timeout_millis=millis(timeout_seconds)
    )

    def replica(value: Any) -> models.DatabaseStatus:
        started = _decode.decode(models.ReplicaResult, value)
        if started.database is None:
            raise LitebackupError(ErrorCode.INVALID_RESPONSE, "the daemon did not return the replica", 200)
        return started.database

    return Call("POST", "/v1/replicas/start", body=body, timeout_seconds=timeout_seconds, decode=replica)


def start_prefix_replica(
    prefix: str, *, directory: PathSource | None, write_protection: WriteProtection | None = None
) -> Call[models.PrefixReplica]:
    if not directory:
        raise invalid_argument("replicating a prefix needs a directory")
    body = _body(prefix=prefix, directory=absolute_path(directory), write_protection=write_protection)
    body["prefix"] = prefix

    def prefix_replica(value: Any) -> models.PrefixReplica:
        started = _decode.decode(models.ReplicaResult, value)
        if started.prefix_replica is None:
            raise LitebackupError(ErrorCode.INVALID_RESPONSE, "the daemon did not return the prefix replica", 200)
        return started.prefix_replica

    return Call("POST", "/v1/replicas/start", body=body, decode=prefix_replica)


def list_replicas() -> Call[models.ReplicaList]:
    return Call("GET", "/v1/replicas/list", decode=model(models.ReplicaList))


def stop_replica(name: str) -> Call[None]:
    return Call("POST", "/v1/replicas/stop", body={"name": name}, decode=nothing)


def stop_prefix_replica(prefix: str, *, directory: PathSource | None) -> Call[None]:
    if not directory:
        raise invalid_argument("stopping a prefix replica needs a directory")
    return Call("POST", "/v1/replicas/stop", body={"prefix": prefix, "directory": absolute_path(directory)}, decode=nothing)


def events_page(*, after: int = 0, wait: Duration | None = None, limit: int | None = None) -> Call[models.EventList]:
    wait_seconds = seconds(wait, "the event wait")
    query: dict[str, str] = {}
    if after:
        query["after"] = str(after)
    if wait_seconds:
        query["wait_millis"] = str(millis(wait_seconds))
    if limit:
        query["limit"] = str(limit)
    return Call("GET", "/v1/events/list", query, timeout_seconds=wait_seconds, decode=model(models.EventList))


def shutdown(
    *, durability: Literal["none", "server", "cloud"] | None = None, timeout: Duration | None = None
) -> Call[models.ShutdownResult]:
    timeout_seconds = seconds(timeout, "the shutdown timeout")
    body = _body(durability=durability, timeout_millis=millis(timeout_seconds))
    return Call("POST", "/v1/session/shutdown", body=body, timeout_seconds=timeout_seconds, decode=model(models.ShutdownResult))


def start_failover(
    path: PathSource, *, name: str | None = None, timeout: Duration | None = None, discard_local: bool = False
) -> Call[models.Failover]:
    if not path:
        raise invalid_argument("failover needs a path")
    timeout_seconds = seconds(timeout, "the failover timeout")
    body = _body(path=absolute_path(path), name=name, timeout_millis=millis(timeout_seconds), discard_local=discard_local)
    return Call("POST", "/v1/failovers/start", body=body, timeout_seconds=timeout_seconds, decode=model(models.Failover))


def is_terminal_watch_error(error: LitebackupError) -> bool:
    """Errors that end an event watch instead of being retried."""
    return error.code in (
        ErrorCode.SDK_UNSUPPORTED,
        ErrorCode.CLOSED,
        ErrorCode.CANCELLED,
        ErrorCode.INVALID_ARGUMENT,
        ErrorCode.CURSOR_EXPIRED,
    )
