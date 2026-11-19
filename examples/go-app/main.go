// Command go-app opens its SQLite database with database/sql and registers
// it for continuous backup at startup, from the same DSN it opens.
package main

import (
	"context"
	"database/sql"
	"log"
	"os"

	_ "modernc.org/sqlite"

	"github.com/litebackup/sdk/litebackup"
	"github.com/litebackup/sdk/litebackup/sqlitedsn"
)

func main() {
	ctx := context.Background()
	dsn := os.Getenv("DATABASE_DSN")
	if dsn == "" {
		dsn = "file:data/app.sqlite?_pragma=journal_mode(WAL)"
	}
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()
	// Opening the first connection creates the file, which registration needs.
	if err := db.PingContext(ctx); err != nil {
		log.Fatal(err)
	}

	path, err := sqlitedsn.Modernc(dsn)
	if err != nil {
		log.Fatal(err)
	}
	// Registration is idempotent: every start and every replica of the app may call it.
	rule, err := litebackup.StartBackup(ctx, path, nil)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("backing up %s as %s", rule.Path, rule.Name)
	// Serve with db; Litebackup syncs changes in the background.
}
