package litebackup

import (
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"testing"
	"time"
)

// fakeRequest is one request the fake daemon received.
type fakeRequest struct {
	Method string
	Path   string
	Query  map[string]string
	Body   map[string]any
	Header http.Header
}

// fakeDaemon answers on a unix socket like the litebackup daemon.
type fakeDaemon struct {
	t      *testing.T
	socket string

	mutex    sync.Mutex
	requests []fakeRequest
	routes   map[string]func(request *fakeRequest) (int, any)
}

// shortDirectory returns a temporary directory short enough for a unix socket path.
func shortDirectory(t *testing.T) string {
	t.Helper()
	directory, err := os.MkdirTemp("", "lb")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(directory); err != nil {
			t.Error(err)
		}
	})
	return directory
}

// newFakeDaemon starts a fake daemon whose routes answer by method and path.
func newFakeDaemon(t *testing.T) *fakeDaemon {
	t.Helper()
	daemon := &fakeDaemon{t: t, socket: filepath.Join(shortDirectory(t), "client.sock"), routes: map[string]func(request *fakeRequest) (int, any){}}
	daemon.route("GET /v1/status/show", func(*fakeRequest) (int, any) {
		return http.StatusOK, map[string]any{"session": "unit", "server": "http://example", "client_version": "1.0.0", "session_id": "s", "watch": "inotify", "databases": []any{}}
	})
	listener, err := net.Listen("unix", daemon.socket)
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: daemon}
	go func() {
		if serveErr := server.Serve(listener); serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
			t.Error(serveErr)
		}
	}()
	t.Cleanup(func() {
		if err := server.Close(); err != nil {
			t.Error(err)
		}
	})
	return daemon
}

// route sets the answer for "METHOD /path".
func (d *fakeDaemon) route(key string, answer func(request *fakeRequest) (int, any)) {
	d.mutex.Lock()
	defer d.mutex.Unlock()
	d.routes[key] = answer
}

// ServeHTTP records the request and answers from its route.
func (d *fakeDaemon) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	recorded := fakeRequest{Method: request.Method, Path: request.URL.Path, Query: map[string]string{}, Header: request.Header.Clone()}
	for key, values := range request.URL.Query() {
		recorded.Query[key] = values[0]
	}
	content, err := io.ReadAll(request.Body)
	if err != nil {
		d.t.Error(err)
	}
	if len(content) > 0 {
		if err := json.Unmarshal(content, &recorded.Body); err != nil {
			d.t.Errorf("%s: body %q: %v", request.URL.Path, content, err)
		}
	}
	d.mutex.Lock()
	d.requests = append(d.requests, recorded)
	answer := d.routes[request.Method+" "+request.URL.Path]
	d.mutex.Unlock()
	writer.Header().Set("X-Litebackup-Client-Version", "1.0.0")
	writer.Header().Set("Content-Type", "application/json")
	if answer == nil {
		writer.WriteHeader(http.StatusNotFound)
		if _, err := writer.Write([]byte(`{"code":"not_found","message":"no route ` + request.URL.Path + `"}`)); err != nil {
			d.t.Error(err)
		}
		return
	}
	status, body := answer(&recorded)
	encoded, err := json.Marshal(body)
	if err != nil {
		d.t.Error(err)
	}
	writer.WriteHeader(status)
	if _, err := writer.Write(encoded); err != nil {
		d.t.Error(err)
	}
}

// received returns the requests to one path.
func (d *fakeDaemon) received(path string) []fakeRequest {
	d.mutex.Lock()
	defer d.mutex.Unlock()
	var matching []fakeRequest
	for _, request := range d.requests {
		if request.Path == path {
			matching = append(matching, request)
		}
	}
	return matching
}

// connect connects a client to the fake daemon.
func (d *fakeDaemon) connect(t *testing.T) *Client {
	t.Helper()
	client, err := Connect(t.Context(), &ConnectArgs{SocketPath: d.socket})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := client.Close(); err != nil {
			t.Error(err)
		}
	})
	return client
}

// databaseStatus is a canned DatabaseStatus answer.
func databaseStatus(name string, role string, state string, server int64, cloud int64) map[string]any {
	return map[string]any{
		"name": name, "path": "/var/lib/app/" + name, "role": role, "state": state, "acquired": false, "failover": false,
		"local_changed": false, "synced_server": map[string]any{"version": server}, "synced_cloud": map[string]any{"version": cloud},
		"journal_mode": "wal", "page_count": 2, "sync_count": 1, "hint_misses": 0,
	}
}

func TestVersionHeaderAndErrors(t *testing.T) {
	daemon := newFakeDaemon(t)
	daemon.route("POST /v1/backup-rules/start", func(*fakeRequest) (int, any) {
		return http.StatusBadRequest, map[string]any{"code": "invalid_argument", "message": "invalid destination"}
	})
	daemon.route("POST /v1/databases/release", func(*fakeRequest) (int, any) {
		return http.StatusUpgradeRequired, map[string]any{"code": "sdk_unsupported", "message": "update the SDK"}
	})
	client := daemon.connect(t)
	if client.ClientVersion() != "1.0.0" {
		t.Fatalf("client version %q", client.ClientVersion())
	}
	_, err := client.StartBackup(t.Context(), "/var/lib/app/main.db", nil)
	var answered *Error
	if !errors.As(err, &answered) || answered.Code != "invalid_argument" || answered.Status != http.StatusBadRequest || !strings.Contains(answered.Message, "destination") {
		t.Fatalf("backup error %#v", err)
	}
	if !IsCode(err, "invalid_argument") {
		t.Fatal("IsCode missed invalid_argument")
	}
	_, err = client.Release(t.Context(), "main.db", nil)
	if !IsCode(err, "sdk_unsupported") {
		t.Fatalf("release error %v", err)
	}
	for _, path := range []string{"/v1/status/show", "/v1/backup-rules/start", "/v1/databases/release"} {
		for _, request := range daemon.received(path) {
			if got := request.Header.Get("X-Litebackup-SDK-Version"); got != "go/"+SDKVersion {
				t.Fatalf("%s sent SDK version %q", path, got)
			}
		}
	}
}

func TestStartBackup(t *testing.T) {
	daemon := newFakeDaemon(t)
	daemon.route("POST /v1/backup-rules/start", func(request *fakeRequest) (int, any) {
		return 200, map[string]any{"id": "b1", "path": request.Body["path"]}
	})
	client := daemon.connect(t)
	dir := t.TempDir()
	t.Chdir(dir)
	rule, err := client.StartBackup(t.Context(), "My App.sqlite", &StartBackupArgs{Prefix: "É/"})
	if err != nil || rule.ID != "b1" || rule.Path != filepath.Join(dir, "My App.sqlite") {
		t.Fatalf("rule %v %v", rule, err)
	}
}

func TestRestoreVersionsReplicateAcquire(t *testing.T) {
	daemon := newFakeDaemon(t)
	daemon.route("GET /v1/databases/versions", func(request *fakeRequest) (int, any) {
		if request.Query["after"] == "" {
			return http.StatusOK, map[string]any{"versions": []any{map[string]any{"version": 1, "state_digest": "aa", "time": "2026-11-15T09:30:00Z"}}, "next": 1}
		}
		return http.StatusOK, map[string]any{"versions": []any{map[string]any{"version": 2, "state_digest": "bb", "time": "2026-11-15T09:31:00Z"}}}
	})
	daemon.route("POST /v1/databases/restore", func(request *fakeRequest) (int, any) {
		return http.StatusOK, map[string]any{"name": request.Body["name"], "path": request.Body["path"], "version": 2, "page_count": 3, "page_size": 4096, "state_digest": "bb"}
	})
	daemon.route("POST /v1/replicas/start", func(request *fakeRequest) (int, any) {
		return http.StatusOK, map[string]any{"database": databaseStatus("app/main.db", "replica", "syncing", 2, 2)}
	})
	daemon.route("POST /v1/databases/acquire", func(request *fakeRequest) (int, any) {
		return http.StatusOK, map[string]any{"database": databaseStatus("tenants/42.db", "origin", "syncing", 0, 0), "outcome": "created"}
	})
	client := daemon.connect(t)

	versions, err := client.Versions(t.Context(), "app/main.db", nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(versions) != 2 || versions[1].Version != 2 || versions[1].StateDigest != "bb" || versions[0].Time.IsZero() {
		t.Fatalf("versions %+v", versions)
	}
	if after := daemon.received("/v1/databases/versions")[1].Query["after"]; after != "1" {
		t.Fatalf("second page after %q", after)
	}
	at := time.Date(2026, 11, 15, 9, 30, 30, 0, time.UTC)
	restored, err := client.Restore(t.Context(), "app/main.db", &RestoreArgs{To: "/tmp/restored.db", Time: at})
	if err != nil {
		t.Fatal(err)
	}
	if restored.PageSize != 4096 || restored.StateDigest != "bb" {
		t.Fatalf("restored %+v", restored)
	}
	if body := daemon.received("/v1/databases/restore")[0].Body; body["time"] != "2026-11-15T09:30:30Z" || body["path"] != "/tmp/restored.db" {
		t.Fatalf("restore body %v", body)
	}
	if _, err := client.Restore(t.Context(), "app/main.db", &RestoreArgs{To: "/tmp/x.db", Version: 1, Time: at}); err == nil {
		t.Fatal("a restore with a version and a time passed")
	}
	replica, err := client.Replicate(t.Context(), "app/main.db", &ReplicateArgs{To: "/var/lib/replica/main.db", WriteProtection: WriteProtectionDetect, Timeout: 5 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	if replica.Role != RoleReplica || replica.SyncedServer.Version != 2 {
		t.Fatalf("replica %+v", replica)
	}
	body := daemon.received("/v1/replicas/start")[0].Body
	if body["to"] != "/var/lib/replica/main.db" || body["write_protection"] != "detect" || body["timeout_millis"] != float64(5000) {
		t.Fatalf("replicate body %v", body)
	}
	acquired, err := client.Acquire(t.Context(), "tenants/42.db", &AcquireArgs{To: "/var/lib/app/42.db", OffloadReleased: true, BlockOnLost: true})
	if err != nil {
		t.Fatal(err)
	}
	if acquired.Outcome != "created" {
		t.Fatalf("acquired %+v", acquired)
	}
	body = daemon.received("/v1/databases/acquire")[0].Body
	if body["offload"] != "released" || body["on_lost"] != "block" || body["path"] != "/var/lib/app/42.db" {
		t.Fatalf("acquire body %v", body)
	}
}

func TestFailoverEvents(t *testing.T) {
	daemon := newFakeDaemon(t)
	pages := make(chan map[string]any, 4)
	daemon.route("GET /v1/events/list", func(request *fakeRequest) (int, any) {
		after, _ := strconv.ParseInt(request.Query["after"], 10, 64)
		if request.Query["wait_millis"] == "" {
			// The cursor read before the start request asks where the log ends.
			if request.Query["after"] != "-1" {
				t.Errorf("the cursor read asked after=%q, want -1", request.Query["after"])
			}
			return http.StatusOK, map[string]any{"events": []any{}, "next": 7}
		}
		select {
		case page := <-pages:
			return http.StatusOK, page
		case <-time.After(200 * time.Millisecond):
			return http.StatusOK, map[string]any{"events": []any{}, "next": after}
		}
	})
	daemon.route("POST /v1/failovers/start", func(request *fakeRequest) (int, any) {
		return http.StatusOK, map[string]any{
			"name": "app/main.db", "path": request.Body["path"], "role": "replica",
			"database": databaseStatus("app/main.db", "replica", "waiting", 3, 3),
			"writer":   map[string]any{"name": "app/main.db", "active": true, "this_machine": false, "machine": "web-1", "lapsed": false},
		}
	})
	daemon.route("POST /v1/backup-rules/stop", func(*fakeRequest) (int, any) { return http.StatusOK, map[string]any{} })
	client := daemon.connect(t)

	roles := make(chan Role, 4)
	saved := make(chan string, 4)
	failover, err := client.Failover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
		OnRoleChange:    func(role Role) { roles <- role },
		OnConflictSaved: func(path string) { saved <- path },
	})
	if err != nil {
		t.Fatal(err)
	}
	body := daemon.received("/v1/failovers/start")[0].Body
	if _, found := body["takeover"]; found || body["path"] != "/var/lib/app/main.db" {
		t.Fatalf("start body %v", body)
	}
	if failover.Role() != RoleReplica || failover.IsOrigin() {
		t.Fatalf("role %s", failover.Role())
	}

	pages <- map[string]any{"events": []any{
		map[string]any{"id": 8, "time": "2026-11-15T09:01:00Z", "type": "role_changed", "message": "origin", "name": "other.db", "role": "origin"},
		map[string]any{"id": 9, "time": "2026-11-15T09:01:00Z", "type": "role_changed", "message": "origin", "name": "app/main.db", "role": "origin"},
	}, "next": 9}
	if role := receive(t, roles); role != RoleOrigin {
		t.Fatalf("role change %s", role)
	}
	if !failover.IsOrigin() {
		t.Fatal("not the origin after role_changed")
	}
	pages <- map[string]any{"events": []any{
		map[string]any{"id": 10, "time": "2026-11-15T09:02:00Z", "type": "role_changed", "message": "replica", "name": "app/main.db", "role": "replica"},
		map[string]any{"id": 11, "time": "2026-11-15T09:02:00Z", "type": "conflict_saved", "message": "/var/lib/app/main.db.conflict-1.sqlite", "name": "app/main.db", "path": "/var/lib/app/main.db.conflict-1.sqlite"},
	}, "next": 11}
	if role := receive(t, roles); role != RoleReplica {
		t.Fatalf("role change %s", role)
	}
	if path := receive(t, saved); path != "/var/lib/app/main.db.conflict-1.sqlite" {
		t.Fatalf("conflict saved %q", path)
	}
	for _, request := range daemon.received("/v1/events/list") {
		after, _ := strconv.ParseInt(request.Query["after"], 10, 64)
		if request.Query["wait_millis"] != "" && after < 7 {
			t.Fatalf("long-poll started at after=%s, before the cursor", request.Query["after"])
		}
	}

	if err := failover.Stop(t.Context()); err != nil {
		t.Fatal(err)
	}
	if body := daemon.received("/v1/backup-rules/stop")[0].Body; body["name"] != "app/main.db" {
		t.Fatalf("stop body %v", body)
	}
}

// receive waits for one value.
func receive[T any](t *testing.T, values <-chan T) T {
	t.Helper()
	select {
	case value := <-values:
		return value
	case <-time.After(5 * time.Second):
		t.Fatal("timed out")
		var zero T
		return zero
	}
}

func TestSessionSocket(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("HOME", filepath.Join(directory, "user"))
	t.Setenv("XDG_RUNTIME_DIR", filepath.Join(directory, "home"))
	t.Setenv(SessionEnvironment, "")
	configuration := filepath.Join(directory, "client.json")
	previous := SystemConfigurationPath
	SystemConfigurationPath = configuration
	t.Cleanup(func() { SystemConfigurationPath = previous })

	session, socket, err := SessionSocket("")
	if err != nil {
		t.Fatal(err)
	}
	if session != DefaultSession || socket != filepath.Join(directory, "home", "litebackup", "default", "client.sock") {
		t.Fatalf("session %s socket %s", session, socket)
	}
	content, err := json.Marshal(map[string]any{"user": "app", "uid": os.Getuid(), "state_directory": "/var/lib/litebackup-client", "runtime_directory": filepath.Join(directory, "run")})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(configuration, content, 0o600); err != nil {
		t.Fatal(err)
	}
	_, socket, err = SessionSocket("reports")
	if err != nil {
		t.Fatal(err)
	}
	if socket != filepath.Join(directory, "run", "reports", "client.sock") {
		t.Fatalf("service socket %s", socket)
	}
	if _, _, err := SessionSocket("Bad Name"); err == nil {
		t.Fatal("an invalid session name passed")
	}
}

func TestBinaryInstallCommand(t *testing.T) {
	home := t.TempDir()
	t.Setenv(BinaryPathEnvironment, "")
	t.Setenv("PATH", t.TempDir())
	t.Setenv("HOME", home)
	_, err := Binary()
	if err == nil || !strings.Contains(err.Error(), "curl -fsSL https://litebackup.com/cli.sh | bash") {
		t.Fatalf("error %v", err)
	}
	local := filepath.Join(home, ".local", "bin", "litebackup")
	if err := os.MkdirAll(filepath.Dir(local), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(local, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	found, err := Binary()
	if err != nil || found != local {
		t.Fatalf("a user install in ~/.local/bin: %s %v", found, err)
	}
	t.Setenv(BinaryPathEnvironment, "/opt/litebackup")
	binary, err := Binary()
	if err != nil || binary != "/opt/litebackup" {
		t.Fatalf("binary %s %v", binary, err)
	}
}

func TestReplicatePrefix(t *testing.T) {
	daemon := newFakeDaemon(t)
	daemon.route("POST /v1/replicas/start", func(request *fakeRequest) (int, any) {
		return http.StatusOK, map[string]any{"prefix": map[string]any{"id": "p1", "prefix": request.Body["prefix"], "to": request.Body["to"], "write_protection": "lock", "databases": 0}}
	})
	daemon.route("POST /v1/replicas/stop", func(request *fakeRequest) (int, any) {
		if request.Body["prefix"] != "tenants" || request.Body["to"] != filepath.Join(mustWorkingDirectory(t), "replicas") {
			t.Errorf("stop body: %#v", request.Body)
		}
		return http.StatusOK, map[string]any{}
	})
	client, err := Connect(t.Context(), &ConnectArgs{SocketPath: daemon.socket})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	result, err := client.ReplicatePrefix(t.Context(), "tenants", &ReplicatePrefixArgs{To: "replicas", WriteProtection: WriteProtectionLock})
	if err != nil {
		t.Fatal(err)
	}
	if result.ID != "p1" || result.To != filepath.Join(mustWorkingDirectory(t), "replicas") {
		t.Fatalf("prefix: %#v", result)
	}
	if err := client.StopReplicaPrefix(t.Context(), "tenants", "replicas"); err != nil {
		t.Fatal(err)
	}
	result, err = client.ReplicatePrefix(t.Context(), "", &ReplicatePrefixArgs{To: "all"})
	if err != nil || result.Prefix != "" {
		t.Fatalf("all databases: %#v, %v", result, err)
	}
	if _, err := client.ReplicatePrefix(t.Context(), "tenants", nil); !IsCode(err, "invalid_argument") {
		t.Fatalf("empty directory: %v", err)
	}
	if err := client.StopReplicaPrefix(t.Context(), "tenants", ""); !IsCode(err, "invalid_argument") {
		t.Fatalf("empty stop directory: %v", err)
	}
	daemon.route("POST /v1/replicas/start", func(*fakeRequest) (int, any) { return http.StatusOK, map[string]any{} })
	if _, err := client.ReplicatePrefix(t.Context(), "tenants", &ReplicatePrefixArgs{To: "replicas"}); !IsCode(err, "invalid_response") {
		t.Fatalf("missing prefix: %v", err)
	}
	daemon.route("POST /v1/replicas/start", func(*fakeRequest) (int, any) {
		return http.StatusForbidden, map[string]any{"code": "forbidden", "message": "namespace not allowed"}
	})
	if _, err := client.ReplicatePrefix(t.Context(), "tenants", &ReplicatePrefixArgs{To: "replicas"}); !IsCode(err, "forbidden") {
		t.Fatalf("forbidden: %v", err)
	}
}

func mustWorkingDirectory(t *testing.T) string {
	t.Helper()
	path, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	return path
}

func TestSessionSocketUsesUserService(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("HOME", directory)
	t.Setenv("XDG_RUNTIME_DIR", "")
	t.Setenv(SessionEnvironment, "")
	previous := SystemConfigurationPath
	SystemConfigurationPath = filepath.Join(directory, "client.json")
	t.Cleanup(func() { SystemConfigurationPath = previous })
	record := filepath.Join(directory, ".config", "litebackup", "service.json")
	if err := os.MkdirAll(filepath.Dir(record), 0o700); err != nil {
		t.Fatal(err)
	}
	content, err := json.Marshal(map[string]any{"session": "default", "state_directory": "/var/lib/app/state/litebackup", "runtime_directory": "/run/user/1000/litebackup"})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(record, content, 0o644); err != nil {
		t.Fatal(err)
	}
	_, socket, err := SessionSocket("")
	if err != nil {
		t.Fatal(err)
	}
	if socket != "/run/user/1000/litebackup/default/client.sock" {
		t.Fatalf("user service socket %s", socket)
	}
	for _, name := range []string{"..", ".", ".hidden"} {
		if _, _, err := SessionSocket(name); err == nil {
			t.Fatalf("session %q passed", name)
		}
	}
}
