"""Session names and sockets, resolved exactly as the litebackup command does."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping

from .errors import invalid_argument

DEFAULT_SESSION = "default"
SYSTEM_CONFIGURATION_PATH = "/etc/litebackup/client.json"
"""Records a ``litebackup install service`` install; root and the service's user use its socket."""
_SESSION_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*")


def _runtime_directory(path: str) -> tuple[str, int | None] | None:
    try:
        with open(path, encoding="utf-8") as file:
            configuration = json.load(file)
    except (OSError, ValueError):
        return None
    if not isinstance(configuration, dict):
        return None
    directory = configuration.get("runtime_directory")
    if not isinstance(directory, str) or not directory:
        return None
    uid = configuration.get("uid")
    return directory, uid if isinstance(uid, int) else None


def session_socket(
    session: str | None, environment: Mapping[str, str], system_configuration: str = SYSTEM_CONFIGURATION_PATH
) -> tuple[str, str]:
    """Returns the session name and its socket path.

    Where ``litebackup install service`` ran as root, root and the service's user use
    the service's socket; where a user installed a service of their own, that user
    uses the directory it recorded. Everyone else uses
    $XDG_RUNTIME_DIR/litebackup/<session>/client.sock, or a per-user directory under
    the temporary directory, which must be this user's alone.
    """
    name = session or environment.get("LITEBACKUP_SESSION") or DEFAULT_SESSION
    if not _SESSION_PATTERN.fullmatch(name):
        raise invalid_argument(f"session names use [a-z0-9._-] and start with a letter or digit, got {name!r}")
    uid = os.getuid()
    system = _runtime_directory(system_configuration)
    if system is not None and (uid == 0 or uid == system[1]):
        return name, os.path.join(system[0], name, "client.sock")
    home = environment.get("HOME") or os.path.expanduser("~")
    user = _runtime_directory(os.path.join(home, ".config", "litebackup", "service.json"))
    if user is not None:
        return name, os.path.join(user[0], name, "client.sock")
    runtime_root = environment.get("XDG_RUNTIME_DIR")
    if not runtime_root:
        runtime_root = os.path.join(environment.get("TMPDIR") or tempfile.gettempdir(), f"litebackup-{uid}")
        _private_runtime_directory(runtime_root, uid)
    return name, os.path.join(runtime_root, "litebackup", name, "client.sock")


def _private_runtime_directory(directory: str, uid: int) -> None:
    """Makes sure the per-user runtime directory used without $XDG_RUNTIME_DIR is this user's own.

    The temporary directory is shared, so another local user could create it first
    and answer as the daemon. It is created with mode 0700 when missing; one that
    is a symlink, is not a directory, belongs to another user, or is open to anyone
    else is refused, as the litebackup command refuses it.
    """
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise invalid_argument(f"creating {directory} failed: {error}") from error
    status = os.lstat(directory)
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != uid or status.st_mode & 0o077:
        raise invalid_argument(
            f"{directory} is not a directory of this user's alone; remove it or set XDG_RUNTIME_DIR to this user's own directory"
        )
