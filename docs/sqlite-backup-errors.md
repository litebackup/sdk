# Troubleshoot SQLite backups with Litebackup

Start with the local client and the server console. Use the same OS user and
session as your app; otherwise you may be inspecting a different daemon.

```sh
litebackup status
litebackup rules
litebackup replicate list
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
| Client joined the wrong server | A different `LITEBACKUP_API_KEY` replaces the session's key when its client next starts and holds no write lock, and is refused with `drain_required` while it holds one; run `litebackup drain` first, or select the intended session |
| `not_configured` | Finish bucket, recovery, and retention setup in the server console |
| `busy` | Another machine backs up that name; fix routing or use a deliberate separate namespace |
| `lock_unavailable` on acquire | Another machine owns the database; serialize the task and retry later, or acquire with `lockTimeout` (`lock_timeout`, `LockTimeout`, `--lock-timeout`) to wait until it is released |
| `not_found` | Check exact remote name and prefix; a new acquired database needs an existing local file, even if empty |
| `still_open` | Close every SQLite connection and other process holding the acquired file, then retry release |
| `changed` on offload or acquire | The file was written after its last sync, or while acquire replaced it; acquire it so the write syncs, or retry once the app stops writing |
| `not_durable` on offload | The server does not hold the synced version yet, or, with `durability: "cloud"`, the bucket does not; retry the offload once it does, or release with `offload` and the same `wait`, which removes the file on a later pass |
| `file_exists` on restore or acquire | The file already exists; choose a new file in an existing directory |
| `rule_exists` on backup start | The path already has a backup rule with other settings, and `details.rule` is that rule; stop it by its ID, then start the backup again with the new settings |
| `rule_database` on release | A backup rule on this machine keeps the database and its write lock, so there is nothing to release; stop its backup to hand it to another machine |
| `retrying` state | An origin's syncs fail and run again at `nextRetry`; `error` says why |
| `conflicted` | The file was refused and left as it is: it differs from the server head, or its write lock was lost, taken by another machine or broken by an admin. Restore the head to another path to compare, then acquire with `discardLocal` to take the head, or back the file up under another name |
| `SQLITE_BUSY` writing a replica | Use a readonly connection or route the write to the origin |
| `sdk_unsupported` | Upgrade the SDK to match the client/server; update the server before installed clients |
| Cloud wait times out | Check server archive delay, bucket health, network, disk space, and per-database `error` |

Awaiting `backups.start` reports registration errors. It does not monitor
future sync failures. Log `error` and `failed`, compare server and
cloud versions, and alert when the `syncing` timestamp is older than you can
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
