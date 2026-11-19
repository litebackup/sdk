"""Session names and sockets, resolved exactly as the litebackup command does."""

from __future__ import annotations

import json
import os
import re
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
    the temporary directory.
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
    runtime_root = environment.get("XDG_RUNTIME_DIR") or os.path.join(
        environment.get("TMPDIR") or tempfile.gettempdir(), f"litebackup-{uid}"
    )
    return name, os.path.join(runtime_root, "litebackup", name, "client.sock")
