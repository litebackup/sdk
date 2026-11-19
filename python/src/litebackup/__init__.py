"""Back up, replicate, and restore SQLite databases with Litebackup.

The client talks to the litebackup daemon of a session over its unix socket and
starts it when it is not running, joining with LITEBACKUP_TOKEN the first time::

    from litebackup import Litebackup

    with Litebackup() as litebackup:
        litebackup.backups.start("/var/lib/app/main.sqlite")

AsyncLitebackup has the same groups for asyncio. Driver and framework support is
in litebackup.integrations: sqlite3, aiosqlite, sqlalchemy, django, and fastapi.
"""

from ._binary import BINARY_PATH_ENVIRONMENT, TOKEN_ENVIRONMENT, install_command, litebackup_binary
from ._failover import AsyncFailover, Failover
from ._generated.models import (
    AcquireResult,
    BackupRule,
    Conflict,
    DatabaseStatus,
    DatabaseStatusList,
    Event,
    EventList,
    PrefixReplica,
    ReleaseResult,
    ReplicaList,
    RestorableVersion,
    RestoreResult,
    ShutdownResult,
    Status,
    StopBackupResult,
    SyncedState,
    VersionList,
    Writer,
)
from ._managed import DEFAULT_CLEANUP_TIMEOUT, AsyncScopeAdapter, ScopeAdapter
from ._paths import SYSTEM_CONFIGURATION_PATH, session_socket
from ._version import MINIMUM_DAEMON_VERSION, SDK_VERSION
from ._wire import Durability, Duration, PathSource, WriteProtection
from .client import AsyncLitebackup, Litebackup
from .errors import ErrorCode, LitebackupError

__version__ = SDK_VERSION

__all__ = [
    "BINARY_PATH_ENVIRONMENT",
    "DEFAULT_CLEANUP_TIMEOUT",
    "MINIMUM_DAEMON_VERSION",
    "SDK_VERSION",
    "SYSTEM_CONFIGURATION_PATH",
    "TOKEN_ENVIRONMENT",
    "AcquireResult",
    "AsyncFailover",
    "AsyncLitebackup",
    "AsyncScopeAdapter",
    "BackupRule",
    "Conflict",
    "DatabaseStatus",
    "DatabaseStatusList",
    "Durability",
    "Duration",
    "ErrorCode",
    "Event",
    "EventList",
    "Failover",
    "Litebackup",
    "LitebackupError",
    "PathSource",
    "PrefixReplica",
    "ReleaseResult",
    "ReplicaList",
    "RestorableVersion",
    "RestoreResult",
    "ScopeAdapter",
    "ShutdownResult",
    "Status",
    "StopBackupResult",
    "SyncedState",
    "VersionList",
    "WriteProtection",
    "Writer",
    "install_command",
    "litebackup_binary",
    "session_socket",
]
