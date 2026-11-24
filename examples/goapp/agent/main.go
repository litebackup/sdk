// Command agent runs one agent step against the agent's own database: it
// leases the database from a pool, records the step in a transaction, and
// closes the pool, which releases the database once the server holds the
// step, so the next machine continues from here.
package main

import (
	"context"
	"database/sql"
	"errors"
	"log"
	"os"
	"path/filepath"
	"time"

	_ "modernc.org/sqlite"

	"github.com/litebackup/sdk/litebackup"
)

func main() {
	if len(os.Args) != 3 {
		log.Fatal("usage: agent AGENT-ID STEP")
	}
	agentID, step := os.Args[1], os.Args[2]
	ctx := context.Background()
	client, err := litebackup.Connect(ctx, nil)
	if err != nil {
		log.Fatal(err)
	}
	defer client.Close()
	agents := os.Getenv("AGENT_DIRECTORY")
	if agents == "" {
		agents = "/var/lib/app/agents"
	}
	pool, err := litebackup.NewPool(client, &litebackup.PoolArgs[*sql.DB]{
		Adapter: litebackup.SQLAdapter("sqlite", nil),
	})
	if err != nil {
		log.Fatal(err)
	}
	err = pool.Lease(
		ctx,
		"agents/"+agentID+".sqlite",
		&litebackup.LeaseArgs{
			Path: filepath.Join(agents, agentID+".sqlite"),
			// While another machine holds this agent, wait up to a minute for it.
			LockTimeout: time.Minute,
		},
		func(ctx context.Context, db *sql.DB) error {
			tx, err := db.BeginTx(ctx, nil)
			if err != nil {
				return err
			}
			defer tx.Rollback()
			if _, err := tx.ExecContext(ctx, "CREATE TABLE IF NOT EXISTS steps (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"); err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, "INSERT INTO steps (body) VALUES (?)", step); err != nil {
				return err
			}
			return tx.Commit()
		},
	)
	// Closing the pool releases the lease; a failed release names its step.
	if err := errors.Join(err, pool.Close(ctx)); err != nil {
		log.Fatal(err)
	}
}
