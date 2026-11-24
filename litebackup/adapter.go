package litebackup

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io/fs"
	"net/url"
	"os"
	"strings"
)

// Adapter opens and closes the handle a Pool gives to its calls. Open must
// open an existing file and never create one: a missing file is an offloaded
// copy the pool brings back, never an empty new database. Close must roll
// back a transaction a call left unfinished and never commit it, and must
// report an error when a connection may still be open: the pool releases a
// leased database only after Close succeeds.
type Adapter[DB any] interface {
	Open(ctx context.Context, path string) (DB, error)
	Close(ctx context.Context, db DB) error
}

// SQLAdapterArgs configures SQLAdapter.
type SQLAdapterArgs struct {
	// DSN builds the data source name from the database's path, for driver
	// options such as busy_timeout or foreign_keys. It must return a file: URI,
	// which the adapter adds mode=rw to, replacing any mode it sets, so a
	// missing file is never created. The file: URI of the bare path when nil.
	DSN func(path string) string
}

// SQLAdapter opens a pool's databases with database/sql and the named driver,
// such as "sqlite" (modernc.org/sqlite) or "sqlite3"
// (github.com/mattn/go-sqlite3).
//
// Open refuses a missing file with not_found and opens the existing one in
// mode=rw. Close closes the *sql.DB. Commit what the work should keep:
// closing a SQLite connection rolls back its unfinished transaction. A
// connection still in use, such as a transaction the work never ended, makes
// Close fail, so a leased database is not released while it may still be
// written.
func SQLAdapter(driverName string, args *SQLAdapterArgs) Adapter[*sql.DB] {
	adapter := sqlAdapter{driverName: driverName}
	if args != nil {
		adapter.dsn = args.DSN
	}
	return adapter
}

type sqlAdapter struct {
	driverName string
	dsn        func(path string) string
}

// Open opens and pings an existing database.
func (a sqlAdapter) Open(ctx context.Context, path string) (*sql.DB, error) {
	if _, err := os.Stat(path); errors.Is(err, fs.ErrNotExist) {
		return nil, &Error{
			Code:    CodeNotFound,
			Message: path + " does not exist; the adapter opens existing databases only",
			Err:     err,
		}
	}
	dsn, err := existingOnlyDSN(path, a.dsn)
	if err != nil {
		return nil, err
	}
	db, err := sql.Open(a.driverName, dsn)
	if err != nil {
		return nil, err
	}
	if err := db.PingContext(ctx); err != nil {
		return nil, errors.Join(err, db.Close())
	}
	return db, nil
}

// existingOnlyDSN is the DSN build returns for path, or the file: URI of path
// when build is nil, with mode=rw in its query when it is a file: URI.
func existingOnlyDSN(path string, build func(path string) string) (string, error) {
	if build == nil {
		return (&url.URL{Scheme: "file", Path: path, RawQuery: "mode=rw"}).String(), nil
	}
	dsn := build(path)
	if !strings.HasPrefix(dsn, "file:") {
		// Only a file: URI can say mode=rw, so a missing file is never created.
		return "", invalidArgument(fmt.Sprintf(
			"the DSN %q must be a file: URI, so the adapter can open the database without creating it",
			dsn,
		))
	}
	location, query, _ := strings.Cut(dsn, "?")
	parameters, err := url.ParseQuery(query)
	if err != nil {
		return "", invalidArgument(fmt.Sprintf("the DSN %q has a query that does not parse: %v", dsn, err))
	}
	parameters.Set("mode", "rw")
	return location + "?" + parameters.Encode(), nil
}

// Close closes the pool and fails when a connection was still in use or
// closing outlasts ctx. Closing then goes on in the background, and a leased
// database stays acquired.
func (a sqlAdapter) Close(ctx context.Context, db *sql.DB) error {
	busyConnections := db.Stats().InUse
	closed := make(chan error, 1)
	go func() { closed <- db.Close() }()
	var closeErr error
	select {
	case closeErr = <-closed:
	case <-ctx.Done():
		return fmt.Errorf(
			"closing the database did not finish before the cleanup timeout: %w",
			ctx.Err(),
		)
	}
	if busyConnections > 0 {
		return errors.Join(
			fmt.Errorf(
				"%d connection(s) were still in use, such as a transaction that was never committed or rolled back",
				busyConnections,
			),
			closeErr,
		)
	}
	return closeErr
}

// DatabasePath returns the main database file db opened, read through any
// driver with PRAGMA database_list, so an app that keeps only its *sql.DB can
// register it: StartBackup(ctx, path, nil). An in-memory or temporary
// database has no file and is refused with invalid_argument.
func DatabasePath(ctx context.Context, db *sql.DB) (string, error) {
	var path string
	err := db.QueryRowContext(ctx, "SELECT file FROM pragma_database_list WHERE name = 'main'").
		Scan(&path)
	if err != nil {
		return "", err
	}
	if path == "" {
		return "", invalidArgument("the database is in memory or temporary and has no file to back up")
	}
	return path, nil
}
