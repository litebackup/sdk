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

// TestResolvedPathIsDriverFile opens each DSN with the real driver,
// writes a table, and checks the table is in the file the resolver names.
func TestResolvedPathIsDriverFile(t *testing.T) {
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
				{
					"file://localhost" + filepath.Join(directory, "localhost.sqlite"),
					"localhost.sqlite",
				},
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
				if _, err := db.Exec("CREATE TABLE written (id INTEGER)"); err != nil {
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
				if err := check.QueryRow("SELECT count(*) FROM sqlite_schema WHERE name = 'written'").Scan(&tables); err != nil ||
					tables != 1 {
					t.Fatalf(
						"%s: the driver did not write %s (%d tables, %v)",
						testCase.dsn,
						resolved,
						tables,
						err,
					)
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

// TestSQLAdapterCloseRefusesOpenTransaction fails Close while a
// transaction holds a connection, and rolls it back once it is closed.
func TestSQLAdapterCloseRefusesOpenTransaction(t *testing.T) {
	for _, driver := range drivers {
		t.Run(driver.name, func(t *testing.T) {
			ctx := context.Background()
			path := filepath.Join(t.TempDir(), "lease.sqlite")
			if err := os.WriteFile(path, nil, 0o644); err != nil {
				t.Fatal(err)
			}
			adapter := litebackup.SQLAdapter(driver.name, nil)
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
			var stepCount int
			if err := db.QueryRow("SELECT count(*) FROM steps").Scan(&stepCount); err != nil ||
				stepCount != 0 {
				t.Fatalf("%d steps, %v", stepCount, err)
			}
			if err := adapter.Close(ctx, db); err != nil {
				t.Fatal(err)
			}
		})
	}
}

// TestSQLAdapterNeverCreates refuses a missing file with not_found and leaves
// it missing, with the default DSN and with one that asks for mode=rwc, and
// opens an existing file at a path that needs escaping in a URI.
func TestSQLAdapterNeverCreates(t *testing.T) {
	for _, driver := range drivers {
		t.Run(driver.name, func(t *testing.T) {
			ctx := context.Background()
			directory := filepath.Join(t.TempDir(), "data dir")
			if err := os.MkdirAll(directory, 0o755); err != nil {
				t.Fatal(err)
			}
			missing := filepath.Join(directory, "missing.sqlite")
			for _, args := range []*litebackup.SQLAdapterArgs{
				nil,
				{DSN: func(path string) string { return "file:" + path + "?mode=rwc" }},
			} {
				adapter := litebackup.SQLAdapter(driver.name, args)
				if _, err := adapter.Open(ctx, missing); !litebackup.IsCode(err, litebackup.CodeNotFound) {
					t.Fatalf("opening a missing file: %v, want not_found", err)
				}
				if _, err := os.Stat(missing); !errors.Is(err, os.ErrNotExist) {
					t.Fatalf("the adapter created %s: %v", missing, err)
				}
			}
			existing := filepath.Join(directory, "escaped#?.sqlite")
			if err := os.WriteFile(existing, nil, 0o644); err != nil {
				t.Fatal(err)
			}
			adapter := litebackup.SQLAdapter(driver.name, nil)
			db, err := adapter.Open(ctx, existing)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := db.Exec("CREATE TABLE notes (body TEXT)"); err != nil {
				t.Fatal(err)
			}
			path, err := litebackup.DatabasePath(ctx, db)
			if err != nil {
				t.Fatal(err)
			}
			if path != existing {
				t.Fatalf("the database opened %s, want %s", path, existing)
			}
			if err := adapter.Close(ctx, db); err != nil {
				t.Fatal(err)
			}
		})
	}
}

// TestSQLAdapterDSNOptions opens with the driver options a DSN function sets.
func TestSQLAdapterDSNOptions(t *testing.T) {
	for _, driver := range []struct{ name, option string }{
		{"sqlite", "_pragma=busy_timeout(4321)"},
		{"sqlite3", "_busy_timeout=4321"},
	} {
		t.Run(driver.name, func(t *testing.T) {
			ctx := context.Background()
			path := filepath.Join(t.TempDir(), "options.sqlite")
			if err := os.WriteFile(path, nil, 0o644); err != nil {
				t.Fatal(err)
			}
			adapter := litebackup.SQLAdapter(driver.name, &litebackup.SQLAdapterArgs{
				DSN: func(path string) string { return "file:" + path + "?" + driver.option },
			})
			db, err := adapter.Open(ctx, path)
			if err != nil {
				t.Fatal(err)
			}
			defer adapter.Close(ctx, db)
			var busyTimeout int
			if err := db.QueryRow("PRAGMA busy_timeout").Scan(&busyTimeout); err != nil {
				t.Fatal(err)
			}
			if busyTimeout != 4321 {
				t.Fatalf("busy_timeout %d, want the DSN's 4321", busyTimeout)
			}
		})
	}
}

// TestDatabasePath reads the file a relative or URI DSN opened, and refuses
// an in-memory database.
func TestDatabasePath(t *testing.T) {
	for _, driver := range drivers {
		t.Run(driver.name, func(t *testing.T) {
			ctx := context.Background()
			directory, err := filepath.EvalSymlinks(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			t.Chdir(directory)
			for _, dsn := range []string{"relative.sqlite", "file:uri.sqlite?cache=shared"} {
				db, err := sql.Open(driver.name, dsn)
				if err != nil {
					t.Fatal(err)
				}
				path, err := litebackup.DatabasePath(ctx, db)
				if err != nil {
					t.Fatal(err)
				}
				resolved, err := driver.resolve(dsn)
				if err != nil {
					t.Fatal(err)
				}
				if path != resolved {
					t.Fatalf("%q: DatabasePath %s, want %s", dsn, path, resolved)
				}
				if err := db.Close(); err != nil {
					t.Fatal(err)
				}
			}
			memory, err := sql.Open(driver.name, ":memory:")
			if err != nil {
				t.Fatal(err)
			}
			defer memory.Close()
			if _, err := litebackup.DatabasePath(ctx, memory); !litebackup.IsCode(
				err,
				litebackup.CodeInvalidArgument,
			) {
				t.Fatalf("an in-memory database: %v, want invalid_argument", err)
			}
		})
	}
}
