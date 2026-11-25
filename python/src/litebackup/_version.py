"""The SDK version and the oldest daemon it talks to."""

SDK_VERSION = "1.4.0"
"""Sent with every call. Daemons refuse SDKs older than their minimum with sdk_unsupported."""

MINIMUM_DAEMON_VERSION = "1.1.0"
"""Connecting to an older daemon fails with daemon_unsupported."""
