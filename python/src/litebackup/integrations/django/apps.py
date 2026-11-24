"""The AppConfig of ``litebackup.integrations.django``."""

from __future__ import annotations

import contextlib
import os
from typing import Any

from django.apps import AppConfig
from django.conf import settings
from django.core import exceptions
from django.db import connections
from django.db.backends.sqlite3.creation import DatabaseCreation

from ...client import Litebackup
from ...errors import LitebackupError
from .._sqlite_uri import file_from_uri, plain_file

_CLIENT_KEYS = ("api_key", "session", "socket_path", "binary_path")
_START_KEYS = ("name", "prefix", "maximum_local_databases", "offload_durability", "offload_rollback_journal")


def sqlite_file(alias: str) -> str:
    """The file a configured SQLite database opens, refusing in-memory and test databases."""
    connection = connections[alias]
    if connection.vendor != "sqlite":
        raise exceptions.ImproperlyConfigured(
            f"LITEBACKUP databases: {alias!r} uses the {connection.vendor} backend; only SQLite files are backed up"
        )
    name = connection.settings_dict["NAME"]
    filename = os.fspath(name) if name is not None else ""
    test_name = (connection.settings_dict.get("TEST") or {}).get("NAME")
    if DatabaseCreation.is_in_memory_db(filename) or (test_name and os.fspath(test_name) == filename):
        raise exceptions.ImproperlyConfigured(
            f"LITEBACKUP databases: {alias!r} is an in-memory or test database, which has no file to back up"
        )
    try:
        # Django's SQLite backend opens NAME with uri=True.
        path = file_from_uri(filename) if filename.startswith("file:") else plain_file(filename)
    except LitebackupError as error:
        raise exceptions.ImproperlyConfigured(f"LITEBACKUP databases: {alias!r}: {error.message}") from error
    if not os.path.isdir(os.path.dirname(path)):
        raise exceptions.ImproperlyConfigured(f"LITEBACKUP databases: {alias!r}: the directory of {path} does not exist")
    # Before the first migrate there is no file yet. An empty file is the empty
    # database SQLite would create on first connect, and the daemon backs it up
    # from its first write.
    with contextlib.suppress(FileExistsError):
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    return path


class LitebackupConfig(AppConfig):
    name = "litebackup.integrations.django"
    label = "litebackup"
    verbose_name = "Litebackup"

    def ready(self) -> None:
        configuration: dict[str, Any] = getattr(settings, "LITEBACKUP", {})
        if not configuration.get("enabled", True):
            return
        databases: dict[str, dict[str, Any]] = configuration.get("databases") or {}
        if not databases:
            return
        unknown = set(configuration) - {"databases", "enabled", *_CLIENT_KEYS}
        if unknown:
            raise exceptions.ImproperlyConfigured(f"LITEBACKUP has unknown keys: {', '.join(sorted(unknown))}")
        registrations = []
        for alias, options in databases.items():
            if alias not in settings.DATABASES:
                raise exceptions.ImproperlyConfigured(f"LITEBACKUP databases: {alias!r} is not in DATABASES")
            extra = set(options) - set(_START_KEYS)
            if extra:
                raise exceptions.ImproperlyConfigured(f"LITEBACKUP databases: {alias!r} has unknown keys: {', '.join(sorted(extra))}")
            registrations.append((sqlite_file(alias), options))
        with Litebackup(**{key: configuration[key] for key in _CLIENT_KEYS if key in configuration}) as litebackup:
            for path, options in registrations:
                try:
                    litebackup.backups.start(path, **options)
                except LitebackupError as error:
                    raise exceptions.ImproperlyConfigured(f"registering the backup of {path} failed: {error}") from error
