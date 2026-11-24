# SDK contract

The Go, TypeScript, and Python SDKs are three spellings of one client. This
page is the behavior they share; `testdata/contract/` holds the fixtures each
SDK's test suite runs, so a change here is a change to every SDK at once.

The wire protocol is `client-openapi.yaml`. Wire names are snake_case and are
never renamed on the wire. TypeScript returns camelCase keys, Go uses its own
field names, and Python keeps the wire names.

## Fixtures

| Directory | Each file holds | Checked by |
| --- | --- | --- |
| `requests/` | an operation, its arguments, and the one request it sends, or the SDK error it raises without sending | every SDK |
| `errors/` | a daemon answer and the error the SDK raises from it | every SDK |
| `version/` | the daemon version a connect sees and whether the SDK accepts it | every SDK |
| `pagination/`, `events/` | request and answer exchanges, and the items an iterator yields | every SDK |
| `lifecycle/` | a pool lease scenario: which steps fail, the daemon calls, and the errors the lease and closing the pool raise | every SDK |
| `binary/` | platforms and their npm package, release file, and wheel platform | TypeScript, Python, wheel builds |
| `credentials/` | a `LITEBACKUP_API_KEY` value and whether it is an API key, a join token, none, or refused | every SDK |

Arguments in fixtures are snake_case and language-neutral:

- `{cwd}` in a string is the working directory the runner sets before the
  call. Relative paths resolve against it.
- `"{umask}"`, the whole string with its quotes, is the test process's file
  mode creation mask as a JSON number, such as `18` for 022.
- `{"$path": "data/main.sqlite"}` is the language's path type: a `pathlib.Path`
  in Python, a `file:` URL in TypeScript (made absolute against `{cwd}`), and a
  plain string in Go.
- Durations are strings such as `"1500ms"`, `"90s"`, or `"2m"`. Times are RFC 3339
  strings. Each runner converts them to `time.Duration`/`time.Time`, a `Duration`
  string/`Date`, or `timedelta`/`datetime`.
- `offload_on_release` on acquire is `OffloadOnRelease` in Go, a bool.
  `offload_durability` on acquire and on a backup start is
  `OffloadDurability` in Go, a `Durability`, and `durability` on offload is
  `OffloadArgs.Durability`.
- `names/sdk-names.json` lists each operation's name in the three SDKs
  and the types all three export under one name; each SDK's tests check
  their own names against it, and the Go tests check that every fixture's
  operation is listed.

A request's `query` compares as a map of strings regardless of order. Its
`body` compares as JSON: a field absent from the fixture must be absent from
the request.

Each runner answers the connect probe itself, so fixtures list only the
operation's own requests.

## Requests

- **Paths.** Every local path is sent absolute and cleaned: a relative path
  resolves against the working directory when the call is made, and `..`
  segments are removed. Python accepts `os.PathLike`, and TypeScript accepts
  `file:` URLs.
- **Defaults are omitted.** An option left out, or set to the daemon's
  default (`maintenance: false`, `offload_on_release: false`, `offload: false`,
  `offload_durability: "server"`, a zero duration), is not
  sent; the `durability` of release and offload is sent as given. Only settings
  that differ from the default go on the wire, so the three SDKs send
  identical bodies.
- **The umask.** Acquire and restore send the caller's file mode creation
  mask as `umask`, so a file the daemon creates gets the bits a file the
  caller created would (0666 less it). Each SDK reads its process's umask
  when the call is made, unless the `umask` option gives one; `0` is sent
  like any other value. On Linux it reads the `Umask` line of
  `/proc/self/status`, which leaves the mask alone; elsewhere, as on
  macOS, it sets the mask and puts it back at once (Go and Python under a
  lock, Node in `process.umask()`), so a file another thread creates in that
  moment could get the mask 0. A umask outside 0 to 0777 is
  `invalid_argument`.
- **Durations.** A server-side `timeout` is sent as `timeout_millis`, in whole
  milliseconds. Event waits are sent as `wait_millis`. A negative duration is
  `invalid_argument`.
- **Times.** Restore and revert times are sent in UTC with a `Z` suffix and with
  fractional seconds only when they are not zero, trailing zeros removed:
  `2026-11-15T09:30:00Z`, `2026-11-15T09:30:00.25Z`.
- **Validation first.** Arguments the SDK can check locally fail with
  `invalid_argument` before anything is sent: a restore without a destination,
  a version and a time together, a revert with neither or with a version below
  1, a name and a prefix together, a prefix replica
  without a directory, an empty backup source.

## Errors

Every failure is one error type with four fields: `code`, `message`,
`status`, and `details`. Go has `*litebackup.Error`, TypeScript has
`LitebackupError`, and Python has `litebackup.LitebackupError`.

Decoding a non-200 answer:

1. A JSON object with a string `code` keeps that code verbatim, including codes
   this SDK has never heard of. `message` is its `message`.
2. Every other field of that object goes into `details` under its wire name,
   unchanged. `backup-rules/start` answers `rule_exists` with the saved rule in
   `details.rule`.
3. Anything else, such as a proxy's HTML or an empty body, becomes
   `http_<status>` with the trimmed body text as the message. A JSON object
   without `code` keeps its `message`, if any.

`status` is the HTTP status, or 0 for errors the SDK raised itself. These are
the codes the SDK raises:

| Code | When |
| --- | --- |
| `invalid_argument` | an argument failed local validation |
| `binary_not_found` | no litebackup binary was found to start the daemon or a foreground backup, or the explicit override names no file |
| `launch_failed` | `litebackup launch` ran and failed |
| `drain_required` | `litebackup launch` refused a credential other than the one the client took its write locks with; a shutdown with the current credential releases them |
| `connection_failed` | the socket refused the connection or closed it mid-call |
| `daemon_unsupported` | the daemon is older than this SDK's minimum daemon version |
| `cancelled` | the caller cancelled the wait; the daemon may still finish the operation |
| `closed` | the client was closed |
| `invalid_response` | the daemon answered 200 with a body the SDK cannot decode |
| `lease_cleanup_failed` | a pool could not close a leased database or release it; a `LeaseCleanupError` with its `step` |

The daemon's own `sdk_unsupported` (426: this SDK is older than the daemon
accepts) and `client_unsupported` (426: the server needs a newer daemon) are
passed through unchanged. Each version code names the side that is too old:
the daemon (`daemon_unsupported`), the SDK (`sdk_unsupported`), or the
client as the server sees it (`client_unsupported`).

## Version negotiation

Every call sends `Litebackup-SDK-Version: <language>/<version>`, such as
`go/1.1.0`, `typescript/1.1.0`, or `python/1.1.0`. Every answer carries
`Litebackup-Client-Version`, the daemon's version, which the client records.

Connecting sends `GET /v1/status/show?limit=1`. When the answer's
`Litebackup-Client-Version` is missing or older than the SDK's minimum
daemon version (1.1.0), the connect fails with `daemon_unsupported`. The
message names both versions and how to replace the daemon: `litebackup drain
--session <session>` stops it after a final sync, and the next call starts the
new binary. A service that `litebackup install service` set up is restarted
through systemd or launchd instead. Version comparison is numeric per dotted
part and ignores any `-suffix`.

## Timeouts and cancellation

- A call with a server-side `timeout` gets a client-side deadline of that
  timeout plus 30 seconds, so the daemon's own `timeout` answer arrives first.
  An event long-poll's deadline is its wait plus 30 seconds.
- A call without a timeout has no client-side deadline. It ends when the
  daemon answers or the caller cancels.
- Cancellation (a Go context or a TypeScript `AbortSignal`) stops only the
  local wait. The error is `cancelled`, and its message says the daemon may
  still finish the operation: an acquire may still take the lock, and a
  release may still release it. In async Python, cancelling the task raises
  `asyncio.CancelledError` unchanged, as asyncio code expects; the sync Python
  client has no cancellation, so the lifecycle fixtures with `cancel` run
  only against the async client.
- When a client-side deadline passes before the daemon answers, the error is
  `timeout` with status 0, and its message says the daemon may still finish.

## Retries

Only readonly calls are retried, and only by watchers: `events.watch` and
failovers retry a failed event long-poll with backoff from 250 ms up to 10 s,
and stop on `sdk_unsupported`. A watch also ends on `closed`, `cancelled`,
`invalid_argument`, and `cursor_expired` (410: the cursor is older than the
oldest event the daemon keeps), raising that error without retrying the
cursor. A failover never ends on `cursor_expired`: it reports the error, takes
a fresh cursor at the end of the log (`after: -1`), then reads its database's
role and reports a role change it missed, and follows from the fresh cursor.
The cursor is read before the role and kept while a failed role read is
retried with the same backoff, so no event after it is lost. Acquire,
release, start, stop, restore, revert, shutdown, sync, offload, and launch are
never retried. Concurrent launches need no retry: `litebackup launch` takes the
session's launch lock, so one daemon starts.

## Failover roles

A failover's role is `origin`, `replica`, or `unknown`. The daemon reports
only the first two; the SDKs set `unknown` themselves (Go `RoleUnknown`,
TypeScript `FailoverRole`, Python `"unknown"`), and `IsOrigin()`, `isOrigin`,
and `is_origin` are false while it holds. A `refused` event newer than the
last role read, and a role read whose state is `conflicted`, make it
`unknown`: the daemon refused this machine's file, and the failover stays conflicted
there until it is started again with `discard_local` or stopped.

- The role callback (`OnRoleChange`, `on_role_change`, the TypeScript
  `origin`, `replica`, and `unknown` listeners) first hears the role the
  start answered, once, on the follower: TypeScript on the next turn of the
  event loop, Go and sync Python possibly before the start returns. Then it
  hears every change.
- A failover long-poll waits 10 s. The role is confirmed whenever the daemon
  answers a long-poll or a role read. Every follower read has a client-side
  deadline 15 s after the last confirmation (after the read's start once the
  role is unknown); one it ends is `timeout` with status 0.
- A read the daemon did not answer, any error with status 0
  (`connection_failed`, `timeout`, `daemon_unsupported`), makes the role
  `unknown` at once. A failed read the daemon answered keeps the role until
  15 s have passed after the last confirmation, and the backoff wait never
  runs past that; then the role is `unknown`. 15 s is the default client
  heartbeat interval, a quarter of the default 60 s session lease another
  machine waits out before it takes over. `sdk_unsupported` and `closed`
  end following and make the role `unknown` too.
- Once the role is `unknown`, the follower reads the log's end
  (`after: -1`), then the database's role, then the log's end again and,
  without waiting, the events up to that second end, however many pages,
  ignoring later ones. When one of them changed the database's role, the role
  read may predate it, so the follower reads the role again and looks again,
  until a look finds no role change; a look whose cursor expired starts over
  from a fresh log end. Then it reports the role. It keeps its cursor: the next long-poll hands
  over refusals made meanwhile, reported without changing the role, and
  skips `role_changed` events up to the end it looked at, because the role
  read holds them. A failover whose
  cursor expired reads its role the same way.
- Stopping a failover, or closing its client, makes its role `unknown`
  without a role callback, including when a closing client ends a read
  with `closed` before the follower stops.

## Close

Closing a client releases its connections and stops its event watches and
failover followers. It never shuts the daemon down, never stops a failover on
the daemon, and never releases an acquired database. Calls after close fail
with `closed`.

## Binary resolution

When nothing listens on the session socket, the SDK starts the daemon with
`litebackup launch --session <session>`, using the first of:

1. the explicit override: the client option (`BinaryPath`, `binaryPath`,
   `binary_path`), then `LITEBACKUP_PATH`;
2. the bundled binary: the npm platform package or the wheel's
   `litebackup/_bin/litebackup` (Go bundles none);
3. the installed command: `litebackup` on `PATH`, then `~/.local/bin/litebackup`.

When none exists, the error is `binary_not_found` with the install command.
An explicit override that names no file is `binary_not_found` too, naming the
path; it never falls back to a bundled or installed binary. One that exists
but cannot run, such as a script whose interpreter is missing, or one behind a
directory this user may not enter, fails as `launch_failed`; for a missing
interpreter the message names the script's first line, not a missing file.
An explicit socket path never launches anything. Nothing is downloaded at
runtime.

## Credentials

A machine has one credential: `LITEBACKUP_API_KEY`, or the client option
(`APIKey`, `apiKey`, `api_key`), which overrides it. An empty option is no
credential in TypeScript and Python, whatever the variable holds; Go reads the
variable when `APIKey` is empty. Its value is an API key
or a join token, then `@` and the server's address
(`litebackup_join_...@your-instance.litebackup.cloud`). The SDK tells the two
apart by prefix, as the daemon does: a join token starts with
`litebackup_join_`, an API key with `litebackup_`. An invite token
(`litebackup_invite_`), a value with neither prefix, or one without the
address (a host, or an `http://` or `https://` URL of one) is
`invalid_argument` before anything runs, and the message never
repeats the value. Spaces around it are ignored, and an empty value is none.

The SDK passes the credential to `litebackup launch` and to foreground
backups as `LITEBACKUP_API_KEY`. The daemon decides when it starts:

- with no credential, it uses the key it saved for the session;
- with the credential it saved (the same address, and the same API key or
  the join token its key came from), it uses the saved key and sends
  nothing;
- with any other credential, it exchanges a join token for a new key of the
  machine, or takes an API key as it is, and saves it in place of the old
  one, unless the client holds a write lock: then the launch refuses it with
  `drain_required` before it sends or saves anything, and the SDK raises
  that code (`CodeDrainRequired`, `ErrorCode.drainRequired`,
  `ErrorCode.DRAIN_REQUIRED`) with the launch command's message. A shutdown
  with the current credential releases the locks first.

A daemon that is already running keeps its key until it restarts. A
foreground backup uses another credential for its job only and saves
nothing.

## Pagination and events

- Database pages pass the previous page's `next` as `after` and end when
  `next` is absent or empty.
- Version pages pass `next` as `after` and end when `next` is absent or 0,
  when it does not move past `after`, when a page is empty, or when the
  requested limit is reached. A limit of 0 yields nothing and sends nothing.
- A version the primary bucket holds carries `archived`, when the server
  recorded its object. A version synced between archive passes has no
  `archived`: it restores from the server's disk only until history pruning
  removes it.
- Event watches send `after` (omitted when 0) and `wait_millis` (30 s unless
  set). A watch from `after: -1` starts at the end of the log: the first
  answer has no events and its `next` is the newest ID. The cursor moves to the
  largest of the event IDs seen and the page's `next`. A watch ends when the
  caller stops iterating or cancels.
- A cursor older than the oldest event the daemon keeps is answered with 410
  `cursor_expired`. A watch ends with that error, without retrying, and the
  caller chooses a new cursor: `after: -1` for the log's end. A failover
  resumes at the log's end and re-reads its role instead (see Retries).

## Pools and leases

A pool keeps databases open between calls. A lifecycle fixture creates a pool,
runs one lease, then closes the pool:
`acquire → open → work → sync (with durability) → close → release`.

```text
create(path)            when path is given and nothing is there: an empty file, and its directory
acquire(name, path, acquire options)    the first lease of the name only
open(path)              adapter: open the existing file, never create one
work(connection)        the caller's code
sync(name)              with durability, after work that succeeded; cloud then reads status until the bucket holds it
close(connection)       adapter: roll back an unfinished transaction, then close
release(name, durability, offload, timeout_millis: what close left of the cleanup timeout)
```

`close` and `release` run when the lease ends: `idle_release` runs out (a
minute by default), `release(name)`, or closing the pool. A later call on a
lease first reads the database's status and fails with `lock_lost` when this
machine no longer holds it. A fixture's `expect.raised` are the lease call's errors and
`expect.close_raised` the pool close's.

- `require_existing` defaults to false: when `path` is given and no file is
  there, the first lease creates an empty file (and missing directories)
  before acquiring. The daemon treats an empty file as no copy: it becomes a
  new database when the server has none and is replaced by the server's head
  otherwise. With it, a database neither the server nor this machine has
  fails with `not_found`.
- `durability` is `server` or `cloud`; any other value is `invalid_argument`
  before anything is created. With it, a call that succeeded syncs and
  returns once the server, or the bucket, holds the version. The release that
  ends the lease waits for it too, `server` when omitted.
- `offload` defaults to false, and the release requests it only when the last
  call succeeded. A failed or cancelled call keeps the lease and its local
  copy. A call counts as cancelled when the caller's signal, context, or task
  was cancelled by the time the work returns, even if the work returned
  normally; the lease then raises `cancelled` (in async Python, the
  `CancelledError`).
- `cleanup_timeout` is finite, 10 minutes by default, and set on the pool.
  Close and release share one deadline of that length, which starts when the
  release does and is unaffected by the caller's cancellation. The release's
  `timeout_millis` is what close left, rounded to milliseconds and at least 1.
- A lost lock is found by the next call's status read: that call raises
  `lock_lost` without running its work, and the call after it acquires again.
- Built-in adapters roll back unfinished transactions and never commit. Work
  that wants its changes kept commits them.

Failures:

| What fails | Calls made | Raised by the lease | Raised by closing the pool |
| --- | --- | --- | --- |
| acquire | acquire | the acquire error | nothing |
| open | acquire, open, release without offload | the open error, grouped with the release's `lease_cleanup_failed` if the release failed too | the release again, if it failed |
| work | acquire, open, work; close, release without offload | the work error | a cleanup failure, if there was one |
| close | acquire, open, work, close; **no release** | nothing | `lease_cleanup_failed` for the close |
| release | acquire, open, work, close, release | nothing | `lease_cleanup_failed` for the release |

A close failure never releases or offloads: a connection may still be open, and
releasing would let another machine write while this one still can. The local
file is left as it is, still acquired. The error's details are
`{step: "close", name, path, acquired: true}`, and its message gives the
recovery: close every connection, then call `release`. A release failure's
details are `{step: "release", name, path, durability}`. Each
`LeaseCleanupError` carries the underlying error as its cause.

When more than one error is raised, the first is the one that ended the work
(the open or work error) and the rest follow in step order: Go returns
`errors.Join(...)`, TypeScript throws an `AggregateError`, and Python raises an
`ExceptionGroup`. A call with one error raises that error alone.

A fixture may carry `skip: {language: reason}` where a language's conventions
differ: Go's zero values mean defaults, so a Go `Limit` or `CleanupTimeout` of
0 cannot mean zero.
