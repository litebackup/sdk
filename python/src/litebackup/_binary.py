"""Finding the litebackup binary. Nothing is downloaded at runtime."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from importlib import resources

from .errors import ErrorCode, LitebackupError

BINARY_PATH_ENVIRONMENT = "LITEBACKUP_PATH"


def install_command() -> str:
    """The one line that installs the litebackup command on Linux and macOS for whoever runs it."""
    return "curl -fsSL https://litebackup.com/cli.sh | bash"


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def bundled_binary() -> str | None:
    """The binary this platform's wheel carries, or None for the sdist and pure installs."""
    try:
        candidate = resources.files("litebackup").joinpath("_bin", "litebackup")
    except (ModuleNotFoundError, NotADirectoryError):
        return None
    path = os.fspath(candidate)  # type: ignore[call-overload]
    return path if _executable(path) else None


def litebackup_binary(environment: Mapping[str, str] | None = None, binary_path: str | None = None) -> str:
    """Finds the litebackup binary.

    The explicit path, then LITEBACKUP_PATH, then the binary the wheel bundles,
    then litebackup on PATH, then ~/.local/bin/litebackup. Raises
    binary_not_found with the install command when there is none, or when the
    explicit path or LITEBACKUP_PATH names no file.
    """
    environment = os.environ if environment is None else environment
    override = (binary_path or "").strip() or environment.get(BINARY_PATH_ENVIRONMENT, "").strip()
    if override:
        # An explicit path that names no file never falls back to another
        # binary; a file that cannot run fails later, as launch_failed.
        if missing_path(override):
            raise LitebackupError(
                ErrorCode.BINARY_NOT_FOUND,
                f"the litebackup binary {override} does not exist; "
                f"set binary_path or {BINARY_PATH_ENVIRONMENT} to it, or install it with: {install_command()}",
            )
        return override
    bundled = bundled_binary()
    if bundled:
        return bundled
    found = shutil.which("litebackup", path=environment.get("PATH", os.defpath))
    if found:
        return found
    home = environment.get("HOME") or os.path.expanduser("~")
    local = os.path.join(home, ".local", "bin", "litebackup")
    if _executable(local):
        return local
    raise LitebackupError(
        ErrorCode.BINARY_NOT_FOUND,
        f"the litebackup command is not installed; set {BINARY_PATH_ENVIRONMENT} or install it with: {install_command()}",
    )


def missing_path(path: str) -> bool:
    """Whether nothing is at path: it, or a directory on its way, does not exist.

    A path behind a directory this user may not enter is not missing.
    """
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def start_failure_message(binary: str, error: OSError) -> str:
    """Why starting binary failed.

    A file that is there but fails to start as missing cannot run: the
    interpreter its #! line names, or its dynamic loader, is not on this
    machine. The message names that line rather than repeating the file's
    path as if it were missing.
    """
    if not isinstance(error, (FileNotFoundError, NotADirectoryError)) or missing_path(binary):
        return f"starting {binary} failed: {error}"
    interpreter_line = script_interpreter_line(binary)
    unusable_binary = f"starting {binary} failed: it cannot run because"
    if not interpreter_line:
        return f"{unusable_binary} its interpreter or dynamic loader is not on this machine"
    return f"{unusable_binary} the interpreter its first line names is not on this machine: {interpreter_line}"


def script_interpreter_line(path: str) -> str:
    """The #! line that starts the file at path, or "" when it starts with none or cannot be read."""
    try:
        with open(path, "rb") as script:
            start = script.read(256)
    except OSError:
        return ""
    first_line = start.split(b"\n", 1)[0].decode("utf-8", "replace")
    if not first_line.startswith("#!"):
        return ""
    return first_line.strip()
