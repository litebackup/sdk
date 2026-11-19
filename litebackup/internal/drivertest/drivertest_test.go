// Package drivertest checks sqlitedsn and SQLAdapter against the real
// modernc.org/sqlite and github.com/mattn/go-sqlite3 drivers. It is its own
// module so the SDK module needs neither driver.
package drivertest

import (
	"context"
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
	_ "modernc.org/sqlite"

	"github.com/litebackup/sdk/litebackup"
	"github.com/litebackup/sdk/litebackup/sqlitedsn"
)

// drivers pairs each driver name with its resolver.
var drivers = []struct {
	name    string
	resolve func(string) (string, error)
}{
	{"sqlite", sqlitedsn.Modernc},
	{"sqlite3", sqlitedsn.Mattn},
}

// TestResolvedPathIsTheFileTheDriverOpens opens each DSN with the real driver,
// writes a table, and checks the table is in the file the resolver names.
func TestResolvedPathIsTheFileTheDriverOpens(t *testing.T) {
	for _, driver := range drivers {
		t.Run(driver.name, func(t *testing.T) {
			directory, err := filepath.EvalSymlinks(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			t.Chdir(directory)
			if err := os.MkdirAll(filepath.Join(directory, "data dir"), 0o755); err != nil {
				t.Fatal(err)
			}
			cases := []struct{ dsn, want string }{
				{"plain.sqlite", "plain.sqlite"},
				{"./data dir/../relative.sqlite", "relative.sqlite"},
				{filepath.Join(directory, "absolute.sqlite"), "absolute.sqlite"},
				{"query.sqlite?_busy_timeout=5000", "query.sqlite"},
				{"file:uri.sqlite", "uri.sqlite"},
				{"file:uri-query.sqlite?cache=shared&_busy_timeout=100", "uri-query.sqlite"},
				{"file:data%20dir/escaped%23.sqlite?mode=rwc", "data dir/escaped#.sqlite"},
				{"file://" + filepath.Join(directory, "authority.sqlite"), "authority.sqlite"},
				{"file://localhost" + filepath.Join(directory, "localhost.sqlite"), "localhost.sqlite"},
				{"file:fragment.sqlite#ignored", "fragment.sqlite"},
			}
			for _, testCase := range cases {
				resolved, err := driver.resolve(testCase.dsn)
				if err != nil {
					t.Fatalf("%s: %v", testCase.dsn, err)
				}
				want := filepath.Join(directory, testCase.want)
				if resolved != want {
					t.Fatalf("%s resolved to %s, want %s", testCase.dsn, resolved, want)
				}
				db, err := sql.Open(driver.name, testCase.dsn)
				if err != nil {
					t.Fatal(err)
				}
				if _, err := db.Exec("CREATE TABLE marker (id INTEGER)"); err != nil {
					t.Fatalf("%s: %v", testCase.dsn, err)
				}
				if err := db.Close(); err != nil {
					t.Fatal(err)
				}
				check, err := sql.Open(driver.name, resolved)
				if err != nil {
					t.Fatal(err)
				}
				var tables int
				if err := check.QueryRow("SELECT count(*) FROM sqlite_schema WHERE name = 'marker'").Scan(&tables); err != nil || tables != 1 {
					t.Fatalf("%s: the driver did not write %s (%d tables, %v)", testCase.dsn, resolved, tables, err)
				}
				if err := check.Close(); err != nil {
					t.Fatal(err)
				}
			}
		})
	}
}

// TestInMemoryAndTemporaryDatabasesAreRefused refuses every DSN that opens no file.
func TestInMemoryAndTemporaryDatabasesAreRefused(t *testing.T) {
	for _, driver := range drivers {
		for _, dsn := range []string{"", ":memory:", "file::memory:", "file::memory:?cache=shared", "file:named?mode=memory&cache=shared", "file:", "file:x.sqlite?vfs=memdb", "x.sqlite?vfs=memdb"} {
			if _, err := driver.resolve(dsn); !errors.Is(err, sqlitedsn.ErrNoFile) {
				t.Fatalf("%s %q: %v, want ErrNoFile", driver.name, dsn, err)
			}
		}
		if _, err := driver.resolve("file://example.com/x.sqlite"); err == nil {
			t.Fatalf("%s accepted a remote authority", driver.name)
		}
	}
}

// TestSQLAdapterRefusesToCloseAroundAnOpenTransaction fails Close while a
// transaction holds a connection, and rolls it back once it is closed.
func TestSQLAdapterRefusesToCloseAroundAnOpenTransaction(t *testing.T) {
	for _, driver := range drivers {
		t.Run(driver.name, func(t *testing.T) {
			ctx := context.Background()
			path := filepath.Join(t.TempDir(), "scope.sqlite")
			adapter := litebackup.SQLAdapter(driver.name)
			db, err := adapter.Open(ctx, path)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := db.Exec("CREATE TABLE steps (id INTEGER)"); err != nil {
				t.Fatal(err)
			}
			transaction, err := db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			if _, err := transaction.Exec("INSERT INTO steps VALUES (1)"); err != nil {
				t.Fatal(err)
			}
			if err := adapter.Close(ctx, db); err == nil {
				t.Fatal("closed a pool with a transaction in progress")
			}
			if err := transaction.Rollback(); err != nil {
				t.Fatal(err)
			}
			db, err = adapter.Open(ctx, path)
			if err != nil {
				t.Fatal(err)
			}
			var rows int
			if err := db.QueryRow("SELECT count(*) FROM steps").Scan(&rows); err != nil || rows != 0 {
				t.Fatalf("%d rows, %v", rows, err)
			}
			if err := adapter.Close(ctx, db); err != nil {
				t.Fatal(err)
			}
			if _, err := adapter.Open(ctx, path+"?mode=ro"); !litebackup.IsCode(err, litebackup.CodeInvalidArgument) {
				t.Fatalf("a path with '?': %v", err)
			}
		})
	}
}
