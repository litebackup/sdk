"""Starting a session's daemon with ``litebackup launch``, as the CLI does.

The launch command takes the session's launch lock, so concurrent processes
start one daemon. Launches are never retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
from collections.abc import Mapping

from ._binary import BINARY_PATH_ENVIRONMENT, litebackup_binary, start_failure_message
from ._credential import API_KEY_ENVIRONMENT
from .errors import ErrorCode, LitebackupError

LAUNCH_TIMEOUT_SECONDS = 30.0


def socket_listening(socket_path: str) -> bool:
    """Whether something accepts connections on the socket."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(1.0)
    try:
        connection.connect(socket_path)
    except OSError:
        return False
    finally:
        connection.close()
    return True


def _launch_command(
    session: str, environment: Mapping[str, str], api_key: str | None, binary_path: str | None
) -> tuple[list[str], dict[str, str]]:
    binary = litebackup_binary(environment, binary_path)
    launch_environment = {**environment, BINARY_PATH_ENVIRONMENT: binary}
    if api_key is not None:
        launch_environment[API_KEY_ENVIRONMENT] = api_key
    return [binary, "launch", "--session", session, "--stdout-format", "json"], launch_environment


def launch_refusal(output: str) -> tuple[str, str]:
    """The code and message of the last error the launch command reported as JSON.

    The code is drain_required when the launch refused another credential for a client that holds write locks, and
    launch_failed otherwise, also with the whole output when the command reported no error as JSON.
    """
    refusal = ""
    refusal_code: str = ErrorCode.LAUNCH_FAILED
    for line in output.splitlines():
        try:
            launch_diagnostic = json.loads(line)
        except ValueError:
            continue
        # A warning carries an event; an error does not.
        if isinstance(launch_diagnostic, dict) and "event" not in launch_diagnostic and isinstance(launch_diagnostic.get("code"), str):
            message = launch_diagnostic.get("message")
            if isinstance(message, str) and message:
                refusal = message
                drain_required = launch_diagnostic["code"] == ErrorCode.DRAIN_REQUIRED
                refusal_code = ErrorCode.DRAIN_REQUIRED if drain_required else ErrorCode.LAUNCH_FAILED
    if not refusal:
        return ErrorCode.LAUNCH_FAILED, output.strip()
    return refusal_code, refusal


def _launch_failed(output: str, cause: str) -> LitebackupError:
    code, refusal = launch_refusal(output)
    return LitebackupError(code, f"starting Litebackup failed: {refusal or cause}")


def launch_daemon(session: str, socket_path: str, environment: Mapping[str, str], api_key: str | None, binary_path: str | None) -> None:
    """Starts the session daemon when nothing listens on its socket."""
    if socket_listening(socket_path):
        return
    command, launch_environment = _launch_command(session, environment, api_key, binary_path)
    try:
        completed = subprocess.run(
            command, env=launch_environment, capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_SECONDS, check=False
        )
    except OSError as error:
        raise LitebackupError(ErrorCode.LAUNCH_FAILED, start_failure_message(command[0], error)) from error
    except subprocess.TimeoutExpired as error:
        raise _launch_failed("", str(error)) from error
    if completed.returncode != 0:
        raise _launch_failed(completed.stderr or completed.stdout, f"exit status {completed.returncode}")


async def launch_daemon_async(
    session: str, socket_path: str, environment: Mapping[str, str], api_key: str | None, binary_path: str | None
) -> None:
    """Starts the session daemon when nothing listens on its socket, without blocking the event loop."""
    if await asyncio.to_thread(socket_listening, socket_path):
        return
    command, launch_environment = _launch_command(session, environment, api_key, binary_path)
    try:
        process = await asyncio.create_subprocess_exec(
            *command, env=launch_environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as error:
        raise LitebackupError(ErrorCode.LAUNCH_FAILED, start_failure_message(command[0], error)) from error
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), LAUNCH_TIMEOUT_SECONDS)
    except BaseException as error:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await asyncio.shield(process.wait())
        if isinstance(error, TimeoutError):
            raise _launch_failed("", f"litebackup launch did not finish within {LAUNCH_TIMEOUT_SECONDS:g} s") from error
        raise
    if process.returncode != 0:
        raise _launch_failed((stderr or stdout).decode("utf-8", "replace"), f"exit status {process.returncode}")


def process_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    return dict(os.environ if environment is None else environment)
