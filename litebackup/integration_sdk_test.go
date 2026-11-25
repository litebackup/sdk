package litebackup

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// sqliteCommandAdapter opens nothing itself: the work writes with the sqlite3
// command, and Close can fail or leave a process holding the file open.
type sqliteCommandAdapter struct {
	failClose  error
	appCommand *exec.Cmd
}

func (a *sqliteCommandAdapter) Open(_ context.Context, path string) (string, error) {
	return path, nil
}

func (a *sqliteCommandAdapter) Close(context.Context, string) error {
	return a.failClose
}

// holdOpen starts a process that keeps the database open until the test ends.
func holdOpen(t *testing.T, path string) *exec.Cmd {
	t.Helper()
	appCommand := exec.Command(
		"python3",
		"-u",
		"-c",
		"import sqlite3,sys,time; c=sqlite3.connect(sys.argv[1]); c.execute('select count(*) from sqlite_schema').fetchall(); print('open'); time.sleep(60)",
		path,
	)
	output, err := appCommand.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := appCommand.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = appCommand.Process.Kill()
		_ = appCommand.Wait()
	})
	buffer := make([]byte, 4)
	if _, err := output.Read(buffer); err != nil {
		t.Fatal(err)
	}
	return appCommand
}

// TestIntegrationRegistrationAndManaged checks the 1.0 contract against a
// real daemon: repeated registration, managed scopes with real waits and
// failures, concurrent startup, explicit sockets, and version refusal.
func TestIntegrationRegistrationAndManaged(t *testing.T) {
	server := startDevelopmentServer(t)
	ctx := t.Context()
	worker := server.connect("worker")

	// Registration is idempotent; other settings are refused with the saved rule.
	appPath := server.path("app", "main.sqlite")
	addItems(t, appPath, 2)
	first, err := worker.StartBackup(ctx, appPath, nil)
	if err != nil {
		t.Fatal(err)
	}
	again, err := worker.StartBackup(ctx, appPath, nil)
	if err != nil || again.ID != first.ID {
		t.Fatalf("starting again: %+v %v", again, err)
	}
	_, err = worker.StartBackup(ctx, appPath, &StartBackupArgs{MaximumLocalDatabases: 3})
	var denied *Error
	if !errors.As(err, &denied) || denied.Code != CodeRuleExists || denied.Status != 409 {
		t.Fatalf("other settings: %v", err)
	}
	rule, _ := denied.Details["rule"].(map[string]any)
	if rule["id"] != first.ID || rule["name"] != "main.sqlite" {
		t.Fatalf("the refusal carries %v, want the saved rule %s", denied.Details, first.ID)
	}

	// A lease with cloud durability returns once the bucket holds its work,
	// keeps the lock, and offloads when the pool releases it.
	agentPath := server.path("agents", "42.sqlite")
	if err := os.WriteFile(agentPath, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	pool, err := NewPool(worker, &PoolArgs[string]{Adapter: &sqliteCommandAdapter{}})
	if err != nil {
		t.Fatal(err)
	}
	err = pool.Lease(ctx, "agents/42.sqlite", &LeaseArgs{
		Path: agentPath, Durability: DurabilityCloud, Offload: true,
	}, func(_ context.Context, path string) error {
		addItems(t, path, 5)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	status, err := worker.Database(ctx, "agents/42.sqlite")
	if err != nil {
		t.Fatal(err)
	}
	if status.SyncedCloud.Version == 0 || status.SyncedCloud.Version != status.SyncedServer.Version ||
		!status.Acquired {
		t.Fatalf("after a lease with cloud durability: %+v", status)
	}
	if err := pool.Release(ctx, "agents/42.sqlite"); err != nil {
		t.Fatal(err)
	}
	status, _ = worker.Database(ctx, "agents/42.sqlite")
	if status.State != "offloaded" {
		t.Fatalf("after the pool released with offload: %+v", status)
	}

	// Failed work keeps the lease and its error; the release then does not offload.
	workFailed := errors.New("the agent step failed")
	err = pool.Lease(ctx, "agents/42.sqlite", &LeaseArgs{Offload: true},
		func(_ context.Context, path string) error {
			if countItems(t, path) != 5 {
				t.Errorf("the offloaded database came back with %d rows", countItems(t, path))
			}
			addItems(t, path, 1)
			return workFailed
		})
	if err != workFailed {
		t.Fatalf("failed work returned %v", err)
	}
	writer, err := worker.Writer(ctx, "agents/42.sqlite")
	if err != nil || !writer.ThisMachine {
		t.Fatalf("after failed work the lease is gone: %+v %v", writer, err)
	}
	if err := pool.Close(ctx); err != nil {
		t.Fatal(err)
	}
	status, _ = worker.Database(ctx, "agents/42.sqlite")
	if status.State == "offloaded" {
		t.Fatal("the release after failed work offloaded the local copy")
	}

	// A failed close does not release: the database stays acquired here.
	closeFailed := errors.New("a connection is still open")
	pool, err = NewPool(worker, &PoolArgs[string]{Adapter: &sqliteCommandAdapter{failClose: closeFailed}})
	if err != nil {
		t.Fatal(err)
	}
	if err := pool.Lease(ctx, "agents/42.sqlite", nil, func(context.Context, string) error { return nil }); err != nil {
		t.Fatal(err)
	}
	err = pool.Release(ctx, "agents/42.sqlite")
	var cleanup *LeaseCleanupError
	if !errors.As(err, &cleanup) || cleanup.Step != LeaseCleanupStepClose || !IsCode(err, CodeLeaseCleanupFailed) ||
		!errors.Is(err, closeFailed) {
		t.Fatalf("failed close returned %v", err)
	}
	writer, err = worker.Writer(ctx, "agents/42.sqlite")
	if err != nil || !writer.ThisMachine {
		t.Fatalf("after a failed close the lock moved: %+v %v", writer, err)
	}
	if _, err := worker.Release(ctx, "agents/42.sqlite", &ReleaseArgs{Durability: DurabilityServer}); err != nil {
		t.Fatalf("the recovery the error names: %v", err)
	}

	// A release that times out on an open handle is a cleanup failure with its durability.
	pool, err = NewPool(worker, &PoolArgs[string]{Adapter: &sqliteCommandAdapter{}, CleanupTimeout: 2 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	err = pool.Lease(ctx, "agents/42.sqlite", &LeaseArgs{Durability: DurabilityServer},
		func(_ context.Context, path string) error {
			holdOpen(t, path)
			return nil
		})
	if err != nil {
		t.Fatal(err)
	}
	err = pool.Close(ctx)
	cleanup = nil
	if !errors.As(err, &cleanup) || cleanup.Step != LeaseCleanupStepRelease ||
		cleanup.Err.Details["durability"] != "server" || !IsCode(cleanup.Err.Err, CodeStillOpen) {
		t.Fatalf("release with a handle open returned %v", err)
	}

	// Concurrent connects to a fresh session start one daemon.
	session := server.session("concurrent")
	token, err := server.joinToken(session)
	if err != nil {
		t.Fatal(err)
	}
	var group sync.WaitGroup
	clients := make([]*Client, 6)
	failures := make([]error, len(clients))
	for index := range clients {
		group.Go(func() {
			clients[index], failures[index] = Connect(
				ctx,
				&ConnectArgs{APIKey: token, Session: session},
			)
		})
	}
	group.Wait()
	for index, failure := range failures {
		if failure != nil {
			t.Fatalf("connect %d: %v", index, failure)
		}
	}
	server.clients = append(server.clients, clients[0])
	for _, client := range clients[1:] {
		_ = client.Close()
	}
	daemons, _ := exec.Command("pgrep", "-f", "daemon --session "+session+"$").Output()
	if lines := len(strings.Fields(string(daemons))); lines != 1 {
		t.Fatalf("%d daemons for one session", lines)
	}

	// An explicit socket never launches; a missing binary is binary_not_found.
	_, err = Connect(
		ctx,
		&ConnectArgs{SocketPath: filepath.Join(shortDirectory(t), "missing.sock")},
	)
	if !IsCode(err, CodeConnectionFailed) {
		t.Fatalf("an explicit socket with no daemon: %v", err)
	}
	t.Setenv(BinaryPathEnvironment, "")
	t.Setenv("PATH", shortDirectory(t))
	t.Setenv("HOME", shortDirectory(t))
	_, err = Connect(ctx, &ConnectArgs{Session: server.session("nobinary")})
	if !IsCode(err, CodeBinaryNotFound) {
		t.Fatalf("no binary: %v", err)
	}
}
