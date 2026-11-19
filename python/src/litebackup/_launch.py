"""Starting a session's daemon with ``litebackup launch``, as the CLI does.

The launch command takes the session's launch lock, so concurrent processes
start one daemon. Launches are never retried.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from collections.abc import Mapping

from ._binary import BINARY_PATH_ENVIRONMENT, TOKEN_ENVIRONMENT, litebackup_binary
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
    session: str, environment: Mapping[str, str], token: str | None, binary_path: str | None
) -> tuple[list[str], dict[str, str]]:
    binary = litebackup_binary(environment, binary_path)
    launch_environment = {**environment, BINARY_PATH_ENVIRONMENT: binary}
    if token:
        launch_environment[TOKEN_ENVIRONMENT] = token
    return [binary, "launch", "--session", session], launch_environment


def _launch_failed(output: str, cause: str) -> LitebackupError:
    return LitebackupError(ErrorCode.LAUNCH_FAILED, f"starting Litebackup failed: {output.strip() or cause}")


def launch_daemon(session: str, socket_path: str, environment: Mapping[str, str], token: str | None, binary_path: str | None) -> None:
    """Starts the session daemon when nothing listens on its socket."""
    if socket_listening(socket_path):
        return
    command, launch_environment = _launch_command(session, environment, token, binary_path)
    try:
        completed = subprocess.run(
            command, env=launch_environment, capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _launch_failed("", str(error)) from error
    if completed.returncode != 0:
        raise _launch_failed(completed.stderr or completed.stdout, f"exit status {completed.returncode}")


async def launch_daemon_async(
    session: str, socket_path: str, environment: Mapping[str, str], token: str | None, binary_path: str | None
) -> None:
    """Starts the session daemon when nothing listens on its socket, without blocking the event loop."""
    if await asyncio.to_thread(socket_listening, socket_path):
        return
    command, launch_environment = _launch_command(session, environment, token, binary_path)
    try:
        process = await asyncio.create_subprocess_exec(
            *command, env=launch_environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as error:
        raise _launch_failed("", str(error)) from error
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), LAUNCH_TIMEOUT_SECONDS)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise _launch_failed("", f"litebackup launch did not finish within {LAUNCH_TIMEOUT_SECONDS:g} s") from error
    if process.returncode != 0:
        raise _launch_failed((stderr or stdout).decode("utf-8", "replace"), f"exit status {process.returncode}")


def process_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    return dict(os.environ if environment is None else environment)
