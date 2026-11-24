# Changelog

## 1.2.0

No changes to the SDKs. They are published at 1.2.0 with the litebackup client
1.2.0, and keep working with a 1.1.0 client.

## 1.1.0

The Litebackup SDKs for TypeScript, Python, and Go. They need the litebackup
client 1.1.0: an SDK refuses an older client with `daemon_unsupported`, and the
client answers an SDK older than 1.1.0 with 426 `sdk_unsupported`. The npm
package and the wheels bundle the client; Go finds it through `LITEBACKUP_PATH`
or `PATH`.

- **One contract.** `npm install @litebackup/sdk`, `pip install litebackup`, and
  `go get github.com/litebackup/sdk/litebackup` share the same operations,
  request bodies, errors, and lifecycle rules. Error codes take their values
  from `client-openapi.yaml`, which declares every header and every 426.
- **Python** has `Litebackup` and `AsyncLitebackup`, with integrations for
  sqlite3, aiosqlite, SQLAlchemy, Django, and FastAPI. Wheels bundle the client
  for Linux (glibc and musl) and macOS on x64 and arm64.
- **Driver helpers.** `databasePath(db)` from `@litebackup/sdk/better-sqlite3`,
  `/node-sqlite`, and `/drizzle`, and `database_path()` from the Python driver
  integrations, return the file a connection opened. In Go, `sqlitedsn`
  resolves a driver's DSN to its file.
- **One credential.** `LITEBACKUP_API_KEY` holds an API key or a join token, then
  `@` and the server address. A join token is exchanged once for a key of the
  machine; an API key is used as it is. The client option is `APIKey` in Go,
  `apiKey` in TypeScript, and `api_key` in Python. An invite token or anything
  else is `invalid_argument` before anything runs. The daemon uses the key it
  saved unless the variable holds a different credential.
- **Another credential needs a drain.** While the client holds write locks, a
  different credential is refused with `drain_required`, and nothing is sent or
  saved. Shut the client down with the current credential, then connect with the
  new one; `litebackup up --drop-locks` drops the locks when the old credential
  does not work, and each database takes a new lock at its next sync.
- **Idempotent registration.** Starting a backup rule again with the same path
  and settings returns the saved rule. Other settings for a saved path fail
  with `rule_exists`, and the error carries the saved rule. A restore never
  overwrites a file: an existing one, or journal files beside it, is
  `file_exists`.
- **Paths.** Restore, acquire, leases, and single replicas take `path`
  (Go `Path`); prefix replicas take `directory` (Go `Directory`), as does
  stopping one.
- **Pools.** `databases.pool` (TypeScript, Python) and `litebackup.NewPool`
  (Go) keep databases open between calls. `use` runs work with a backup
  rule's database, bringing an offloaded copy back first; `lease` runs work
  with a database that moves between machines, keeping its write lock until
  it is idle for a minute, the pool releases it, or the pool closes, creating
  a new one on first use; another machine's lease waits for it with
  `lockTimeout`. A failed
  close never releases; a failed release is a `LeaseCleanupError` with its
  `step`. Adapters open existing files only; Go's `SQLAdapter` takes a `DSN`
  function, and `DatabasePath` reads the file an open `*sql.DB` uses.
- **Offload.** An offload removes a released, closed, unchanged copy once the
  server holds its last version. `cloud` durability waits for the bucket: offload's
  and release's `durability`, and `offloadDurability` / `offload_durability` /
  `OffloadDurability` on acquire and on a backup start. Backup rules and acquired
  databases report their `offload_durability`; acquire's `offloadOnRelease`
  (`offload_on_release`, `OffloadOnRelease`) turns the offload on. `not_durable` means the chosen
  durability does not hold the synced version yet.
- **Acquire.** The answer's `decision` is `created`, `matched`, `resumed`,
  `downloaded`, `fast_forwarded`, or `replaced`. A local file that differs from
  the head and continues no head this machine synced is refused with 409
  `conflict` and left untouched; `discardLocal` (`discard_local`, Go
  `DiscardLocal`, `litebackup acquire --discard-local`) writes the
  head over it.
- **Lock errors.** `lock_unavailable` means another machine holds the write lock
  a call asks for. A foreground backup refused because something else writes the
  database is `busy`. A lost lock turns the copy `conflicted` with a
  `lock_lost` event, its file as it was, and apps can still write to it.
  An acquire resumes the copy while the head is still the version it last synced.
- **Version codes.** `daemon_unsupported`, `sdk_unsupported`, and
  `client_unsupported` each name the side that is too old. A refusal from the
  server arrives as `client_unsupported` (426), or with 503 as `not_configured`,
  `wrong_server`, `unauthenticated`, `forbidden`, `plan_limit`, or `integrity`;
  any other refusal is `unavailable`.
- **Failover.** A failover reports the starting role once, then every change,
  through `OnRoleChange`, `on_role_change`, and the TypeScript `origin`,
  `replica`, and `unknown` listeners. TypeScript calls them on the next turn of
  the event loop, so register them right after `failovers.start()` resolves.
- **Unknown role.** While the daemon does not answer (`connection_failed`, a call
  timeout, `daemon_unsupported`) the role is `unknown` at once, and 15 s of failed
  reads leave it unconfirmed. `IsOrigin()`, `isOrigin`, and `is_origin` are false
  then, so a health route stops sending writes to a machine whose daemon stopped.
  Once the daemon answers, the follower reads its role and reports it. Failover
  long-polls wait 10 s.
- **Diverged files.** A failover file that holds writes the server lacks, or
  differs from the head when the machine starts as the origin, stops there: the
  file is left as it is, the database is `conflicted`, and `OnRefused`,
  `on_refused`, or the `refused` listener hears the daemon's message. A replica
  start over a file that is not empty, the head, or a clean copy of its last
  synced version is refused with 409 `conflict`. `discardLocal` writes the head
  over the file (decision `replaced`).
- **Paging.** Each listing has a method for all of it, one for a page, and one for
  each item in turn: TypeScript `databases.all()`, `allVersions()`, and
  `versionsPage()`; Python `databases.all()` and `all_versions()`; Go
  `AllDatabases`, `AllVersions`, and `WatchEvents`. Event watches retry failed
  long-polls with backoff.
- **Events.** The client keeps events for 30 days and at most 100,000, except
  the current `path_unreadable` events (a path a backup rule cannot read). An
  events cursor older than the oldest event kept
  is answered with 410 `cursor_expired`, which ends a watch (Go
  `CodeCursorExpired`, TypeScript `ErrorCode.cursorExpired`, Python
  `ErrorCode.CURSOR_EXPIRED`). A failover resumes at the log's end and reads its
  role again.
- **Statuses.** A backup or failover origin that holds no write lock, as after a
  start or a drain, is `pending` until a sync takes the lock. A database this
  machine released or drained has an empty `writer_machine_label`.
- **Names.** A writer has `writer_machine_label` and `acquired_time`; a database
  status has `writer_machine_label`; a prefix replica has `database_count`, and a
  replica start answers with `prefix_replica`. `synced_cloud.archived` is when the
  server recorded the version as archived. `testdata/contract/names/sdk-names.json`
  lists the names the three SDKs share, and each SDK's tests check them.
- **Shutdown.** `ShutdownResult` has `released` and `failed`; `failed` lists a
  `DrainFailure` (`name`, `code`, `message`) for each database the drain could not
  sync, wait for, or release. Each keeps any write lock it holds, and an empty
  `failed` is the only complete shutdown.
- **Versions.** A listed version the primary bucket holds has `archived` (Go
  `Archived`, zero when absent; TypeScript an optional string; Python
  `datetime | None`). A version synced between archive passes has none, and
  restores from the server's disk until history pruning removes it.
- **Revert.** `databases.revert(name, version | time)` (Go `Revert(ctx, name,
  &RevertArgs{Version, Time})`) publishes a past version's content again as a new
  head, one past the current head, and answers with `RevertResult`. Every later
  version stays restorable. Only a closed origin of this machine reverts
  (`still_open`); a replica or standby is `invalid_argument`, and a file holding
  writes the server lacks is `conflict`. A version and a time together, neither,
  or a version below 1 is `invalid_argument` before anything is sent.
- **Binary lookup.** A binary path option (`BinaryPath`, `binaryPath`,
  `binary_path`) or `LITEBACKUP_PATH` that names no file is `binary_not_found`,
  with the path and the install command, and never falls back to another binary.
  A file that exists but cannot run is `launch_failed`.
- **Umask.** Acquire and restore send the caller's umask, so a file the daemon
  creates gets 0666 less it. The `umask` option (Go `Umask *fs.FileMode`) gives
  another; a value outside 0 to 0777 is `invalid_argument`. A copy the head
  overwrites keeps its bits.
- **Messages.** A server refusal reads as its message alone, with the code in the
  code field. `launch_failed` carries the launch command's own message.
- **Errors.** Every failure is a `LitebackupError` (Go `*litebackup.Error`,
  matched with `litebackup.IsCode`) with a `code` and the answer's other fields in
  `details`.
- **Cancellation.** TypeScript operations take a `signal`, Python async calls honor
  task cancellation, and Go calls take a context. Calls with a server-side timeout
  get a client-side deadline 30 seconds past it, and closing a client ends its
  calls, watches, and foreground backups.
