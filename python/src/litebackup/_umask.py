"""The caller's file mode creation mask, which acquire and restore send."""

from __future__ import annotations

import os
import threading

from .errors import invalid_argument

_UMASK_LOCK = threading.Lock()
"""Keeps two threads of this package from setting the umask at once."""


def current_umask() -> int:
    """This process's umask, so a file the daemon creates gets the bits a file this process created would.

    On Linux it reads the Umask line of /proc/self/status, which leaves the mask
    alone; elsewhere, as on macOS, it sets the mask and puts it back at once, so
    a file another thread creates in that moment could get the mask 0.
    """
    mask = status_umask("/proc/self/status")
    if mask is not None:
        return mask
    with _UMASK_LOCK:
        previous = os.umask(0)
        os.umask(previous)
    return previous


def status_umask(path: str) -> int | None:
    """Reads the octal Umask line of a /proc status file, None when it has none."""
    # Read as bytes: the process name on the Name line need not be UTF-8.
    try:
        with open(path, "rb") as status:
            lines = status.read().splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith(b"Umask:"):
            continue
        try:
            mask = int(line.removeprefix(b"Umask:").strip(), 8)
        except ValueError:
            return None
        return mask if 0 <= mask <= 0o777 else None
    return None


def request_umask(umask: int | None) -> int:
    """The umask a request sends: the one given, checked, or this process's."""
    if umask is None:
        return current_umask()
    if isinstance(umask, bool) or not isinstance(umask, int) or not 0 <= umask <= 0o777:
        raise invalid_argument("umask must be between 0 and 0777")
    return umask
