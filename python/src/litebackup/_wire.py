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
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlencode

from . import _decode
from ._generated import models
from ._version import MINIMUM_DAEMON_VERSION, SDK_VERSION
from .errors import ErrorCode, LitebackupError, invalid_argument

Result = TypeVar("Result")

SDK_VERSION_HEADER = "X-Litebackup-SDK-Version"
CLIENT_VERSION_HEADER = "x-litebackup-client-version"
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

    @property
    def target(self) -> str:
        return f"{self.path}?{urlencode(self.query)}" if self.query else self.path

    @property
    def deadline_seconds(self) -> float | None:
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
        ErrorCode.INCOMPATIBLE_VERSION,
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
        raise invalid_argument("restore needs an aware datetime, such as datetime.now(UTC)")
    time = time.astimezone(UTC)
    text = time.strftime("%Y-%m-%dT%H:%M:%S")
    if time.microsecond:
        text += f".{time.microsecond:06d}".rstrip("0")
    return text + "Z"


def _body(**fields: Any) -> dict[str, Any]:
    """A request body without its unset fields."""
    return {key: value for key, value in fields.items() if value is not None and value is not False and value != ""}


# Operations.


def status(*, after: str = "", limit: int = 100) -> Call[models.Status]:
    return Call("GET", "/v1/status/show", {"after": after, "limit": str(limit)}, decode=model(models.Status))


def databases_page(*, after: str = "", limit: int = 100) -> Call[models.DatabaseStatusList]:
    return Call("GET", "/v1/databases/list", {"after": after, "limit": str(limit)}, decode=model(models.DatabaseStatusList))


def database(name: str) -> Call[models.DatabaseStatus]:
    return Call("GET", "/v1/databases/status", {"name": name}, decode=model(models.DatabaseStatus))


def sync(name: str) -> Call[models.DatabaseStatus]:
    return Call("POST", "/v1/databases/sync", body={"name": name}, decode=model(models.DatabaseStatus))


def offload(name: str) -> Call[models.DatabaseStatus]:
    return Call("POST", "/v1/databases/offload", body={"name": name}, decode=model(models.DatabaseStatus))


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
    name: str, *, to: PathSource | None, version: int | None = None, time: datetime | str | None = None
) -> Call[models.RestoreResult]:
    if not to:
        raise invalid_argument("restore needs a path to write to")
    if version is not None and time is not None:
        raise invalid_argument("restore takes a version or a time, not both")
    body: dict[str, Any] = {"name": name, "path": absolute_path(to)}
    if version is not None:
        body["version"] = version
    if time is not None:
        body["time"] = wire_time(time)
    return Call("POST", "/v1/databases/restore", body=body, decode=model(models.RestoreResult))


def acquire(
    name: str,
    *,
    to: PathSource | None = None,
    maintenance: bool = False,
    offload: Literal["never", "released"] = "never",
    machine_label: str | None = None,
    on_lost: Literal["conflict", "block"] = "conflict",
    timeout: Duration | None = None,
) -> Call[models.AcquireResult]:
    timeout_seconds = seconds(timeout, "the acquire timeout")
    body = _body(
        name=name,
        path=absolute_path(to) if to else None,
        maintenance=maintenance,
        offload="released" if offload == "released" else None,
        machine_label=machine_label,
        on_lost="block" if on_lost == "block" else None,
        timeout_millis=millis(timeout_seconds),
    )
    return Call("POST", "/v1/databases/acquire", body=body, timeout_seconds=timeout_seconds, decode=model(models.AcquireResult))


def release(
    name: str, *, wait: Durability | None = None, offload: bool = False, timeout: Duration | None = None
) -> Call[models.ReleaseResult]:
    timeout_seconds = seconds(timeout, "the release timeout")
    body = _body(name=name, wait=wait, offload=offload, timeout_millis=millis(timeout_seconds))
    return Call("POST", "/v1/databases/release", body=body, timeout_seconds=timeout_seconds, decode=model(models.ReleaseResult))


def start_backup(
    source: PathSource, *, name: str | None = None, prefix: str | None = None, maximum_local_databases: int | None = None
) -> Call[models.BackupRule]:
    if source == "" or source is None:
        raise invalid_argument("backup needs a path")
    if name and prefix:
        raise invalid_argument("choose a name or a prefix")
    body = _body(path=absolute_path(source), name=name, prefix=prefix, maximum_local_databases=maximum_local_databases or None)
    return Call("POST", "/v1/backup-rules/start", body=body, decode=model(models.BackupRule))


def stop_backup(name: str) -> Call[models.StopBackupResult]:
    return Call("POST", "/v1/backup-rules/stop", body={"name": name}, decode=model(models.StopBackupResult))


def stop_rule(rule_id: str) -> Call[models.StopBackupResult]:
    return Call("POST", "/v1/backup-rules/stop", body={"rule_id": rule_id}, decode=model(models.StopBackupResult))


def list_rules() -> Call[list[models.BackupRule]]:
    return Call("GET", "/v1/backup-rules/list", decode=lambda value: _decode.decode(models.BackupList, value).backups)


def start_replica(
    name: str, *, to: PathSource | None = None, write_protection: WriteProtection | None = None, timeout: Duration | None = None
) -> Call[models.DatabaseStatus]:
    timeout_seconds = seconds(timeout, "the replica timeout")
    body = _body(name=name, to=absolute_path(to) if to else None, write_protection=write_protection, timeout_millis=millis(timeout_seconds))

    def replica(value: Any) -> models.DatabaseStatus:
        result = _decode.decode(models.ReplicaResult, value)
        if result.database is None:
            raise LitebackupError(ErrorCode.INVALID_RESPONSE, "the daemon did not return the replica", 200)
        return result.database

    return Call("POST", "/v1/replicas/start", body=body, timeout_seconds=timeout_seconds, decode=replica)


def start_prefix_replica(
    prefix: str, *, to: PathSource | None, write_protection: WriteProtection | None = None
) -> Call[models.PrefixReplica]:
    if not to:
        raise invalid_argument("replicating a prefix needs a directory")
    body = _body(prefix=prefix, to=absolute_path(to), write_protection=write_protection)
    body["prefix"] = prefix

    def prefix_replica(value: Any) -> models.PrefixReplica:
        result = _decode.decode(models.ReplicaResult, value)
        if result.prefix is None:
            raise LitebackupError(ErrorCode.INVALID_RESPONSE, "the daemon did not return the prefix replica", 200)
        return result.prefix

    return Call("POST", "/v1/replicas/start", body=body, decode=prefix_replica)


def list_replicas() -> Call[models.ReplicaList]:
    return Call("GET", "/v1/replicas/list", decode=model(models.ReplicaList))


def stop_replica(name: str) -> Call[None]:
    return Call("POST", "/v1/replicas/stop", body={"name": name}, decode=nothing)


def stop_prefix_replica(prefix: str, *, to: PathSource | None) -> Call[None]:
    if not to:
        raise invalid_argument("stopping a prefix replica needs a directory")
    return Call("POST", "/v1/replicas/stop", body={"prefix": prefix, "to": absolute_path(to)}, decode=nothing)


def list_conflicts() -> Call[list[models.Conflict]]:
    return Call("GET", "/v1/conflicts/list", decode=lambda value: _decode.decode(models.ConflictList, value).conflicts)


def delete_conflict(conflict_id: str) -> Call[None]:
    return Call("POST", "/v1/conflicts/delete", body={"id": conflict_id}, decode=nothing)


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


def shutdown(*, wait: Literal["none", "server", "cloud"] | None = None, timeout: Duration | None = None) -> Call[models.ShutdownResult]:
    timeout_seconds = seconds(timeout, "the shutdown timeout")
    body = _body(wait=wait, timeout_millis=millis(timeout_seconds))
    return Call("POST", "/v1/session/shutdown", body=body, timeout_seconds=timeout_seconds, decode=model(models.ShutdownResult))


def start_failover(path: PathSource, *, name: str | None = None, timeout: Duration | None = None) -> Call[models.Failover]:
    if not path:
        raise invalid_argument("failover needs a path")
    timeout_seconds = seconds(timeout, "the failover timeout")
    body = _body(path=absolute_path(path), name=name, timeout_millis=millis(timeout_seconds))
    return Call("POST", "/v1/failovers/start", body=body, timeout_seconds=timeout_seconds, decode=model(models.Failover))


def is_terminal_watch_error(error: LitebackupError) -> bool:
    """Errors that end an event watch instead of being retried."""
    return error.code in (ErrorCode.SDK_UNSUPPORTED, ErrorCode.CLOSED, ErrorCode.CANCELLED, ErrorCode.INVALID_ARGUMENT)
