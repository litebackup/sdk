package litebackup

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"
)

// DefaultCleanupTimeout bounds closing and releasing a managed database when
// ManagedArgs.CleanupTimeout is 0.
const DefaultCleanupTimeout = 10 * time.Minute

// ScopeAdapter opens and closes the connection Managed hands to its work.
// Close must roll back a transaction the work left unfinished and never
// commit it, and must report an error when a connection may still be open:
// Managed releases the database only after Close succeeds.
type ScopeAdapter[DB any] interface {
	Open(ctx context.Context, path string) (DB, error)
	Close(ctx context.Context, db DB) error
}

// ManagedArgs configures Managed.
type ManagedArgs[DB any] struct {
	// To is the local file; the path this machine already uses when empty.
	To string
	// Durability is how far the final sync must reach before the release
	// returns: DurabilityServer or DurabilityCloud. It is required.
	Durability Durability
	// Offload removes the local file once the bucket holds the released
	// version. It is requested only when the work succeeded.
	Offload bool
	// CleanupTimeout bounds closing and releasing, which run even after ctx
	// ends; DefaultCleanupTimeout when 0.
	CleanupTimeout time.Duration
	// Adapter opens and closes the connection. It is required; SQLAdapter
	// adapts database/sql.
	Adapter ScopeAdapter[DB]
	// MachineLabel is what other machines see as the writer.
	MachineLabel string
	// Maintenance checkpoints on release and VACUUMs past the freelist threshold.
	Maintenance bool
	// BlockOnLost blocks local writes while the write lock is lost.
	BlockOnLost bool
	// AcquireTimeout bounds downloading the head.
	AcquireTimeout time.Duration
}

// Managed acquires a database, opens it with the adapter, runs work, closes
// it, and releases it with the chosen durability, for workers and agents that
// move SQLite state between machines:
//
//	err := litebackup.Managed(ctx, client, "agents/42.sqlite", &litebackup.ManagedArgs[*sql.DB]{
//		To: path, Durability: litebackup.DurabilityCloud, Adapter: litebackup.SQLAdapter("sqlite"),
//	}, func(ctx context.Context, db *sql.DB) error {
//		_, err := db.ExecContext(ctx, "INSERT INTO steps (body) VALUES (?)", step)
//		return err
//	})
//
// Closing and releasing run under their own CleanupTimeout, even when ctx is
// cancelled. When work fails, the database is still closed and released,
// without offload, and the work's error comes first. When closing fails, the
// database is not released: it stays acquired by this machine with its file
// untouched, and the scope_cleanup_failed error says how to recover. Several
// failures are returned together with errors.Join.
func Managed[DB any](ctx context.Context, client *Client, name string, args *ManagedArgs[DB], work func(ctx context.Context, db DB) error) error {
	if args == nil {
		return invalidArgument("a managed database needs a durability and an adapter")
	}
	settings := *args
	if settings.Durability != DurabilityServer && settings.Durability != DurabilityCloud {
		return invalidArgument("a managed database needs a durability of server or cloud")
	}
	if settings.Adapter == nil || work == nil {
		return invalidArgument("a managed database needs an adapter and a function to run")
	}
	if settings.CleanupTimeout < 0 {
		return invalidArgument("the cleanup timeout must not be negative")
	}
	if settings.CleanupTimeout == 0 {
		settings.CleanupTimeout = DefaultCleanupTimeout
	}
	if ctx.Err() != nil {
		return &Error{Code: CodeCancelled, Message: "the managed database was cancelled before it was acquired", Err: ctx.Err()}
	}
	acquired, err := client.Acquire(ctx, name, &AcquireArgs{
		To: settings.To, Maintenance: settings.Maintenance, MachineLabel: settings.MachineLabel,
		BlockOnLost: settings.BlockOnLost, Timeout: settings.AcquireTimeout,
	})
	if err != nil {
		return err
	}
	path := acquired.Database.Path
	if path == "" {
		path, _ = absolutePath(settings.To)
	}
	scope := &managedScope{client: client, name: name, path: path, durability: settings.Durability, cleanupTimeout: settings.CleanupTimeout, ctx: ctx}

	db, err := settings.Adapter.Open(ctx, path)
	if err != nil {
		return joinScopeErrors(err, scope.release(false))
	}
	workDone := false
	defer func() {
		if workDone {
			return
		}
		// The work panicked: close and release without offload, then panic again.
		recovered := recover()
		cleanupContext, cancel := scope.cleanupContext()
		defer cancel()
		closeErr := settings.Adapter.Close(cleanupContext, db)
		if closeErr == nil {
			_ = scope.release(false)
		}
		panic(recovered)
	}()
	workErr := work(ctx, db)
	workDone = true

	cleanupContext, cancel := scope.cleanupContext()
	defer cancel()
	if closeErr := settings.Adapter.Close(cleanupContext, db); closeErr != nil {
		return joinScopeErrors(workErr, scope.closeFailed(closeErr))
	}
	return joinScopeErrors(workErr, scope.release(workErr == nil && settings.Offload))
}

// managedScope is one Managed call's database and cleanup settings.
type managedScope struct {
	client         *Client
	name           string
	path           string
	durability     Durability
	cleanupTimeout time.Duration
	ctx            context.Context
}

// cleanupContext keeps the caller's values but not its cancellation, and
// gives the daemon's own timeout answer time to arrive.
func (s *managedScope) cleanupContext() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.WithoutCancel(s.ctx), s.cleanupTimeout+deadlineGrace)
}

// release releases the database and returns a scope_cleanup_failed error
// when that fails.
func (s *managedScope) release(offload bool) error {
	cleanupContext, cancel := s.cleanupContext()
	defer cancel()
	_, err := s.client.Release(cleanupContext, s.name, &ReleaseArgs{Wait: s.durability, Offload: offload, Timeout: s.cleanupTimeout})
	if err == nil {
		return nil
	}
	return &Error{
		Code: CodeScopeCleanupFailed,
		Message: fmt.Sprintf("releasing %s failed: %v. Its file at %s is intact and this machine may still hold its write lock; call Release(ctx, %q, ...) again",
			s.name, err, s.path, s.name),
		Details: map[string]any{"step": "release", "name": s.name, "path": s.path, "durability": string(s.durability)},
		Err:     err,
	}
}

// closeFailed is the scope_cleanup_failed error for a close that failed.
func (s *managedScope) closeFailed(err error) error {
	return &Error{
		Code: CodeScopeCleanupFailed,
		Message: fmt.Sprintf("closing %s failed, so it was not released and stays acquired by this machine: %v. Close every connection to %s, then call Release(ctx, %q, ...)",
			s.name, err, s.path, s.name),
		Details: map[string]any{"step": "close", "name": s.name, "path": s.path, "acquired": true},
		Err:     err,
	}
}

// joinScopeErrors returns nil, the one error, or errors.Join of them in order.
func joinScopeErrors(errs ...error) error {
	var failures []error
	for _, err := range errs {
		if err != nil {
			failures = append(failures, err)
		}
	}
	switch len(failures) {
	case 0:
		return nil
	case 1:
		return failures[0]
	default:
		return errors.Join(failures...)
	}
}

// SQLAdapter opens a managed database with database/sql and the named
// driver, such as "sqlite" (modernc.org/sqlite) or "sqlite3"
// (github.com/mattn/go-sqlite3), passing the path as the DSN.
//
// Close closes the pool. Commit what the work should keep: closing a SQLite
// connection rolls back its unfinished transaction. A connection still in
// use, such as a transaction the work never ended, makes Close fail, so the
// database is not released while it may still be written.
func SQLAdapter(driverName string) ScopeAdapter[*sql.DB] {
	return sqlAdapter{driverName: driverName}
}

type sqlAdapter struct {
	driverName string
}

// Open opens and pings the database.
func (a sqlAdapter) Open(ctx context.Context, path string) (*sql.DB, error) {
	if strings.ContainsRune(path, '?') {
		return nil, invalidArgument(fmt.Sprintf("SQLAdapter passes the path as the DSN, and %q contains '?'; write a ScopeAdapter that builds the DSN", path))
	}
	db, err := sql.Open(a.driverName, path)
	if err != nil {
		return nil, err
	}
	if err := db.PingContext(ctx); err != nil {
		return nil, errors.Join(err, db.Close())
	}
	return db, nil
}

// Close closes the pool and fails when a connection was still in use.
func (a sqlAdapter) Close(_ context.Context, db *sql.DB) error {
	inUse := db.Stats().InUse
	closeErr := db.Close()
	if inUse > 0 {
		return errors.Join(fmt.Errorf("%d connection(s) were still in use, such as a transaction that was never committed or rolled back", inUse), closeErr)
	}
	return closeErr
}
