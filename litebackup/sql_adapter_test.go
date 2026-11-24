package litebackup

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"testing"
	"time"
)

// blockingDriver hands out connections whose Close waits for release.
type blockingDriver struct{ release chan struct{} }

func (d blockingDriver) Open(string) (driver.Conn, error) { return blockingConn(d), nil }

type blockingConn struct{ release chan struct{} }

func (blockingConn) Prepare(string) (driver.Stmt, error) { return nil, errors.New("unsupported") }
func (blockingConn) Begin() (driver.Tx, error)           { return nil, errors.New("unsupported") }

func (c blockingConn) Close() error {
	<-c.release
	return nil
}

func TestSQLAdapterCloseHonorsTheCleanupContext(t *testing.T) {
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	sql.Register("litebackup-blocking-close", blockingDriver{release: release})
	db, err := sql.Open("litebackup-blocking-close", "x")
	if err != nil {
		t.Fatal(err)
	}
	conn, err := db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if err := conn.Close(); err != nil { // back to the idle pool, closed by db.Close
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 100*time.Millisecond)
	defer cancel()
	started := time.Now()
	err = SQLAdapter("litebackup-blocking-close", nil).Close(ctx, db)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("close: %v, want the cleanup deadline", err)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("close took %s", elapsed)
	}
}
