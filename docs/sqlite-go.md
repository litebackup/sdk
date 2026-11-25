# How to back up SQLite databases in Go

Install a [server](litebackup-server.md), copy a join token from **Machines → Add machine**,
and install the client command where the Go app runs:

```sh
curl -fsSL https://litebackup.com/cli.sh | bash
go get github.com/litebackup/sdk/litebackup
export LITEBACKUP_API_KEY='JOIN_TOKEN@your-instance.litebackup.cloud'
```

The SDK starts the local client when needed. It uses `ConnectArgs.BinaryPath`,
then `LITEBACKUP_PATH`, then `litebackup` on `PATH`, then
`~/.local/bin/litebackup`, where the installer puts it for a user other than
root. It refuses a client older than 1.5.0 with `daemon_unsupported`. Your
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

func backupPaths() error {
    if len(os.Args) < 2 {
        return errors.New("provide at least one SQLite path")
    }
    ctx := context.Background()
    for _, path := range os.Args[1:] {
        wait, cancel := context.WithTimeout(ctx, 30*time.Minute)
        err := litebackup.Backup(wait, path, &litebackup.BackupArgs{Prefix: "scheduled/"})
        cancel()
        if err != nil {
            return fmt.Errorf("backup %s: %w", path, err)
        }
    }
    return nil
}

func main() {
    if err := backupPaths(); err != nil {
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

`DatabasePath` reads the file an open `*sql.DB` uses, through any driver, so an
existing app adds two lines after its `sql.Open`. It refuses in-memory and
temporary databases.

```go
db, err := sql.Open("sqlite", "file:data/app.sqlite?_pragma=journal_mode(WAL)") // existing
if err != nil {
    return err
}
path, err := litebackup.DatabasePath(ctx, db) // added
if err != nil {
    return err
}
_, err = client.StartBackup(ctx, path, nil) // added
```

An app that has only a DSN can resolve it with `sqlitedsn.Modernc` or
`sqlitedsn.Mattn`, which parse it as each driver does and import no driver.

Registration is idempotent: starting the same path with the same settings
returns the saved rule, so every start may call it. Other settings for a saved
path fail with `rule_exists`, and `Details["rule"]` is the saved rule. A complete
program is in [examples/goapp](../examples/goapp/main.go).

## Serve many databases from a pool

A `Pool` keeps databases open between calls. `Use` is for databases a backup
rule on this machine covers, such as one per tenant: the pool opens each on
first use, keeps it open, and brings an offloaded copy back first. `Lease` is
for databases that move between machines: the first call acquires the write
lock, and the pool keeps it until `IdleRelease` (a minute unused by
default), `Release`, or `Close`. `NewPool` is a package function because Go methods cannot take type
parameters.

```go
pool, err := litebackup.NewPool(client, &litebackup.PoolArgs[*sql.DB]{
    Adapter: litebackup.SQLAdapter("sqlite", &litebackup.SQLAdapterArgs{
        DSN: func(path string) string { return "file:" + path + "?_pragma=busy_timeout(5000)" },
    }),
})
if err != nil {
    return err
}
defer pool.Close(ctx)

err = pool.Use(ctx, "tenants/42.sqlite", func(ctx context.Context, db *sql.DB) error {
    _, err := db.ExecContext(ctx, "INSERT INTO notes (body) VALUES (?)", "hello")
    return err
})

err = pool.Lease(ctx, "agents/7.sqlite", &litebackup.LeaseArgs{
    Path: "/var/lib/app/agents/7.sqlite", Durability: litebackup.DurabilityServer, LockTimeout: time.Minute,
}, func(ctx context.Context, db *sql.DB) error {
    _, err := db.ExecContext(ctx, "INSERT INTO steps (body) VALUES (?)", "tests passed")
    return err
})
```

`SQLAdapter` opens existing files only, in `mode=rw`, so a copy offloaded
between two calls is brought back rather than recreated empty; `DSN` adds
driver options. Closing and releasing a lease use `context.WithoutCancel(ctx)`
and share one `CleanupTimeout` (10 minutes by default), so they finish after
`ctx` is cancelled. `SQLAdapter` fails to close while a connection is in use,
such as a transaction that was never committed or rolled back; the database
is then not released and stays acquired here. A failed release is a
`*LeaseCleanupError` whose `Step` is `LeaseCleanupStepClose` or
`LeaseCleanupStepRelease`; `IsCode(err, litebackup.CodeLeaseCleanupFailed)`
matches it. Complete programs are in
[examples/goapp/tenants](../examples/goapp/tenants/main.go) and
[examples/goapp/agent](../examples/goapp/agent/main.go). See
[agents](sqlite-agent-state.md) for leases and offloading.

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
    Path: "/var/lib/app/reporting.sqlite",
})
if err != nil {
    return err
}
_, err = client.Restore(ctx, "main.sqlite", &litebackup.RestoreArgs{
    Path: "/tmp/main-restored.sqlite",
})
return err
```

Replicate waits for the first local copy; restore requires a new destination.
The [API reference](sdk-reference.md) maps every Go method to its TypeScript and Python equivalents.
