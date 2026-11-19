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
| `lifecycle/` | a managed-scope scenario: which steps fail, the daemon calls, and the error raised | every SDK |
| `binary/` | platforms and their npm package, release file, and wheel platform | TypeScript, Python, wheel builds |

Arguments in fixtures are snake_case and language-neutral:

- `{cwd}` in a string is the working directory the runner sets before the
  call. Relative paths resolve against it.
- `{"$path": "data/main.sqlite"}` is the language's path type: a `pathlib.Path`
  in Python, a `file:` URL in TypeScript (made absolute against `{cwd}`), and a
  plain string in Go.
- Durations are strings such as `"1500ms"`, `"90s"`, or `"2m"`. Times are RFC 3339
  strings. Each runner converts them to `time.Duration`/`time.Time`, a `Duration`
  string/`Date`, or `timedelta`/`datetime`.
- `offload: "released"` and `on_lost: "block"` on acquire are spelled
  `OffloadReleased: true` and `BlockOnLost: true` in Go.

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
  default (`maintenance: false`, `offload: "never"` or `false`, `on_lost:
  "conflict"`, a zero duration), is not sent. Only settings that differ from
  the default go on the wire, so the three SDKs send identical bodies.
- **Durations.** A server-side `timeout` is sent as `timeout_millis`, in whole
  milliseconds. Event waits are sent as `wait_millis`. A negative duration is
  `invalid_argument`.
- **Times.** Restore times are sent in UTC with a `Z` suffix and with
  fractional seconds only when they are not zero, trailing zeros removed:
  `2026-11-15T09:30:00Z`, `2026-11-15T09:30:00.25Z`.
- **Validation first.** Arguments the SDK can check locally fail with
  `invalid_argument` before anything is sent: a restore without a destination,
  a version and a time together, a name and a prefix together, a prefix replica
  without a directory, an empty backup source.

## Errors

Every failure is one error type with four fields: `code`, `message`,
`status`, and `details`. Go has `*litebackup.Error`, TypeScript has
`LitebackupError`, and Python has `litebackup.LitebackupError`.

Decoding a non-200 answer:

1. A JSON object with a string `code` keeps that code verbatim, including codes
   this SDK has never heard of. `message` is its `message`.
2. Every other field of that object goes into `details` under its wire name,
   unchanged. `backup-rules/start` answers `exists` with the saved rule in
   `details.rule`.
3. Anything else, such as a proxy's HTML or an empty body, becomes
   `http_<status>` with the trimmed body text as the message. A JSON object
   without `code` keeps its `message`, if any.

`status` is the HTTP status, or 0 for errors the SDK raised itself. These are
the codes the SDK raises:

| Code | When |
| --- | --- |
| `invalid_argument` | an argument failed local validation |
| `binary_not_found` | no litebackup binary was found to start the daemon |
| `launch_failed` | `litebackup launch` ran and failed |
| `connection_failed` | the socket refused the connection or closed it mid-call |
| `incompatible_version` | the daemon is older than this SDK's minimum daemon version |
| `cancelled` | the caller cancelled the wait; the daemon may still finish the operation |
| `closed` | the client was closed |
| `invalid_response` | the daemon answered 200 with a body the SDK cannot decode |
| `scope_cleanup_failed` | a managed scope could not close its database or release it |

The daemon's own `sdk_unsupported` (426: this SDK is older than the daemon
accepts) is passed through unchanged.

## Version negotiation

Every call sends `X-Litebackup-SDK-Version: <language>/<version>`, such as
`go/1.0.0`, `typescript/1.0.0`, or `python/1.0.0`. Every answer carries
`X-Litebackup-Client-Version`, the daemon's version, which the client records.

Connecting sends `GET /v1/status/show?limit=1`. When the answer's
`X-Litebackup-Client-Version` is missing or older than the SDK's minimum
daemon version (1.0.0), the connect fails with `incompatible_version`. The
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
- Cancellation (a Go context, a TypeScript `AbortSignal`, or Python task
  cancellation) stops only the local wait. The error is `cancelled`, and its
  message says the daemon may still finish the operation: an acquire may still
  take the lock, and a release may still release it.

## Retries

Only read-only calls are retried, and only by watchers: `events.watch` and
failovers retry a failed event long-poll with backoff from 250 ms up to 10 s,
and stop on `sdk_unsupported`. Acquire, release, start, stop, restore,
shutdown, sync, offload, and launch are never retried. Concurrent launches
need no retry: `litebackup launch` takes the session's launch lock, so one
daemon starts.

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
An explicit socket path never launches anything. Nothing is downloaded at
runtime.

## Pagination and events

- Database pages pass the previous page's `next` as `after` and end when
  `next` is absent or empty.
- Version pages pass `next` as `after` and end when `next` is absent or 0,
  when it does not move past `after`, when a page is empty, or when the
  requested limit is reached. A limit of 0 yields nothing and sends nothing.
- Event watches send `after` (omitted when 0) and `wait_millis` (30 s unless
  set). A watch from `after: -1` starts at the end of the log: the first
  answer has no events and its `next` is the newest ID. The cursor moves to the
  largest of the event IDs seen and the page's `next`. A watch ends when the
  caller stops iterating or cancels.

## Managed scopes

A managed scope owns a database for the length of a block:
`acquire → open → work → close → release`.

```text
acquire(name, to, acquire options)
open(path)              adapter: open a connection
work(connection)        the caller's code
close(connection)       adapter: roll back an unfinished transaction, then close
release(name, wait: durability, offload, timeout_millis: cleanup timeout)
```

- `durability` is required: `server` or `cloud`. It is the release's `wait`.
- `offload` defaults to false, and is requested only when the work succeeded.
  A failed or cancelled run keeps its local copy for the retry.
- `cleanup_timeout` is finite, 10 minutes by default. It is the release's
  `timeout_millis`, and close and release run under their own deadline of
  that length, unaffected by the caller's cancellation.
- Built-in adapters roll back unfinished transactions and never commit. Work
  that wants its changes kept commits them.

Failures:

| What fails | Calls made | Raised |
| --- | --- | --- |
| acquire | acquire | the acquire error |
| open | acquire, open, release without offload | the open error, grouped with the release's `scope_cleanup_failed` if the release failed too |
| work | all five, release without offload | the work error, grouped with a cleanup failure if there was one |
| close | acquire, open, work, close; **no release** | `scope_cleanup_failed` for the close, grouped after the work error if work failed too |
| release | all five | `scope_cleanup_failed` for the release, grouped after the work error if work failed too |

A close failure never releases or offloads: a connection may still be open, and
releasing would let another machine write while this one still can. The local
file is left as it is, still acquired. The error's details are
`{step: "close", name, path, acquired: true}`, and its message gives the
recovery: close every connection, then call `release`. A release failure's
details are `{step: "release", name, path, durability}`. Each
`scope_cleanup_failed` carries the underlying error as its cause.

When more than one error is raised, the first is the one that ended the work
(the open or work error) and the rest follow in step order: Go returns
`errors.Join(...)`, TypeScript throws an `AggregateError`, and Python raises an
`ExceptionGroup`. A scope with one error raises that error alone.
