"""``backups.once()``: one foreground ``litebackup backup`` job, as the CLI runs it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import time

from . import _wire
from ._binary import missing_path, start_failure_message
from ._wire import Duration, PathSource, absolute_path, seconds
from .errors import ErrorCode, LitebackupError, invalid_argument


def backup_command(
    binary: str, source: PathSource, *, session: str | None, name: str | None, prefix: str | None, durability: str, timeout: Duration | None
) -> list[str]:
    """The litebackup command line of one foreground job."""
    if not source:
        raise invalid_argument("backup needs a path")
    if name is not None and prefix is not None:
        raise invalid_argument("choose a name or a prefix")
    if name is not None and (not name or name.endswith("/")):
        raise invalid_argument("expected an exact name")
    command = [binary, "backup", "--stdout-format", "json", "--durability", durability]
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


MAXIMUM_FILE_DIAGNOSTICS = 20
"""How many of the job's file diagnostics a failed backup's message lists."""


def reported_failure(line: str | bytes) -> dict[str, str] | None:
    """The job's structured error on this line of its error output, or None for a log line."""
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("code"), str) and isinstance(parsed.get("message"), str):
        return parsed
    return None


def backup_failure(stderr: str, returncode: int) -> LitebackupError:
    """The job's final structured report, followed by each file diagnostic it printed before it, or its text.

    Each file diagnostic names the file, and the machine that writes it for a busy one.
    """
    final_report: dict[str, str] | None = None
    diagnostics: list[str] = []
    for line in stderr.strip().splitlines():
        failure = reported_failure(line)
        if failure is None:
            continue
        final_report = failure
        # A file reported twice is listed once, so the limit counts distinct files.
        if len(diagnostics) < MAXIMUM_FILE_DIAGNOSTICS and failure["message"] not in diagnostics:
            diagnostics.append(failure["message"])
    if final_report is None:
        return LitebackupError("backup_failed", stderr.strip() or f"foreground backup failed with exit status {returncode}")
    message = final_report["message"]
    listed = {message}
    for diagnostic in diagnostics:
        if diagnostic not in listed:
            listed.add(diagnostic)
            message += "\n" + diagnostic
    return LitebackupError(final_report["code"], message)


def start_failure(binary: str, error: OSError) -> LitebackupError:
    """binary_not_found when the binary is missing, launch_failed when it cannot run.

    FileNotFoundError while the file is there means its script interpreter is
    missing: the file cannot run.
    """
    missing = isinstance(error, (FileNotFoundError, NotADirectoryError)) and missing_path(binary)
    code = ErrorCode.BINARY_NOT_FOUND if missing else ErrorCode.LAUNCH_FAILED
    return LitebackupError(code, start_failure_message(binary, error))


def cancelled_backup() -> LitebackupError:
    return LitebackupError(ErrorCode.CANCELLED, "the backup was cancelled")


STOP_GRACE_SECONDS = 15.0
"""How long a job has to exit after SIGTERM before it gets SIGKILL."""

DRAIN_SECONDS = 1.0
"""How long a job's error output may stay open after the job exited, in a process it started, before reading stops."""

_POLL_SECONDS = 0.05
"""How often the wait for a job's exit checks it."""

_STDERR_PART = 32768
"""How much of the start and of the end of a job's error output is kept: its first diagnostic and its final report."""


def stop_job(process: subprocess.Popen[str]) -> None:
    """Stops a running job: SIGTERM, then SIGKILL when it has not exited within STOP_GRACE_SECONDS."""
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        process.wait(STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        process.wait()


def await_job(process: subprocess.Popen[str], deadline_seconds: float | None) -> tuple[str, bool]:
    """Reads a job's error output until it exits, killing it once deadline_seconds pass.

    Returns the output and whether the deadline ended the job. Reading stops
    DRAIN_SECONDS after the job exited even if a process it started keeps the
    output open.
    """
    started = time.monotonic()
    exit_time: float | None = None
    timed_out = False
    while True:
        try:
            _, output = process.communicate(timeout=_POLL_SECONDS)
            return output or "", timed_out
        except subprocess.TimeoutExpired as waiting:
            now = time.monotonic()
            if process.poll() is not None:
                exit_time = exit_time if exit_time is not None else now
                if now - exit_time >= DRAIN_SECONDS:
                    partial = waiting.stderr
                    if process.stderr is not None:
                        process.stderr.close()
                    text = partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial or ""
                    return text, timed_out
            elif deadline_seconds is not None and now - started >= deadline_seconds:
                timed_out = True
                with contextlib.suppress(ProcessLookupError):
                    process.kill()


async def _exited(process: asyncio.subprocess.Process) -> None:
    """Waits for the job itself to exit; unlike wait(), not for its output, which a process it started may hold open."""
    # Polled: asyncio's own exit wait also waits for the pipes to close.
    while process.returncode is None:  # noqa: ASYNC110
        await asyncio.sleep(_POLL_SECONDS)


async def stop_job_async(process: asyncio.subprocess.Process) -> None:
    """Stops a running job: SIGTERM, then SIGKILL when it has not exited within STOP_GRACE_SECONDS."""
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(_exited(process), STOP_GRACE_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await _exited(process)


async def await_job_async(process: asyncio.subprocess.Process, deadline_seconds: float | None) -> tuple[bytes, bool]:
    """Reads a job's error output until it exits, stopping it once deadline_seconds pass.

    Returns the output and whether the deadline ended the job. Cancelling stops
    the job, then re-raises. Reading stops DRAIN_SECONDS after the job exited
    even if a process it started keeps the output open.
    """
    head = bytearray()
    tail = bytearray()
    # Structured error lines are kept wherever they fall, however much log
    # output lies between them: the first file diagnostics and the final report.
    pending = bytearray()
    failure_lines: list[bytes] = []
    stream = process.stderr

    # The distinct file diagnostics, up to the limit, and the last structured line.
    failure_messages: list[str] = []
    last_line: list[bytes] = []

    def keep_failure_line(line: bytes) -> None:
        failure = reported_failure(line)
        if failure is None:
            return
        last_line[:] = [line]
        if len(failure_lines) < MAXIMUM_FILE_DIAGNOSTICS and failure["message"] not in failure_messages:
            failure_messages.append(failure["message"])
            failure_lines.append(line)

    async def read() -> None:
        if stream is None:
            return
        while chunk := await stream.read(_STDERR_PART):
            room = _STDERR_PART - len(head)
            head.extend(chunk[:room])
            tail.extend(chunk[room:])
            del tail[:-_STDERR_PART]
            pending.extend(chunk)
            *lines, rest = bytes(pending).split(b"\n")
            for line in lines:
                keep_failure_line(line)
            # A line this long is log output; only its end is kept, and it does not parse.
            pending[:] = rest[-_STDERR_PART:]

    reader = asyncio.ensure_future(read())
    timed_out = False
    try:
        try:
            try:
                await asyncio.wait_for(_exited(process), deadline_seconds)
            except TimeoutError:
                # The exit check polls, so a job may have exited just before the deadline.
                if process.returncode is None:
                    timed_out = True
                    await asyncio.shield(stop_job_async(process))
        except asyncio.CancelledError:
            # Shielded, so a second cancellation cannot leave the job running.
            await asyncio.shield(stop_job_async(process))
            raise
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(reader), DRAIN_SECONDS)
    finally:
        reader.cancel()
        _release(process, drained=reader.done() and not reader.cancelled())
        # The last line may end without a line break, or the reading may have
        # stopped at the drain timeout; either way it is the job's last word.
        keep_failure_line(bytes(pending))
    if reader.done() and not reader.cancelled():
        # The output ended, so wait() returns at once and the transport closes.
        await process.wait()
    if last_line:
        # The structured lines alone, in order, the final report last.
        return b"\n".join([*failure_lines, *last_line]), timed_out
    return bytes(head + (b"\n" if tail and len(head) == _STDERR_PART else b"") + tail), timed_out


def _release(process: asyncio.subprocess.Process, *, drained: bool) -> None:
    """Closes the pipes of a job whose output a process it started still holds open.

    asyncio offers no public way to stop waiting for such pipes, so this closes the
    process's transport, which also releases its pipes.
    """
    if drained:
        return
    transport = getattr(process, "_transport", None)
    if transport is not None:
        transport.close()


def job_deadline(timeout: Duration | None) -> float | None:
    """How long to wait for a job: its timeout plus the grace that lets the job report its own timeout first."""
    timeout_seconds = seconds(timeout, "the backup timeout")
    return None if timeout_seconds is None else timeout_seconds + _wire.DEADLINE_GRACE_SECONDS


def timed_out_backup() -> LitebackupError:
    return LitebackupError(ErrorCode.TIMEOUT, "the foreground backup did not finish within its timeout")
