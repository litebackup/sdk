"""Back up, replicate, and restore SQLite databases with Litebackup.

The client talks to the litebackup daemon of a session over its unix socket and
starts it when it is not running, with the credential in LITEBACKUP_API_KEY::

    from litebackup import Litebackup

    with Litebackup() as litebackup:
        litebackup.backups.start("/var/lib/app/main.sqlite")

AsyncLitebackup has the same groups for asyncio. Driver and framework support is
in litebackup.integrations: sqlite3, aiosqlite, sqlalchemy, django, and fastapi.
"""

from ._binary import BINARY_PATH_ENVIRONMENT, install_command, litebackup_binary
from ._credential import API_KEY_ENVIRONMENT
from ._failover import AsyncFailover, Failover
from ._generated.models import (
    AcquireResult,
    ArchivedVersion,
    BackupRule,
    DatabaseStatus,
    DatabaseStatusList,
    DrainFailure,
    Event,
    EventList,
    PrefixReplica,
    ReleaseResult,
    ReplicaList,
    RestorableVersion,
    RestoreResult,
    RevertResult,
    ShutdownResult,
    Status,
    StopBackupResult,
    SyncedState,
    VersionList,
    Writer,
)
from ._paths import SYSTEM_CONFIGURATION_PATH, session_socket
from ._pool import DEFAULT_CLEANUP_TIMEOUT, Adapter, AsyncAdapter, AsyncPool, Pool
from ._version import MINIMUM_DAEMON_VERSION, SDK_VERSION
from ._wire import Durability, Duration, PathSource, WriteProtection
from .client import AsyncLitebackup, Litebackup
from .errors import ErrorCode, LeaseCleanupError, LitebackupError

__version__ = SDK_VERSION

__all__ = [
    "API_KEY_ENVIRONMENT",
    "BINARY_PATH_ENVIRONMENT",
    "DEFAULT_CLEANUP_TIMEOUT",
    "MINIMUM_DAEMON_VERSION",
    "SDK_VERSION",
    "SYSTEM_CONFIGURATION_PATH",
    "AcquireResult",
    "Adapter",
    "ArchivedVersion",
    "AsyncAdapter",
    "AsyncFailover",
    "AsyncLitebackup",
    "AsyncPool",
    "BackupRule",
    "DatabaseStatus",
    "DatabaseStatusList",
    "DrainFailure",
    "Durability",
    "Duration",
    "ErrorCode",
    "Event",
    "EventList",
    "Failover",
    "LeaseCleanupError",
    "Litebackup",
    "LitebackupError",
    "PathSource",
    "Pool",
    "PrefixReplica",
    "ReleaseResult",
    "ReplicaList",
    "RestorableVersion",
    "RestoreResult",
    "RevertResult",
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
