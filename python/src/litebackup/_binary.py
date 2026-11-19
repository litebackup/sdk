"""Finding the litebackup binary. Nothing is downloaded at runtime."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from importlib import resources

from .errors import ErrorCode, LitebackupError

BINARY_PATH_ENVIRONMENT = "LITEBACKUP_PATH"
TOKEN_ENVIRONMENT = "LITEBACKUP_TOKEN"


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
    binary_not_found with the install command when there is none.
    """
    environment = os.environ if environment is None else environment
    override = (binary_path or "").strip() or environment.get(BINARY_PATH_ENVIRONMENT, "").strip()
    if override:
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
