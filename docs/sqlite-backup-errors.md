# Troubleshoot SQLite backups with Litebackup

Start with the local client and the server console. Use the same OS user and
session as your app; otherwise you may be inspecting a different daemon.

```sh
litebackup status
litebackup rules
litebackup replicate list
litebackup conflict list
```

For a Linux client service, as the user that installed it:

```sh
systemctl --user status litebackup
journalctl --user -u litebackup -f
```

For a system service that root installed, use `sudo systemctl status litebackup`
and `sudo journalctl -u litebackup -f`. On macOS, the service logs to
`~/.local/state/litebackup/default/daemon.log` for the default session.

## Common failures

| Symptom / code | Check / action |
| --- | --- |
| No client binary | Keep npm optional dependencies enabled, install the CLI for Go, or set an absolute `LITEBACKUP_PATH` |
| Connection / launch failure | Inspect daemon logs, socket permissions, session, and the service user's access |
| Client joined the wrong server | Existing session credentials take precedence over a new environment token; select the intended session |
| `not_configured` | Finish bucket, recovery, and retention setup in the server console |
| `busy` | Another machine backs up that name; fix routing or use a deliberate separate namespace |
| `in_use` on acquire | Another machine owns the database; serialize the task and retry later |
| `not_found` | Check exact remote name and prefix; a new acquired database needs an existing local file, even if empty |
| `still_open` | Close every SQLite connection and other process holding the acquired file, then retry release |
| `exists` on restore | Choose a new file in an existing directory |
| `retrying` state | An origin's syncs fail and run again at `nextRetryTime`; `lastError` says why |
| `conflicted` | Inspect the preserved conflict copy; choose an application-specific recovery path |
| `SQLITE_BUSY` writing a replica | Use a readonly connection or route the write to the origin |
| `sdk_unsupported` | Upgrade the SDK to match the client/server; update the server before installed clients |
| Cloud wait times out | Check server archive delay, bucket health, network, disk space, and per-database `lastError` |

Awaiting `backups.start` reports registration errors. It does not monitor
future sync failures. Log `lastError` and `lastErrorTime`, compare server and
cloud versions, and alert when `firstUnsyncedChangeTime` is older than you can
afford to lose. A new empty file can remain at version zero until data is written.

## Avoid silent scheduled failures

Treat a failed wait or cleanup as a failed job. Check the scheduler's exit
status as well as logs, and alert when no successful run occurs in the
expected window. Retrying should use the same logical database names.

A timeout does not erase local data or undo an accepted remote sync. A killed job may leave a writer lease until expiry. See [Cron](sqlite-cron-backup.md) and [Temporal](sqlite-temporal.md).

## Before reporting an issue

Include SDK version, client version, OS/architecture, whether the client is
service-managed, the operation, and the error code. Remove join tokens,
bucket credentials, recovery material, and sensitive paths or database
contents from shared logs. Describe whether the issue also occurs through
the CLI under the same user and session.
