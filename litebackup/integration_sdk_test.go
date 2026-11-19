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
	failClose error
	holder    *exec.Cmd
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
	holder := exec.Command("python3", "-c", "import sqlite3,sys,time; c=sqlite3.connect(sys.argv[1]); c.execute('select count(*) from sqlite_schema').fetchall(); print('open', flush=True); time.sleep(60)", path)
	output, err := holder.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := holder.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = holder.Process.Kill()
		_ = holder.Wait()
	})
	buffer := make([]byte, 4)
	if _, err := output.Read(buffer); err != nil {
		t.Fatal(err)
	}
	return holder
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
	addRows(t, appPath, 2)
	first, err := worker.StartBackup(ctx, appPath, nil)
	if err != nil {
		t.Fatal(err)
	}
	again, err := worker.StartBackup(ctx, appPath, nil)
	if err != nil || again.ID != first.ID {
		t.Fatalf("starting again: %+v %v", again, err)
	}
	_, err = worker.StartBackup(ctx, appPath, &StartBackupArgs{MaximumLocalDatabases: 3})
	var refused *Error
	if !errors.As(err, &refused) || refused.Code != CodeExists || refused.Status != 409 {
		t.Fatalf("other settings: %v", err)
	}
	rule, _ := refused.Details["rule"].(map[string]any)
	if rule["id"] != first.ID || rule["name"] != "main.sqlite" {
		t.Fatalf("the refusal carries %v, want the saved rule %s", refused.Details, first.ID)
	}

	// A managed scope that succeeds releases with a cloud wait and offloads.
	agentPath := server.path("agents", "42.sqlite")
	if err := os.WriteFile(agentPath, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	err = Managed(ctx, worker, "agents/42.sqlite", &ManagedArgs[string]{
		To: agentPath, Durability: DurabilityCloud, Offload: true, Adapter: &sqliteCommandAdapter{},
	}, func(_ context.Context, path string) error {
		addRows(t, path, 5)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	status, err := worker.Database(ctx, "agents/42.sqlite")
	if err != nil {
		t.Fatal(err)
	}
	if status.SyncedCloud.Version == 0 || status.SyncedCloud.Version != status.SyncedServer.Version || status.State != "offloaded" {
		t.Fatalf("after a cloud release with offload: %+v", status)
	}

	// Failed work still releases, without offload, and keeps the work's error.
	workFailed := errors.New("the agent step failed")
	err = Managed(ctx, worker, "agents/42.sqlite", &ManagedArgs[string]{
		Durability: DurabilityServer, Offload: true, Adapter: &sqliteCommandAdapter{},
	}, func(_ context.Context, path string) error {
		if countRows(t, path) != 5 {
			t.Errorf("the offloaded database came back with %d rows", countRows(t, path))
		}
		addRows(t, path, 1)
		return workFailed
	})
	if err != workFailed {
		t.Fatalf("failed work returned %v", err)
	}
	writer, err := worker.Writer(ctx, "agents/42.sqlite")
	if err != nil || writer.ThisMachine {
		t.Fatalf("after failed work the database is still held: %+v %v", writer, err)
	}
	status, _ = worker.Database(ctx, "agents/42.sqlite")
	if status.State == "offloaded" {
		t.Fatal("failed work offloaded the local copy")
	}

	// A failed close does not release: the database stays acquired here.
	closeFailed := errors.New("a connection is still open")
	err = Managed(ctx, worker, "agents/42.sqlite", &ManagedArgs[string]{
		Durability: DurabilityServer, Adapter: &sqliteCommandAdapter{failClose: closeFailed},
	}, func(context.Context, string) error { return nil })
	if !IsCode(err, CodeScopeCleanupFailed) || !errors.Is(err, closeFailed) {
		t.Fatalf("failed close returned %v", err)
	}
	writer, err = worker.Writer(ctx, "agents/42.sqlite")
	if err != nil || !writer.ThisMachine {
		t.Fatalf("after a failed close the lock moved: %+v %v", writer, err)
	}
	if _, err := worker.Release(ctx, "agents/42.sqlite", &ReleaseArgs{Wait: DurabilityServer}); err != nil {
		t.Fatalf("the recovery the error names: %v", err)
	}

	// A release that times out on an open handle is a cleanup failure with its durability.
	err = Managed(ctx, worker, "agents/42.sqlite", &ManagedArgs[string]{
		Durability: DurabilityServer, CleanupTimeout: 2 * time.Second, Adapter: &sqliteCommandAdapter{},
	}, func(_ context.Context, path string) error {
		holdOpen(t, path)
		return nil
	})
	var cleanup *Error
	if !errors.As(err, &cleanup) || cleanup.Code != CodeScopeCleanupFailed || cleanup.Details["durability"] != "server" || !IsCode(cleanup.Err, CodeStillOpen) {
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
			clients[index], failures[index] = Connect(ctx, &ConnectArgs{Token: token, Session: session})
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
	_, err = Connect(ctx, &ConnectArgs{SocketPath: filepath.Join(shortDirectory(t), "missing.sock")})
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

// TestIntegrationOldDaemon refuses a daemon older than MinimumDaemonVersion,
// such as a published 0.7.0, with incompatible_version.
func TestIntegrationOldDaemon(t *testing.T) {
	oldBinary := os.Getenv("LITEBACKUP_OLD_PATH")
	if os.Getenv("LITEBACKUP_INTEGRATION") != "1" || oldBinary == "" {
		t.Skip("set LITEBACKUP_INTEGRATION=1, LITEBACKUP_PATH, and LITEBACKUP_OLD_PATH to a litebackup older than " + MinimumDaemonVersion)
	}
	server := startDevelopmentServer(t)
	session := server.session("old")
	token, err := server.joinToken(session)
	if err != nil {
		t.Fatal(err)
	}
	// The old binary joins and starts its own daemon, as an app machine that
	// was never upgraded would have.
	launch := exec.Command(oldBinary, "launch", "--session", session)
	launch.Env = append(os.Environ(), TokenEnvironment+"="+token, BinaryPathEnvironment+"="+oldBinary)
	if output, err := launch.CombinedOutput(); err != nil {
		t.Fatalf("starting the old daemon: %v\n%s", err, output)
	}
	t.Cleanup(func() {
		_ = exec.Command(oldBinary, "drain", "--session", session).Run()
	})
	_, err = Connect(t.Context(), &ConnectArgs{Session: session})
	if !IsCode(err, CodeIncompatibleVersion) || !strings.Contains(err.Error(), "litebackup drain --session "+session) {
		t.Fatalf("an old daemon: %v", err)
	}
	t.Log(err)
	// Following the guidance replaces it: drain, and the next connect starts the new binary.
	if output, err := exec.Command(oldBinary, "drain", "--session", session).CombinedOutput(); err != nil {
		t.Fatalf("draining the old daemon: %v\n%s", err, output)
	}
	upgraded := server.reconnect("old")
	if compareVersions(upgraded.ClientVersion(), MinimumDaemonVersion) < 0 {
		t.Fatalf("after draining, the daemon is %s", upgraded.ClientVersion())
	}
}
