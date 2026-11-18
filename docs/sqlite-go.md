# How to back up SQLite databases in Go

Install a [server](litebackup-server.md), copy a join token from **Machines → Add machine**,
and install the client command where the Go app runs:

```sh
curl -fsSL https://litebackup.com/cli.sh | sudo bash
go get github.com/litebackup/sdk/litebackup
export LITEBACKUP_TOKEN='JOIN_TOKEN@your-instance.litebackup.cloud'
```

The SDK starts the local client when needed. It uses `LITEBACKUP_PATH`, then
`litebackup` on `PATH`. Your application can use any SQLite driver.

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

## Acquire and release an agent database

This helper expects a connected client and a function that runs the agent
using the given path. That function must close every SQLite connection before
returning. Use a persistent directory accessible to the client.

```go
func runAgent(ctx context.Context, client *litebackup.Client, work func(string) error) error {
    path, name := "/var/lib/app/agents/42.sqlite", "agents/42.sqlite"
    if err := os.MkdirAll("/var/lib/app/agents", 0o750); err != nil {
        return err
    }
    file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
    if err != nil {
        return err
    }
    if err := file.Close(); err != nil {
        return err
    }
    _, err = client.Acquire(ctx, name, &litebackup.AcquireArgs{To: path})
    if err != nil {
        return err
    }
    workErr := work(path)
    cleanup, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
    defer cancel()
    _, releaseErr := client.Release(cleanup, name, &litebackup.ReleaseArgs{
        Wait: litebackup.DurabilityCloud, Offload: true, Timeout: 30 * time.Minute,
    })
    return errors.Join(workErr, releaseErr)
}
```

This fragment uses `context`, `os`, `time`, `errors`, and `litebackup` from
the one-shot program's imports. See [agents](sqlite-agent-state.md) for ownership,
concurrency, lost leases, and offloading.

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
The [API reference](sdk-reference.md) maps every Go method to its TypeScript equivalent.
