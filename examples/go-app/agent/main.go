// Command agent runs one agent step against the agent's own database: it
// acquires the database, opens it, records the step, closes it, and releases
// it to the bucket so the next machine continues from here.
package main

import (
	"context"
	"database/sql"
	"log"
	"os"
	"path/filepath"

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
	if err := os.MkdirAll(agents, 0o755); err != nil {
		log.Fatal(err)
	}
	err = litebackup.Managed(ctx, client, "agents/"+agentID+".sqlite", &litebackup.ManagedArgs[*sql.DB]{
		To:         filepath.Join(agents, agentID+".sqlite"),
		Durability: litebackup.DurabilityCloud,
		Adapter:    litebackup.SQLAdapter("sqlite"),
	}, func(ctx context.Context, db *sql.DB) error {
		if _, err := db.ExecContext(ctx, "CREATE TABLE IF NOT EXISTS steps (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"); err != nil {
			return err
		}
		_, err := db.ExecContext(ctx, "INSERT INTO steps (body) VALUES (?)", step)
		return err
	})
	if err != nil {
		log.Fatal(err)
	}
}
