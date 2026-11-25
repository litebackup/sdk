package litebackup

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// leaseDaemon answers a pool's requests for one leased or backed-up database:
// it logs each database request as a step, answers events with what
// queueEvent queued, and keeps the database's state for status reads.
type leaseDaemon struct {
	*contractDaemon
	steps *scopeSteps
	// status is the database's status JSON, without braces' neighbours.
	mutex  sync.Mutex
	status map[string]any
	events []map[string]any
	nextID int64
	// failRelease makes release answer unavailable.
	failRelease atomic.Bool
	// rules is what rules.list answers.
	rules []map[string]any
}

func newLeaseDaemon(t *testing.T, status map[string]any) *leaseDaemon {
	t.Helper()
	daemon := &leaseDaemon{steps: &scopeSteps{}, status: status}
	version := "1.6.0"
	daemon.contractDaemon = newContractDaemon(t, &version, daemon.answer)
	return daemon
}

func (d *leaseDaemon) answer(received receivedRequest) (int, string, []byte) {
	if received.Path == "/v1/events/list" {
		d.mutex.Lock()
		events := d.events
		d.events = nil
		next := d.nextID
		d.mutex.Unlock()
		if len(events) == 0 {
			time.Sleep(20 * time.Millisecond)
		}
		return d.encode(map[string]any{"events": append([]map[string]any{}, events...), "next": next})
	}
	if received.Path == "/v1/backup-rules/list" {
		d.mutex.Lock()
		defer d.mutex.Unlock()
		return d.encode(map[string]any{"rules": append([]map[string]any{}, d.rules...)})
	}
	step := strings.TrimPrefix(received.Path, "/v1/databases/")
	d.steps.add(step)
	if step == "release" && d.failRelease.Load() {
		return http.StatusServiceUnavailable, "application/json", []byte(`{"code":"unavailable","message":"release failed"}`)
	}
	d.mutex.Lock()
	defer d.mutex.Unlock()
	switch step {
	case "acquire":
		d.status["state"] = "syncing"
		return d.encode(map[string]any{"database": d.status, "decision": "downloaded"})
	case "release":
		return d.encode(map[string]any{"database": d.status})
	}
	return d.encode(d.status)
}

func (d *leaseDaemon) encode(answer any) (int, string, []byte) {
	encoded, err := json.Marshal(answer)
	if err != nil {
		panic(err)
	}
	return http.StatusOK, "application/json", encoded
}

// queueEvent makes the next events page carry an event of eventType.
func (d *leaseDaemon) queueEvent(eventType EventType, name string) {
	d.mutex.Lock()
	defer d.mutex.Unlock()
	d.nextID++
	d.events = append(d.events, map[string]any{
		"id": d.nextID, "name": name, "type": string(eventType),
		"time": time.Now().UTC().Format(time.RFC3339Nano), "message": "",
	})
}

// acquiredStatus is the status of a database this machine acquired.
func acquiredStatus(path string) map[string]any {
	return map[string]any{
		"name": "agents/7.sqlite", "path": path, "role": "origin", "state": "synced",
		"acquired": true, "failover": false, "local_changed": false,
		"synced_server": map[string]any{"version": 3}, "synced_cloud": map[string]any{"version": 3},
		"journal_mode": "wal", "page_count": 2, "sync_count": 3, "hint_misses": 0,
	}
}

// TestLeaseKeepsLockBetweenCalls runs three leases on one database: only the
// first acquires and opens, later ones check the lock, a lease with
// durability syncs without releasing,
// and Close closes and releases once.
func TestLeaseKeepsLockBetweenCalls(t *testing.T) {
	path := filepath.Join(t.TempDir(), "7.sqlite")
	daemon := newLeaseDaemon(t, acquiredStatus(path))
	client := daemon.connect(t)
	adapter := &stepLoggingAdapter{steps: daemon.steps}
	pool, err := NewPool(client, &PoolArgs[string]{Adapter: adapter})
	if err != nil {
		t.Fatal(err)
	}
	for _, durability := range []Durability{"", DurabilityServer, ""} {
		err := pool.Lease(t.Context(), "agents/7.sqlite", &LeaseArgs{Path: path, Durability: durability},
			func(context.Context, string) error {
				daemon.steps.add("work")
				return nil
			})
		if err != nil {
			t.Fatal(err)
		}
	}
	if err := pool.Close(t.Context()); err != nil {
		t.Fatal(err)
	}
	want := []string{"acquire", "open", "work", "status", "work", "sync", "status", "work", "close", "release"}
	if got := daemon.steps.all(); !reflect.DeepEqual(got, want) {
		t.Fatalf("steps %v, want %v", got, want)
	}
}

// TestLeaseDetectsLostLock marks a leased database conflicted between two
// calls: the next call fails with lock_lost without running its work and
// closes the handle, and the call after it acquires again.
func TestLeaseDetectsLostLock(t *testing.T) {
	path := filepath.Join(t.TempDir(), "7.sqlite")
	daemon := newLeaseDaemon(t, acquiredStatus(path))
	client := daemon.connect(t)
	pool, err := NewPool(client, &PoolArgs[string]{Adapter: &stepLoggingAdapter{steps: daemon.steps}})
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close(t.Context())
	work := func(context.Context, string) error {
		daemon.steps.add("work")
		return nil
	}
	if err := pool.Lease(t.Context(), "agents/7.sqlite", &LeaseArgs{Path: path}, work); err != nil {
		t.Fatal(err)
	}
	daemon.mutex.Lock()
	daemon.status["state"] = string(DatabaseStatusStateConflicted)
	daemon.mutex.Unlock()
	if err := pool.Lease(t.Context(), "agents/7.sqlite", &LeaseArgs{Path: path}, work); !IsCode(err, CodeLockLost) {
		t.Fatalf("a lease whose lock was lost: %v, want lock_lost", err)
	}
	if err := pool.Lease(t.Context(), "agents/7.sqlite", &LeaseArgs{Path: path}, work); err != nil {
		t.Fatal(err)
	}
	want := []string{"acquire", "open", "work", "status", "close", "acquire", "open", "work"}
	if got := daemon.steps.all(); !reflect.DeepEqual(got, want) {
		t.Fatalf("steps %v, want %v", got, want)
	}
}

// TestLeaseReopensAfterFailedRelease fails a release after its close
// succeeded: the lease stays, and the next call reopens the handle before
// its work.
func TestLeaseReopensAfterFailedRelease(t *testing.T) {
	path := filepath.Join(t.TempDir(), "7.sqlite")
	daemon := newLeaseDaemon(t, acquiredStatus(path))
	client := daemon.connect(t)
	pool, err := NewPool(client, &PoolArgs[string]{Adapter: &stepLoggingAdapter{steps: daemon.steps}})
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close(t.Context())
	var handles []string
	work := func(_ context.Context, db string) error {
		handles = append(handles, db)
		return nil
	}
	if err := pool.Lease(t.Context(), "agents/7.sqlite", &LeaseArgs{Path: path}, work); err != nil {
		t.Fatal(err)
	}
	daemon.failRelease.Store(true)
	var cleanup *LeaseCleanupError
	if err := pool.Release(t.Context(), "agents/7.sqlite"); !errors.As(err, &cleanup) ||
		cleanup.Step != LeaseCleanupStepRelease {
		t.Fatalf("a failed release: %v, want a release LeaseCleanupError", err)
	}
	daemon.failRelease.Store(false)
	if err := pool.Lease(t.Context(), "agents/7.sqlite", &LeaseArgs{Path: path}, work); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(handles, []string{"connection", "connection"}) {
		t.Fatalf("the calls got handles %q, want an open one each time", handles)
	}
	want := []string{"acquire", "open", "close", "release", "status", "open"}
	if got := daemon.steps.all(); !reflect.DeepEqual(got, want) {
		t.Fatalf("steps %v, want %v", got, want)
	}
}

// TestCloseAwaitsRunningUse closes the pool while a Use call runs: the
// handle is closed only after the call returns, and a later call is refused.
func TestCloseAwaitsRunningUse(t *testing.T) {
	path := filepath.Join(t.TempDir(), "42.sqlite")
	if err := os.WriteFile(path, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	daemon := newLeaseDaemon(t, ruleStatus(path, "synced"))
	client := daemon.connect(t)
	pool, err := NewPool(client, &PoolArgs[string]{Adapter: &stepLoggingAdapter{steps: daemon.steps}})
	if err != nil {
		t.Fatal(err)
	}
	started, finish := make(chan struct{}), make(chan struct{})
	go func() {
		_ = pool.Use(t.Context(), "tenants/42.sqlite", func(context.Context, string) error {
			close(started)
			<-finish
			daemon.steps.add("work done")
			return nil
		})
	}()
	<-started
	closed := make(chan error, 1)
	go func() { closed <- pool.Close(t.Context()) }()
	time.Sleep(100 * time.Millisecond)
	close(finish)
	if err := <-closed; err != nil {
		t.Fatal(err)
	}
	steps := daemon.steps.all()
	if len(steps) < 2 || steps[len(steps)-2] != "work done" || steps[len(steps)-1] != "close" {
		t.Fatalf("steps %v, want the close after the running call", steps)
	}
	if err := pool.Use(t.Context(), "tenants/42.sqlite", func(context.Context, string) error {
		return nil
	}); !IsCode(err, CodeClosed) {
		t.Fatalf("a call after Close: %v, want closed", err)
	}
}

// TestLeaseIdleRelease releases a lease no call used for IdleRelease, and
// keeps one when IdleRelease is negative.
func TestLeaseIdleRelease(t *testing.T) {
	previous := poolCheckInterval
	poolCheckInterval = 10 * time.Millisecond
	t.Cleanup(func() { poolCheckInterval = previous })
	for _, test := range []struct {
		name        string
		idleRelease time.Duration
		released    bool
	}{
		{name: "released", idleRelease: 50 * time.Millisecond, released: true},
		{name: "kept", idleRelease: -1, released: false},
	} {
		t.Run(test.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "7.sqlite")
			daemon := newLeaseDaemon(t, acquiredStatus(path))
			client := daemon.connect(t)
			pool, err := NewPool(client, &PoolArgs[string]{
				Adapter:     &stepLoggingAdapter{steps: daemon.steps},
				IdleRelease: test.idleRelease,
			})
			if err != nil {
				t.Fatal(err)
			}
			defer pool.Close(t.Context())
			if err := pool.Lease(t.Context(), "agents/7.sqlite", &LeaseArgs{Path: path},
				func(context.Context, string) error { return nil }); err != nil {
				t.Fatal(err)
			}
			time.Sleep(300 * time.Millisecond)
			released := strings.Contains(strings.Join(daemon.steps.all(), " "), "release")
			if released != test.released {
				t.Fatalf("steps %v: released %v, want %v", daemon.steps.all(), released, test.released)
			}
		})
	}
}

// TestLeaseGoexit checks that work ending with runtime.Goexit, as t.SkipNow
// and t.FailNow do, keeps exiting, and that the release Close runs does not
// offload.
func TestLeaseGoexit(t *testing.T) {
	path := filepath.Join(t.TempDir(), "7.sqlite")
	daemon := newLeaseDaemon(t, acquiredStatus(path))
	client := daemon.connect(t)
	pool, err := NewPool(client, &PoolArgs[string]{Adapter: &stepLoggingAdapter{steps: daemon.steps}})
	if err != nil {
		t.Fatal(err)
	}
	returned := false
	t.Run("skipped work", func(t *testing.T) {
		_ = pool.Lease(t.Context(), "agents/7.sqlite", &LeaseArgs{Path: path, Offload: true},
			func(context.Context, string) error {
				t.SkipNow()
				return nil
			})
		returned = true
	})
	if returned {
		t.Fatal("Lease returned after the work called runtime.Goexit")
	}
	if err := pool.Close(t.Context()); err != nil {
		t.Fatal(err)
	}
	for _, received := range daemon.requests() {
		if received.Path == "/v1/databases/release" &&
			strings.Contains(string(received.Body), "offload") {
			t.Fatalf("released with offload after Goexit: %s", received.Body)
		}
	}
}

// slowCloseAdapter takes delay to close.
type slowCloseAdapter struct{ delay time.Duration }

func (slowCloseAdapter) Open(context.Context, string) (string, error) { return "db", nil }

func (a slowCloseAdapter) Close(context.Context, string) error {
	time.Sleep(a.delay)
	return nil
}

// TestLeaseCleanupTimeoutIsShared checks that the release Close runs gets
// what closing left of the cleanup timeout.
func TestLeaseCleanupTimeoutIsShared(t *testing.T) {
	path := filepath.Join(t.TempDir(), "7.sqlite")
	daemon := newLeaseDaemon(t, acquiredStatus(path))
	client := daemon.connect(t)
	pool, err := NewPool(client, &PoolArgs[string]{
		Adapter:        slowCloseAdapter{delay: 500 * time.Millisecond},
		CleanupTimeout: 2 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := pool.Lease(t.Context(), "agents/7.sqlite", &LeaseArgs{Path: path},
		func(context.Context, string) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if err := pool.Close(t.Context()); err != nil {
		t.Fatal(err)
	}
	for _, received := range daemon.requests() {
		if received.Path != "/v1/databases/release" {
			continue
		}
		var body struct {
			TimeoutMillis int64 `json:"timeout_millis"`
		}
		if err := json.Unmarshal(received.Body, &body); err != nil {
			t.Fatal(err)
		}
		if body.TimeoutMillis < 1000 || body.TimeoutMillis > 1500 {
			t.Fatalf("release timeout %d ms, want what the 500 ms close left of 2 s", body.TimeoutMillis)
		}
		return
	}
	t.Fatal("no release")
}

// ruleStatus is the status of a backup rule's database.
func ruleStatus(path string, state string) map[string]any {
	status := acquiredStatus(path)
	status["name"] = "tenants/42.sqlite"
	status["acquired"] = false
	status["state"] = state
	status["backup_rule_id"] = "rule-1"
	return status
}

// TestUseBringsBackAndKeepsOpen uses an offloaded rule database from several
// goroutines: the pool brings it back once, opens it once, and keeps it open,
// and refuses Use for an acquired database and Lease for the rule's one.
func TestUseBringsBackAndKeepsOpen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "42.sqlite")
	daemon := newLeaseDaemon(t, ruleStatus(path, string(DatabaseStatusStateOffloaded)))
	client := daemon.connect(t)
	adapter := &stepLoggingAdapter{steps: daemon.steps}
	pool, err := NewPool(client, &PoolArgs[string]{Adapter: adapter})
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close(t.Context())
	var calls atomic.Int32
	var group sync.WaitGroup
	for range 4 {
		group.Go(func() {
			if err := pool.Use(t.Context(), "tenants/42.sqlite", func(context.Context, string) error {
				calls.Add(1)
				return nil
			}); err != nil {
				t.Error(err)
			}
		})
	}
	group.Wait()
	if calls.Load() != 4 {
		t.Fatalf("%d calls ran, want 4", calls.Load())
	}
	opened := 0
	acquired := 0
	for _, step := range daemon.steps.all() {
		switch step {
		case "open":
			opened++
		case "acquire":
			acquired++
		}
	}
	if opened != 1 || acquired != 1 {
		t.Fatalf("steps %v, want one acquire and one open", daemon.steps.all())
	}
	err = pool.Lease(t.Context(), "tenants/42.sqlite", nil, func(context.Context, string) error { return nil })
	if !IsCode(err, CodeInvalidArgument) {
		t.Fatalf("leasing a rule's database the pool uses: %v, want invalid_argument", err)
	}
	daemon.mutex.Lock()
	daemon.status["name"] = "agents/7.sqlite"
	daemon.status["acquired"] = true
	delete(daemon.status, "backup_rule_id")
	daemon.mutex.Unlock()
	err = pool.Use(t.Context(), "agents/7.sqlite", func(context.Context, string) error { return nil })
	if !IsCode(err, CodeInvalidArgument) {
		t.Fatalf("using an acquired database: %v, want invalid_argument", err)
	}
}

// TestUseClosesIdleOnlyWhenRuleOffloads keeps a rule database without a
// local limit open while idle, and closes one whose rule offloads after
// IdleClose.
func TestUseClosesIdleOnlyWhenRuleOffloads(t *testing.T) {
	previous := poolCheckInterval
	poolCheckInterval = 10 * time.Millisecond
	t.Cleanup(func() { poolCheckInterval = previous })
	for _, test := range []struct {
		name      string
		idleClose time.Duration
		closed    bool
	}{
		{name: "kept", idleClose: 0, closed: false},
		{name: "closed", idleClose: 50 * time.Millisecond, closed: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "42.sqlite")
			if err := os.WriteFile(path, nil, 0o644); err != nil {
				t.Fatal(err)
			}
			daemon := newLeaseDaemon(t, ruleStatus(path, "synced"))
			client := daemon.connect(t)
			pool, err := NewPool(client, &PoolArgs[string]{
				Adapter:   &stepLoggingAdapter{steps: daemon.steps},
				IdleClose: test.idleClose,
			})
			if err != nil {
				t.Fatal(err)
			}
			defer pool.Close(t.Context())
			if err := pool.Use(t.Context(), "tenants/42.sqlite", func(context.Context, string) error {
				return nil
			}); err != nil {
				t.Fatal(err)
			}
			time.Sleep(300 * time.Millisecond)
			closed := strings.Contains(strings.Join(daemon.steps.all(), " "), "close")
			if closed != test.closed {
				t.Fatalf("steps %v: closed %v, want %v", daemon.steps.all(), closed, test.closed)
			}
		})
	}
}

// TestUseClosesRollbackJournalAfterUse closes a rollback-journal database
// after each call when its rule offloads such databases, and keeps it open
// without the rule's consent or for a WAL database.
func TestUseClosesRollbackJournalAfterUse(t *testing.T) {
	for _, test := range []struct {
		name        string
		journalMode string
		consent     bool
		closed      bool
	}{
		{name: "rollback with consent", journalMode: "rollback", consent: true, closed: true},
		{name: "rollback without consent", journalMode: "rollback", consent: false, closed: false},
		{name: "WAL with consent", journalMode: "wal", consent: true, closed: false},
	} {
		t.Run(test.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "42.sqlite")
			if err := os.WriteFile(path, nil, 0o644); err != nil {
				t.Fatal(err)
			}
			status := ruleStatus(path, "synced")
			status["journal_mode"] = test.journalMode
			daemon := newLeaseDaemon(t, status)
			daemon.rules = []map[string]any{{
				"id": "rule-1", "path": filepath.Dir(path) + "/*.sqlite", "name": "", "prefix": "tenants/",
				"watch": "inotify", "maximum_local_databases": 10, "offload_durability": "server",
				"offload_rollback_journal": test.consent, "failover": false,
			}}
			client := daemon.connect(t)
			pool, err := NewPool(client, &PoolArgs[string]{Adapter: &stepLoggingAdapter{steps: daemon.steps}})
			if err != nil {
				t.Fatal(err)
			}
			defer pool.Close(t.Context())
			for range 2 {
				if err := pool.Use(t.Context(), "tenants/42.sqlite", func(context.Context, string) error {
					return nil
				}); err != nil {
					t.Fatal(err)
				}
			}
			opened := strings.Count(strings.Join(daemon.steps.all(), " "), "open")
			if test.closed && opened != 2 || !test.closed && opened != 1 {
				t.Fatalf("steps %v: opened %d times", daemon.steps.all(), opened)
			}
		})
	}
}
