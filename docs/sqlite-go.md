# How to back up SQLite databases in Go

Install a [server](litebackup-server.md), copy a join token from **Machines → Add machine**,
and install the client command where the Go app runs:

```sh
curl -fsSL https://litebackup.com/cli.sh | bash
go get github.com/litebackup/sdk/litebackup
export LITEBACKUP_TOKEN='JOIN_TOKEN@your-instance.litebackup.cloud'
```

The SDK starts the local client when needed. It uses `ConnectArgs.BinaryPath`,
then `LITEBACKUP_PATH`, then `litebackup` on `PATH`, then
`~/.local/bin/litebackup`, where the installer puts it for a user other than
root. It refuses a client older than 1.0.0 with `incompatible_version`. Your
application can use any SQLite driver.

## Continuous backup

This complete program registers an existing file and stays up. Registration
does not wait for server or cloud durability.

```go
package main

import (
    "context"
    "log"
    "os"
    "os/signal"
    "syscall"

    "github.com/litebackup/sdk/litebackup"
)

func main() {
    ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
    defer cancel()
    client, err := litebackup.Connect(ctx, nil)
    if err != nil {
        log.Fatal(err)
    }
    defer client.Close()
    _, err = client.StartBackup(ctx, "/var/lib/app/main.sqlite", nil)
    if err != nil {
        log.Fatal(err)
    }
    // Start your application here. The daemon syncs in the background.
    <-ctx.Done()
}
```

`Close()` closes the SDK connection; the daemon and backup rule remain.
Use a [service install](litebackup-client.md) for daemon restart and reboot handling.

For tenants, change the registration to a glob:

```go
_, err = client.StartBackup(ctx, "/var/lib/app/tenants/*.sqlite", &litebackup.StartBackupArgs{
    Prefix: "tenants/",
})
```

## One-shot backup

Run the foreground backup with a context or timeout for scheduled jobs.

```go
package main

import (
    "context"
    "errors"
    "fmt"
    "log"
    "os"
    "time"

    "github.com/litebackup/sdk/litebackup"
)

func run() error {
    if len(os.Args) < 2 {
        return errors.New("provide at least one SQLite path")
    }
    ctx := context.Background()
    for _, path := range os.Args[1:] {
        wait, cancel := context.WithTimeout(ctx, 30*time.Minute)
        err := litebackup.Backup(wait, path, &litebackup.BackupArgs{Prefix: "scheduled/", Wait: litebackup.DurabilityCloud})
        cancel()
        if err != nil {
            return fmt.Errorf("backup %s: %w", path, err)
        }
    }
    return nil
}

func main() {
    if err := run(); err != nil {
        log.Fatal(err)
    }
}
```

```sh
go run . /var/lib/app/exports/main.sqlite /var/lib/app/exports/audit.sqlite
```

See [one-shot jobs](sqlite-backup-script.md) for naming and per-database consistency,
and [Cron](sqlite-cron-backup.md) for preventing overlapping runs.

## Register the database the app opens

`sqlitedsn` finds the file a `database/sql` DSN opens, with each driver's own
parsing: relative paths, `file:` URIs, escaping, and query parameters. It
refuses in-memory and temporary databases and imports no driver.

```go
dsn := "file:data/app.sqlite?_pragma=journal_mode(WAL)"
db, err := sql.Open("sqlite", dsn) // modernc.org/sqlite; use sqlitedsn.Mattn for "sqlite3"
if err != nil {
    return err
}
path, err := sqlitedsn.Modernc(dsn)
if err != nil {
    return err
}
_, err = client.StartBackup(ctx, path, nil)
```

Registration is idempotent: starting the same path with the same settings
returns the saved rule, so every start may call it. Other settings for a saved
path fail with `exists`, and `Details["rule"]` is the saved rule. A complete
program is in [examples/go-app](../examples/go-app/main.go).

## Run an agent against its own database

`Managed` acquires the database, opens it with the adapter, runs your
function, closes it, and releases it. It is a package function because Go
methods cannot take type parameters.

```go
err := litebackup.Managed(ctx, client, "agents/42.sqlite", &litebackup.ManagedArgs[*sql.DB]{
    To:         "/var/lib/app/agents/42.sqlite",
    Durability: litebackup.DurabilityCloud,
    Offload:    true,
    Adapter:    litebackup.SQLAdapter("sqlite"),
}, func(ctx context.Context, db *sql.DB) error {
    _, err := db.ExecContext(ctx, "INSERT INTO steps (body) VALUES (?)", "tests passed")
    return err
})
```

Closing and releasing use `context.WithoutCancel(ctx)` and their own
`CleanupTimeout` (10 minutes by default), so they finish after `ctx` is
cancelled. `SQLAdapter` closes the pool and fails if a connection is still in
use, such as a transaction that was never committed or rolled back; then the
database is not released and stays acquired here. Failures are returned with
`errors.Join`, the work's error first. A `scope_cleanup_failed` error's `Err`
is the close or release error. The complete program is in
[examples/go-app/agent](../examples/go-app/agent/main.go). See
[agents](sqlite-agent-state.md) for concurrency and offloading.

## Iterate pages

`AllDatabases`, `AllVersions`, and `WatchEvents` return `iter.Seq2[T, error]`;
the page calls (`DatabasesPage`, `VersionsPage`, `Events`) remain.

```go
for version, err := range client.AllVersions(ctx, "main.sqlite", nil) {
    if err != nil {
        return err
    }
    fmt.Println(version.Version, version.Time)
}
```

## Replica and restore

With a connected `client`, a joined machine, and an existing remote backup:

```go
_, err = client.Replicate(ctx, "main.sqlite", &litebackup.ReplicateArgs{
    To: "/var/lib/app/reporting.sqlite",
})
if err != nil {
    return err
}
_, err = client.Restore(ctx, "main.sqlite", &litebackup.RestoreArgs{
    To: "/tmp/main-restored.sqlite",
})
return err
```

Replicate waits for the first local copy; restore requires a new destination.
The [API reference](sdk-reference.md) maps every Go method to its TypeScript and Python equivalents.
