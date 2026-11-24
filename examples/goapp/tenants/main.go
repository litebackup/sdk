// Command tenants serves one SQLite database per customer from this machine.
// A backup rule backs up every tenant file continuously, and a pool keeps
// each tenant's *sql.DB open between requests, so every request after the
// first runs at local SQLite speed. Every database stays local.
//
// Usage: tenants TENANT-ID NOTE
package main

import (
	"context"
	"database/sql"
	"errors"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"syscall"
	"time"

	_ "modernc.org/sqlite"

	"github.com/litebackup/sdk/litebackup"
)

const tenantDirectory = "/var/lib/app/tenants"

var tenantIDPattern = regexp.MustCompile(`^[0-9]+$`)

// createTenant makes a tenant's file; the rule picks it up.
func createTenant(ctx context.Context, id string) error {
	if err := os.MkdirAll(tenantDirectory, 0o755); err != nil {
		return err
	}
	db, err := sql.Open("sqlite", "file:"+filepath.Join(tenantDirectory, id+".sqlite")+"?_pragma=journal_mode(WAL)")
	if err != nil {
		return err
	}
	defer db.Close()
	_, err = db.ExecContext(ctx, "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
	return err
}

// awaitTracked waits until the rule's background listing tracks a new tenant
// file, which pool.Use needs; a tenant that existed before is tracked at once.
func awaitTracked(ctx context.Context, client *litebackup.Client, name string) error {
	deadline := time.Now().Add(2 * time.Minute)
	for {
		_, err := client.Database(ctx, name)
		if !litebackup.IsCode(err, litebackup.CodeNotFound) || time.Now().After(deadline) {
			return err
		}
		time.Sleep(time.Second)
	}
}

func main() {
	if len(os.Args) != 3 || !tenantIDPattern.MatchString(os.Args[1]) {
		log.Fatal("usage: tenants TENANT-ID NOTE")
	}
	tenantID, note := os.Args[1], os.Args[2]
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	client, err := litebackup.Connect(ctx, nil)
	if err != nil {
		log.Fatal(err)
	}
	defer client.Close()
	// Idempotent: call it on every start. Add MaximumLocalDatabases only when
	// the disk cannot hold every tenant; offloaded tenants download again on
	// their next use.
	if _, err := client.StartBackup(ctx, tenantDirectory+"/*.sqlite", &litebackup.StartBackupArgs{
		Prefix: "tenants/",
	}); err != nil {
		log.Fatal(err)
	}
	if err := createTenant(ctx, tenantID); err != nil {
		log.Fatal(err)
	}
	if err := awaitTracked(ctx, client, "tenants/"+tenantID+".sqlite"); err != nil {
		log.Fatal(err)
	}
	pool, err := litebackup.NewPool(client, &litebackup.PoolArgs[*sql.DB]{
		Adapter: litebackup.SQLAdapter("sqlite", &litebackup.SQLAdapterArgs{
			DSN: func(path string) string { return "file:" + path + "?_pragma=busy_timeout(5000)" },
		}),
	})
	if err != nil {
		log.Fatal(err)
	}
	err = pool.Use(ctx, "tenants/"+tenantID+".sqlite", func(ctx context.Context, db *sql.DB) error {
		_, err := db.ExecContext(ctx, "INSERT INTO notes (body) VALUES (?)", note)
		return err
	})
	if err := errors.Join(err, pool.Close(ctx)); err != nil {
		log.Fatal(err)
	}
}
