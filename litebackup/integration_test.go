package litebackup

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// integrationLease is the test server's session lease: short, so a failover
// replica takes over a dead origin within seconds.
const integrationLease = 3 * time.Second

// developmentServer is a `litebackup server run --development` with a local
// bucket, the clients its test connected, and their sessions.
type developmentServer struct {
	t         *testing.T
	binary    string
	directory string
	run       string
	data      string
	address   string
	clients   []*Client
}

// startDevelopmentServer runs a development server, adds a local bucket, and
// completes setup, as the litebackup repository's own tests do. It skips
// unless LITEBACKUP_INTEGRATION=1 and LITEBACKUP_PATH names the binary.
func startDevelopmentServer(t *testing.T) *developmentServer {
	t.Helper()
	if os.Getenv("LITEBACKUP_INTEGRATION") != "1" {
		t.Skip("set LITEBACKUP_INTEGRATION=1 and LITEBACKUP_PATH to run against a development server")
	}
	binary := os.Getenv(BinaryPathEnvironment)
	if binary == "" {
		t.Fatal("LITEBACKUP_INTEGRATION=1 needs LITEBACKUP_PATH")
	}
	if _, err := exec.LookPath("sqlite3"); err != nil {
		t.Fatal("the integration test writes databases with the sqlite3 command")
	}
	directory := shortDirectory(t)
	// The run token names the data directory and every session, so the
	// watchdog finds this run's processes and no one else's.
	run := strings.ToLower(filepath.Base(directory))
	server := &developmentServer{t: t, binary: binary, directory: directory, run: run, data: filepath.Join(directory, "server"), address: "127.0.0.1:" + strconv.Itoa(freePort(t))}
	startWatchdog(t, run)
	// Every daemon this test starts keeps its sockets and state here.
	t.Setenv("XDG_RUNTIME_DIR", filepath.Join(directory, "run"))
	t.Setenv("XDG_STATE_HOME", filepath.Join(directory, "state"))
	t.Setenv(TokenEnvironment, "")
	t.Setenv(SessionEnvironment, "")

	log, err := os.Create(filepath.Join(directory, "server.log"))
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command(binary, "server", "run", "--development", "--data-directory", server.data, "--listen-address", server.address, "--archive-delay", "1s", "--minimum-sync-interval", "0s",
		"--session-lease", integrationLease.String(), "--session-heartbeat", (integrationLease / 3).String())
	command.Stdout = log
	command.Stderr = log
	command.SysProcAttr = &syscall.SysProcAttr{Pdeathsig: syscall.SIGKILL}
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		// Stop the daemons before the server, because their shutdown ends their sessions on it.
		for _, client := range server.clients {
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
			if _, err := client.Shutdown(ctx, &ShutdownArgs{Wait: DurabilityNone}); err != nil {
				t.Logf("shutting down %s: %v", client.Session(), err)
			}
			cancel()
			if err := client.Close(); err != nil {
				t.Log(err)
			}
		}
		killRun(run)
		if err := command.Process.Kill(); err != nil {
			t.Log(err)
		}
		if err := command.Wait(); err != nil {
			t.Logf("server exited: %v", err)
		}
		if err := log.Close(); err != nil {
			t.Log(err)
		}
		if t.Failed() {
			content, err := os.ReadFile(log.Name())
			if err == nil {
				t.Logf("server log:\n%s", content)
			}
		}
	})
	// The admin socket answers once the server is up.
	deadline := time.Now().Add(30 * time.Second)
	for {
		_, err := server.joinToken("probe")
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the server never answered: %v", err)
		}
		time.Sleep(100 * time.Millisecond)
	}
	server.admin("bucket", "create", "--provider", "s3", "--endpoint", "file://"+filepath.Join(directory, "bucket"), "--bucket", "local", "--name", "local")
	server.admin("setup", "complete", "key_backup")
	server.admin("setup", "complete", "retention")
	return server
}

// startWatchdog starts a detached shell that kills every process of this run
// once the test binary exits, even when a timeout panic skips the cleanups.
func startWatchdog(t *testing.T, run string) {
	t.Helper()
	script := fmt.Sprintf("while kill -0 %d 2>/dev/null; do sleep 1; done; pkill -9 -f %s", os.Getpid(), run)
	watchdog := exec.Command("sh", "-c", script)
	watchdog.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := watchdog.Start(); err != nil {
		t.Fatal(err)
	}
	if err := watchdog.Process.Release(); err != nil {
		t.Fatal(err)
	}
}

// killRun kills the daemons and the server of one run.
func killRun(run string) {
	// pkill exits 1 when nothing matched, which is fine here.
	_ = exec.Command("pkill", "-9", "-f", "(daemon --session .*-"+run+"$)|("+run+"/server)").Run()
}

// freePort returns a loopback port nothing listens on.
func freePort(t *testing.T) int {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	return port
}

// admin runs a `litebackup server` command against the data directory.
func (s *developmentServer) admin(arguments ...string) string {
	s.t.Helper()
	arguments = append(append([]string{"server"}, arguments...), "--data-directory", s.data)
	output, err := exec.Command(s.binary, arguments...).CombinedOutput()
	if err != nil {
		s.t.Fatalf("litebackup %s: %v\n%s", strings.Join(arguments, " "), err, output)
	}
	return string(output)
}

// joinToken creates a join token named after a session.
func (s *developmentServer) joinToken(name string) (string, error) {
	output, err := exec.Command(s.binary, "server", "join-token", "create", "--name", name, "--data-directory", s.data, "--stdout-format", "json").Output()
	if err != nil {
		return "", err
	}
	var created struct {
		Token string `json:"token"`
	}
	if err := json.Unmarshal(output, &created); err != nil {
		return "", err
	}
	return created.Token + "@http://" + s.address, nil
}

// connect starts a session's daemon with its own join token, as an app on
// another machine would.
func (s *developmentServer) connect(session string) *Client {
	s.t.Helper()
	session = s.session(session)
	token, err := s.joinToken(session)
	if err != nil {
		s.t.Fatal(err)
	}
	client, err := Connect(s.t.Context(), &ConnectArgs{Token: token, Session: session})
	if err != nil {
		s.t.Fatalf("connecting %s: %v", session, err)
	}
	s.clients = append(s.clients, client)
	return client
}

// reconnect starts a session's daemon again; it keeps the join it made.
func (s *developmentServer) reconnect(session string) *Client {
	s.t.Helper()
	session = s.session(session)
	client, err := Connect(s.t.Context(), &ConnectArgs{Session: session})
	if err != nil {
		s.t.Fatalf("reconnecting %s: %v", session, err)
	}
	s.clients = append(s.clients, client)
	return client
}

// kill stops a session's daemon with SIGKILL, so its lease runs out on the server.
func (s *developmentServer) kill(session string) {
	s.t.Helper()
	output, err := exec.Command("pgrep", "-f", "daemon --session "+s.session(session)+"$").Output()
	if err != nil {
		s.t.Fatalf("finding the %s daemon: %v", session, err)
	}
	for _, field := range strings.Fields(string(output)) {
		pid, err := strconv.Atoi(field)
		if err != nil {
			s.t.Fatal(err)
		}
		process, err := os.FindProcess(pid)
		if err != nil {
			s.t.Fatal(err)
		}
		if err := process.Kill(); err != nil {
			s.t.Fatal(err)
		}
	}
}

// session names a session of this run.
func (s *developmentServer) session(name string) string {
	return name + "-" + s.run
}

// path returns a file path under the test directory, creating its directory.
func (s *developmentServer) path(elements ...string) string {
	s.t.Helper()
	path := filepath.Join(append([]string{s.directory}, elements...)...)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		s.t.Fatal(err)
	}
	return path
}

// sqlite runs SQL with the sqlite3 command and returns its output.
func sqlite(t *testing.T, path string, statements string) (string, error) {
	t.Helper()
	output, err := exec.Command("sqlite3", "-cmd", ".timeout 200", path, statements).CombinedOutput()
	return strings.TrimSpace(string(output)), err
}

// addRows adds rows to a database, creating it when needed.
func addRows(t *testing.T, path string, rows int) {
	t.Helper()
	statements := "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, body BLOB);"
	for range rows {
		statements += "INSERT INTO items (body) VALUES (randomblob(2000));"
	}
	output, err := sqlite(t, path, statements)
	if err != nil {
		t.Fatalf("writing %s: %v %s", path, err, output)
	}
}

// countRows counts the rows of a database.
func countRows(t *testing.T, path string) int {
	t.Helper()
	output, err := sqlite(t, path, "SELECT count(*) FROM items;")
	if err != nil {
		t.Fatalf("reading %s: %v %s", path, err, output)
	}
	count, err := strconv.Atoi(output)
	if err != nil {
		t.Fatal(err)
	}
	return count
}

// eventually calls check until it returns nil or the timeout passes.
func eventually(t *testing.T, timeout time.Duration, check func() error) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for {
		err := check()
		if err == nil {
			return
		}
		if time.Now().After(deadline) {
			t.Fatal(err)
		}
		time.Sleep(250 * time.Millisecond)
	}
}

func TestIntegration(t *testing.T) {
	server := startDevelopmentServer(t)
	ctx := t.Context()
	web1 := server.connect("web-1")
	web2 := server.connect("web-2")
	reader := server.connect("reader")
	if web1.ClientVersion() == "" {
		t.Fatal("the daemon did not report its version")
	}

	// Backups and name collisions.
	mainPath := server.path("web-1", "main.db")
	addRows(t, mainPath, 10)
	backup, err := web1.StartBackup(ctx, mainPath, nil)
	if err != nil {
		t.Fatal(err)
	}
	if backup.Name != "main.db" {
		t.Fatalf("backup name %q", backup.Name)
	}
	if _, err := web1.Sync(ctx, backup.Name); err != nil {
		t.Fatal(err)
	}
	otherPath := server.path("web-2", "main.db")
	addRows(t, otherPath, 3)
	err = web2.Backup(ctx, otherPath, nil)
	if !IsCode(err, "busy") {
		t.Fatalf("a second machine backing up main.db got %v, want busy", err)
	}
	prefixed, err := web2.StartBackup(ctx, otherPath, &StartBackupArgs{Prefix: "web-2/"})
	if err != nil {
		t.Fatal(err)
	}
	if prefixed.Name != "web-2/main.db" {
		t.Fatalf("prefixed name %q", prefixed.Name)
	}
	backups, err := web2.Backups(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(backups) != 1 || backups[0].ID != prefixed.ID {
		t.Fatalf("backups %+v", backups)
	}

	// Versions and restore.
	addRows(t, mainPath, 5)
	if _, err := web1.Sync(ctx, backup.Name); err != nil {
		t.Fatal(err)
	}
	versions, err := web1.Versions(ctx, "main.db", nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(versions) < 2 {
		t.Fatalf("versions %+v", versions)
	}
	restoredPath := server.path("restored", "main.db")
	restored, err := web2.Restore(ctx, "main.db", &RestoreArgs{To: restoredPath, Version: versions[0].Version})
	if err != nil {
		t.Fatal(err)
	}
	if restored.Version != versions[0].Version || countRows(t, restoredPath) != 10 {
		t.Fatalf("restored %+v with %d rows", restored, countRows(t, restoredPath))
	}
	_, err = web2.Restore(ctx, "main.db", &RestoreArgs{To: restoredPath})
	if !IsCode(err, "exists") {
		t.Fatalf("restoring over a file got %v, want exists", err)
	}
	headPath := server.path("restored", "head.db")
	// The package functions share a client of LITEBACKUP_SESSION.
	t.Setenv(SessionEnvironment, server.session("web-2"))
	headVersions, err := Versions(ctx, "main.db", nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(headVersions) != len(versions) {
		t.Fatalf("package Versions %d, client Versions %d", len(headVersions), len(versions))
	}
	if _, err := Restore(ctx, "main.db", &RestoreArgs{To: headPath, Time: time.Now()}); err != nil {
		t.Fatal(err)
	}
	if countRows(t, headPath) != 15 {
		t.Fatalf("the head restore has %d rows", countRows(t, headPath))
	}

	// A replica on a readonly machine.
	replicaPath := server.path("reader", "main.db")
	replica, err := reader.Replicate(ctx, "main.db", &ReplicateArgs{To: replicaPath})
	if err != nil {
		t.Fatal(err)
	}
	head, err := web1.Database(ctx, "main.db")
	if err != nil {
		t.Fatal(err)
	}
	if replica.Role != RoleReplica || replica.SyncedServer.Version != head.SyncedServer.Version {
		t.Fatalf("replica %+v, head %d", replica, head.SyncedServer.Version)
	}
	if countRows(t, replicaPath) != 15 {
		t.Fatalf("the replica has %d rows", countRows(t, replicaPath))
	}
	if _, err := sqlite(t, replicaPath, "DELETE FROM items;"); err == nil {
		t.Fatal("a write to the replica passed")
	}
	replicas, err := reader.Replicas(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(replicas.Replicas) != 1 {
		t.Fatalf("replicas %+v", replicas)
	}

	// Acquire and release move a database between machines.
	tenantPath := server.path("web-1", "42.db")
	addRows(t, tenantPath, 4)
	acquired, err := web1.Acquire(ctx, "tenants/42.db", &AcquireArgs{To: tenantPath, Maintenance: true})
	if err != nil {
		t.Fatal(err)
	}
	if acquired.Outcome != "created" || !acquired.Database.Acquired {
		t.Fatalf("acquired %+v", acquired)
	}
	writer, err := web1.Writer(ctx, "tenants/42.db")
	if err != nil {
		t.Fatal(err)
	}
	if !writer.ThisMachine || !writer.Active {
		t.Fatalf("writer %+v", writer)
	}
	addRows(t, tenantPath, 2)
	released, err := web1.Release(ctx, "tenants/42.db", &ReleaseArgs{Wait: DurabilityServer})
	if err != nil {
		t.Fatal(err)
	}
	if released.Database.SyncedServer.Version < 2 {
		t.Fatalf("released %+v", released)
	}
	movedPath := server.path("web-2", "42.db")
	moved, err := web2.Acquire(ctx, "tenants/42.db", &AcquireArgs{To: movedPath})
	if err != nil {
		t.Fatal(err)
	}
	if moved.Outcome != "downloaded" || countRows(t, movedPath) != 6 {
		t.Fatalf("moved %+v with %d rows", moved, countRows(t, movedPath))
	}
	if _, err := web2.Release(ctx, "tenants/42.db", &ReleaseArgs{Wait: DurabilityServer}); err != nil {
		t.Fatal(err)
	}
	conflicts, err := web2.Conflicts(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(conflicts) != 0 {
		t.Fatalf("conflicts %+v", conflicts)
	}
}

// TestIntegrationFailover runs one database on three machines. The origin
// dies and exactly one replica takes over within about the server's short
// lease; the old origin comes back with its writes saved as a conflict; then the new
// origin dies too and the last replica takes over.
func TestIntegrationFailover(t *testing.T) {
	server := startDevelopmentServer(t)
	ctx := t.Context()
	first := server.connect("first")
	second := server.connect("second")
	third := server.connect("third")
	const name = "shared.db"

	firstPath := server.path("first", "shared.db")
	addRows(t, firstPath, 5)
	firstFailover, err := first.Failover(ctx, firstPath, &FailoverArgs{Name: name})
	if err != nil {
		t.Fatal(err)
	}
	if !firstFailover.IsOrigin() {
		t.Fatalf("the first machine is %s", firstFailover.Role())
	}
	if _, err := first.Sync(ctx, name); err != nil {
		t.Fatal(err)
	}
	roles := make(chan Role, 8)
	secondFailover, err := second.Failover(ctx, server.path("second", "shared.db"), &FailoverArgs{
		Name:         name,
		OnRoleChange: func(role Role) { roles <- role },
	})
	if err != nil {
		t.Fatal(err)
	}
	thirdPath := server.path("third", "shared.db")
	thirdFailover, err := third.Failover(ctx, thirdPath, &FailoverArgs{
		Name:         name,
		OnRoleChange: func(role Role) { roles <- role },
	})
	if err != nil {
		t.Fatal(err)
	}
	if secondFailover.IsOrigin() || thirdFailover.IsOrigin() {
		t.Fatalf("roles %s %s", secondFailover.Role(), thirdFailover.Role())
	}
	eventually(t, 30*time.Second, func() error {
		if rows := countRows(t, thirdPath); rows != 5 {
			return fmt.Errorf("the third machine's replica has %d rows", rows)
		}
		return nil
	})

	// The origin dies; one replica takes over about a lease later.
	server.kill("first")
	killed := time.Now()
	select {
	case role := <-roles:
		if role != RoleOrigin {
			t.Fatalf("a replica became %s", role)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("no replica took over")
	}
	if took := time.Since(killed); took > integrationLease+3*time.Second {
		t.Errorf("the takeover took %s with a %s lease", took, integrationLease)
	}
	time.Sleep(2 * time.Second)
	if secondFailover.IsOrigin() == thirdFailover.IsOrigin() {
		t.Fatalf("roles after the takeover %s %s", secondFailover.Role(), thirdFailover.Role())
	}
	origin, replica, originClient := secondFailover, thirdFailover, "second"
	if thirdFailover.IsOrigin() {
		origin, replica, originClient = thirdFailover, secondFailover, "third"
	}
	// The old origin's app kept writing while its daemon was gone.
	addRows(t, firstPath, 3)

	// The first machine comes back: its writes are saved as a conflict and it follows the new origin.
	restarted := server.reconnect("first")
	comeback, err := restarted.Failover(ctx, firstPath, &FailoverArgs{Name: name})
	if err != nil {
		t.Fatal(err)
	}
	// The restarted daemon resumes the failover rule on its own and may set
	// the writes aside before this Failover call; its event log has them.
	eventually(t, 60*time.Second, func() error {
		page, err := restarted.Events(ctx, &EventsArgs{Limit: 10000})
		if err != nil {
			return err
		}
		for _, event := range page.Events {
			if event.Type == "conflict_saved" && event.Name == name {
				_, err := os.Stat(event.Path)
				return err
			}
		}
		return fmt.Errorf("no conflict_saved event yet")
	})
	eventually(t, 60*time.Second, func() error {
		if comeback.Role() != RoleReplica {
			return fmt.Errorf("the first machine is %s", comeback.Role())
		}
		return nil
	})
	if countRows(t, firstPath) != 5 {
		t.Fatalf("the first machine's replica has %d rows, want the new origin's 5", countRows(t, firstPath))
	}
	if !origin.IsOrigin() {
		t.Fatalf("the new origin is %s once the old one returned", origin.Role())
	}

	// The new origin dies too; the last replica takes over.
	server.kill(originClient)
	eventually(t, 30*time.Second, func() error {
		if !replica.IsOrigin() && !comeback.IsOrigin() {
			return fmt.Errorf("no replica took over yet")
		}
		return nil
	})
	time.Sleep(2 * time.Second)
	if replica.IsOrigin() == comeback.IsOrigin() {
		t.Fatalf("roles after the second takeover %s %s", replica.Role(), comeback.Role())
	}
	if err := replica.Stop(ctx); err != nil {
		t.Fatal(err)
	}
}

func TestIntegrationPrefixReplica(t *testing.T) {
	server := startDevelopmentServer(t)
	ctx := t.Context()
	origin := server.connect("prefix-origin")
	reader := server.connect("prefix-reader")
	firstPath := server.path("prefix-origin", "first.db")
	addRows(t, firstPath, 2)
	_, err := origin.StartBackup(ctx, firstPath, &StartBackupArgs{Prefix: "tenants/"})
	if err != nil {
		t.Fatal(err)
	}
	directory := server.path("prefix-reader", "replicas")
	if err := os.MkdirAll(directory, 0o755); err != nil {
		t.Fatal(err)
	}
	_, err = reader.ReplicatePrefix(ctx, "tenants", &ReplicatePrefixArgs{To: directory})
	if err != nil {
		t.Fatal(err)
	}
	waitRows := func(path string, want int) {
		t.Helper()
		eventually(t, 30*time.Second, func() error {
			if _, err := os.Stat(path); err != nil {
				return err
			}
			output, err := sqlite(t, path, "SELECT count(*) FROM items")
			if err != nil {
				return err
			}
			if output != strconv.Itoa(want) {
				return fmt.Errorf("rows %s, want %d", output, want)
			}
			return nil
		})
	}
	waitRows(filepath.Join(directory, "first.db"), 2)
	secondPath := server.path("prefix-origin", "second.db")
	addRows(t, secondPath, 3)
	_, err = origin.StartBackup(ctx, secondPath, &StartBackupArgs{Prefix: "tenants/"})
	if err != nil {
		t.Fatal(err)
	}
	waitRows(filepath.Join(directory, "second.db"), 3)

	page, err := origin.StatusPage(ctx, "", 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Databases) != 1 || page.Next != "tenants/first.db" {
		t.Fatalf("first page: %+v", page)
	}
	last, err := origin.DatabasesPage(ctx, page.Next, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(last.Databases) != 1 || last.Databases[0].Name != "tenants/second.db" || last.Next != "" {
		t.Fatalf("last page: %+v", last)
	}
	// Names and rule IDs keep their meaning even after a source is renamed.
	rules, err := origin.Backups(ctx)
	if err != nil || len(rules) != 2 {
		t.Fatalf("backup rules: %v %v", rules, err)
	}
	if err := os.Rename(firstPath, firstPath+".moved"); err != nil {
		t.Fatal(err)
	}
	if _, err := origin.StopBackup(ctx, "tenants/first.db"); err != nil {
		t.Fatal(err)
	}
	remaining, err := origin.Backups(ctx)
	if err != nil || len(remaining) != 1 || remaining[0].Name != "tenants/second.db" {
		t.Fatalf("remaining rule: %v %v", remaining, err)
	}
	if _, err := origin.StopBackupRule(ctx, remaining[0].ID); err != nil {
		t.Fatal(err)
	}

	if err := reader.StopReplicaPrefix(ctx, "tenants", directory); err != nil {
		t.Fatal(err)
	}
	replicas, err := reader.Replicas(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(replicas.Prefixes) != 0 {
		t.Fatalf("prefixes after stop: %#v", replicas.Prefixes)
	}
	output, err := sqlite(t, filepath.Join(directory, "first.db"), "INSERT INTO items (body) VALUES ('local after stop')")
	if err != nil {
		t.Fatalf("writing stopped replica: %v %s", err, output)
	}
}

// TestForegroundJobs exercises the package-level entry point on a machine with
// credentials but no daemon. The reader uses an independent machine identity.
func TestForegroundJobs(t *testing.T) {
	server := startDevelopmentServer(t)
	reader := server.connect("verify")
	session := server.session("cron")
	token, err := server.joinToken(session)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv(TokenEnvironment, token)
	t.Setenv(SessionEnvironment, session)
	source := server.path("exports", "Å Team", "42.sqlite")
	addRows(t, source, 2)
	top := server.path("exports", "My DB.sqlite")
	addRows(t, top, 1)
	bad := server.path("exports", "Broken.sqlite")
	if err := os.WriteFile(bad, []byte("broken sqlite"), 0600); err != nil {
		t.Fatal(err)
	}
	ctx := t.Context()
	var missing *Error
	err = Backup(ctx, server.path("exports", "*.absent"), nil)
	if !errors.As(err, &missing) || missing.Code != "invalid_argument" {
		t.Fatalf("empty selection: %v", err)
	}
	err = Backup(ctx, server.path("exports", "**", "*.sqlite"), &BackupArgs{Prefix: "租户/", Wait: DurabilityServer})
	var failure *Error
	if !errors.As(err, &failure) || !strings.Contains(failure.Message, "file failures reported") || !strings.Contains(failure.Message, "Broken.sqlite") {
		t.Fatalf("partial failure: %v", err)
	}
	for _, name := range []string{"租户/My DB.sqlite", "租户/Å Team/42.sqlite"} {
		versions, err := reader.Versions(ctx, name, nil)
		if err != nil || len(versions) != 1 {
			t.Fatalf("%s versions %v: %v", name, versions, err)
		}
	}
	link := server.path("exports", "Linked.db")
	if err := os.Symlink(top, link); err != nil {
		t.Fatal(err)
	}
	if err := Backup(ctx, link, nil); err != nil {
		t.Fatal(err)
	}
	if versions, err := reader.Versions(ctx, "Linked.db", nil); err != nil || len(versions) != 1 {
		t.Fatalf("link versions %v: %v", versions, err)
	}
	job, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan error, 1)
	const exact = "Tenant/ E\u0301.sqlite "
	go func() { done <- Backup(job, source, &BackupArgs{Name: exact, Wait: DurabilityCloud}) }()
	eventually(t, 3*time.Second, func() error {
		writer, err := reader.Writer(ctx, exact)
		if err != nil {
			return err
		}
		if !writer.Active {
			return fmt.Errorf("waiting for writer")
		}
		return nil
	})
	if err := Backup(ctx, top, &BackupArgs{Name: exact}); !errors.Is(err, ErrBusy) {
		t.Fatalf("overlap: %v", err)
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel: %v", err)
	}
	writer, err := reader.Writer(ctx, exact)
	if err != nil || writer.Active {
		t.Fatalf("writer after cancellation %v: %v", writer, err)
	}
	if err := reader.Backup(ctx, top, &BackupArgs{Name: exact, Wait: DurabilityCloud}); err != nil {
		t.Fatal(err)
	}
	restored := server.path("restored", "exact.sqlite")
	if _, err := reader.Restore(ctx, exact, &RestoreArgs{To: restored}); err != nil {
		t.Fatal(err)
	}
	if count, err := sqlite(t, restored, "SELECT COUNT(*) FROM items"); err != nil || count != "1" {
		t.Fatalf("replacement contains %q rows: %v", count, err)
	}
	_, socket, err := SessionSocket(session)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(socket); !os.IsNotExist(err) {
		t.Fatalf("foreground session left socket: %v", err)
	}
}
