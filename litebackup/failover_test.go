package litebackup

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// failoverDaemon is a fake daemon for one failover whose event long-polls
// answer from pages and whose rule stop waits for stopGate when set.
func failoverDaemon(t *testing.T, pages chan map[string]any, stopGate chan struct{}) *fakeDaemon {
	t.Helper()
	daemon := newFakeDaemon(t)
	daemon.route("GET /v1/events/list", func(request *fakeRequest) (int, any) {
		after, _ := strconv.ParseInt(request.Query["after"], 10, 64)
		if request.Query["wait_millis"] == "" {
			return http.StatusOK, map[string]any{"events": []any{}, "next": 7}
		}
		select {
		case page := <-pages:
			return http.StatusOK, page
		case <-time.After(50 * time.Millisecond):
			return http.StatusOK, map[string]any{"events": []any{}, "next": after}
		}
	})
	daemon.route("POST /v1/failovers/start", func(request *fakeRequest) (int, any) {
		return http.StatusOK, map[string]any{
			"name": "app/main.db",
			"path": request.Body["path"],
			"role": "replica",
		}
	})
	daemon.route("POST /v1/backup-rules/stop", func(*fakeRequest) (int, any) {
		if stopGate != nil {
			<-stopGate
		}
		return http.StatusOK, map[string]any{}
	})
	return daemon
}

// roleEvent is an events page with one role change of the failover's database.
func roleEvent(id int, role string) map[string]any {
	return map[string]any{"events": []any{
		map[string]any{
			"id":      id,
			"time":    "2026-11-15T09:01:00Z",
			"type":    "role_changed",
			"message": role,
			"name":    "app/main.db",
			"role":    role,
		},
	}, "next": id}
}

func TestFailoverStopAwaitsCallback(t *testing.T) {
	pages := make(chan map[string]any, 4)
	stopGate := make(chan struct{})
	client := failoverDaemon(t, pages, stopGate).connect(t)
	entered := make(chan Role, 4)
	proceed := make(chan struct{})
	failover, err := client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
		OnRoleChange: func(role Role) {
			entered <- role
			<-proceed
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if role := receive(t, entered); role != RoleReplica {
		t.Fatalf("the first callback got %s, want the start's replica", role)
	}

	stopped := make(chan error, 1)
	go func() { stopped <- failover.Stop(t.Context()) }()
	select {
	case err := <-stopped:
		t.Fatalf("Stop returned while a callback ran: %v", err)
	case <-time.After(100 * time.Millisecond):
	}
	close(proceed)
	// The callback ended; Stop now waits on the daemon. Events arriving
	// meanwhile call nothing.
	pages <- roleEvent(9, "replica")
	select {
	case role := <-entered:
		t.Fatalf("a callback ran while Stop was pending: %s", role)
	case <-time.After(200 * time.Millisecond):
	}
	close(stopGate)
	if err := receive(t, stopped); err != nil {
		t.Fatal(err)
	}
	select {
	case role := <-entered:
		t.Fatalf("a callback ran after Stop: %s", role)
	default:
	}
}

func TestFailoverStopInsideCallback(t *testing.T) {
	pages := make(chan map[string]any, 4)
	client := failoverDaemon(t, pages, nil).connect(t)
	var failover *Failover
	stopped := make(chan error, 1)
	ready := make(chan struct{})
	var err error
	failover, err = client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
		OnRoleChange: func(Role) {
			<-ready
			stopped <- failover.Stop(t.Context())
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	close(ready)
	pages <- roleEvent(8, "origin")
	if err := receive(t, stopped); err != nil {
		t.Fatal(err)
	}
}

func TestCloseEndsBlockedCallback(t *testing.T) {
	pages := make(chan map[string]any, 4)
	daemon := failoverDaemon(t, pages, nil)
	hang := make(chan struct{})
	t.Cleanup(func() { close(hang) })
	daemon.route("GET /v1/databases/writer", func(*fakeRequest) (int, any) {
		<-hang
		return http.StatusOK, map[string]any{}
	})
	client, err := Connect(t.Context(), &ConnectArgs{SocketPath: daemon.socket})
	if err != nil {
		t.Fatal(err)
	}
	entered := make(chan struct{})
	callbackErr := make(chan error, 1)
	var failover *Failover
	failover, err = client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
		OnRoleChange: func(role Role) {
			// The start role's call may come before StartFailover returns.
			if role != RoleOrigin {
				return
			}
			close(entered)
			_, err := failover.Writer(context.Background())
			callbackErr <- err
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	pages <- roleEvent(8, "origin")
	receive(t, entered)
	closed := make(chan error, 1)
	go func() { closed <- client.Close() }()
	if err := receive(t, closed); err != nil {
		t.Fatal(err)
	}
	if err := receive(t, callbackErr); !IsCode(err, CodeClosed) {
		t.Fatalf("the callback's call ended with %v, want closed", err)
	}
}

// TestFailoverResumesExpiredCursor checks that an expired cursor
// does not end following: the failover takes a cursor at the log's end, then
// reads its role, reports the role change the pruned events held once, and
// follows from the new cursor. A failed role read is retried with that
// cursor kept, so the log's end is not read again and no event is skipped.
func TestFailoverResumesExpiredCursor(t *testing.T) {
	for _, failedRoleReads := range []int32{0, 1} {
		t.Run(fmt.Sprintf("failed role reads %d", failedRoleReads), func(t *testing.T) {
			checkLogEndResume(t, failedRoleReads)
		})
	}
}

func checkLogEndResume(t *testing.T, failedRoleReads int32) {
	daemon := failoverDaemon(t, nil, nil)
	var readsMutex sync.Mutex
	var reads []string
	addRead := func(read string) {
		readsMutex.Lock()
		defer readsMutex.Unlock()
		reads = append(reads, read)
	}
	var logEndReads, roleReads, freshPages atomic.Int32
	daemon.route("GET /v1/events/list", func(request *fakeRequest) (int, any) {
		after := request.Query["after"]
		if request.Query["wait_millis"] == "" && after != "-1" {
			// The look past the log end after the role read.
			addRead("events after " + after)
			next, _ := strconv.ParseInt(after, 10, 64)
			return http.StatusOK, map[string]any{"events": []any{}, "next": next}
		}
		if request.Query["wait_millis"] == "" {
			if logEndReads.Add(1) == 1 {
				return http.StatusOK, map[string]any{"events": []any{}, "next": 7}
			}
			addRead("log end")
			return http.StatusOK, map[string]any{"events": []any{}, "next": 20}
		}
		switch after {
		case "7":
			return http.StatusGone, map[string]any{
				"code":    CodeCursorExpired,
				"message": "events after 7 are no longer kept",
			}
		case "20":
			if freshPages.Add(1) == 1 {
				addRead("after 20")
				return http.StatusOK, roleEvent(21, "replica")
			}
		}
		time.Sleep(50 * time.Millisecond)
		next, _ := strconv.ParseInt(after, 10, 64)
		return http.StatusOK, map[string]any{"events": []any{}, "next": next}
	})
	daemon.route("GET /v1/databases/status", func(request *fakeRequest) (int, any) {
		if request.Query["name"] != "app/main.db" {
			t.Errorf("role read for %q, want app/main.db", request.Query["name"])
		}
		if roleReads.Add(1) <= failedRoleReads {
			addRead("role failed")
			return http.StatusServiceUnavailable, map[string]any{
				"code":    specCodeUnavailable,
				"message": "the daemon is busy",
			}
		}
		addRead("role")
		return http.StatusOK, map[string]any{
			"name":  "app/main.db",
			"path":  "/var/lib/app/main.db",
			"role":  RoleOrigin,
			"state": "syncing",
		}
	})
	client := daemon.connect(t)
	roles := make(chan Role, 4)
	reported := make(chan error, 4)
	failover, err := client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
		OnRoleChange: func(role Role) { roles <- role },
		OnError:      func(err error) { reported <- err },
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := receive(t, reported); !IsCode(err, CodeCursorExpired) {
		t.Fatalf("OnError got %v, want cursor_expired", err)
	}
	wantReads := "log end, role, log end, after 20"
	if failedRoleReads > 0 {
		if err := receive(t, reported); !IsCode(err, specCodeUnavailable) {
			t.Fatalf("OnError got %v, want the failed role read", err)
		}
		wantReads = "log end, role failed, role, log end, after 20"
	}
	if role := receive(t, roles); role != RoleReplica {
		t.Fatalf("start role %s, want replica from the start", role)
	}
	if role := receive(t, roles); role != RoleOrigin {
		t.Fatalf("first role change %s, want origin from the role read", role)
	}
	if role := receive(t, roles); role != RoleReplica {
		t.Fatalf("second role change %s, want replica from the fresh cursor", role)
	}
	select {
	case role := <-roles:
		t.Fatalf("an extra role change: %s", role)
	case err := <-reported:
		t.Fatalf("an extra error: %v", err)
	case <-time.After(200 * time.Millisecond):
	}
	readsMutex.Lock()
	order := strings.Join(reads, ", ")
	readsMutex.Unlock()
	if order != wantReads {
		t.Fatalf("reads %s, want %s", order, wantReads)
	}
	if err := failover.Stop(t.Context()); err != nil {
		t.Fatal(err)
	}
}

// shortenRoleConfirmation shortens the failover long-poll and the role
// confirmation limit for one test.
func shortenRoleConfirmation(t *testing.T, eventWait time.Duration, limit time.Duration) {
	t.Helper()
	savedWait, savedLimit := failoverEventWait, roleConfirmationLimit
	failoverEventWait, roleConfirmationLimit = eventWait, limit
	t.Cleanup(func() { failoverEventWait, roleConfirmationLimit = savedWait, savedLimit })
}

// originStatus is the database status answer of an origin.
func originStatus() map[string]any {
	return map[string]any{
		"name":  "app/main.db",
		"path":  "/var/lib/app/main.db",
		"role":  RoleOrigin,
		"state": "syncing",
	}
}

func TestFailoverReportsStartRole(t *testing.T) {
	client := failoverDaemon(t, make(chan map[string]any), nil).connect(t)
	roles := make(chan Role, 4)
	failover, err := client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
		OnRoleChange: func(role Role) { roles <- role },
	})
	if err != nil {
		t.Fatal(err)
	}
	if role := receive(t, roles); role != RoleReplica {
		t.Fatalf("start role %s, want replica", role)
	}
	select {
	case role := <-roles:
		t.Fatalf("an extra role change: %s", role)
	case <-time.After(200 * time.Millisecond):
	}
	if err := failover.Stop(t.Context()); err != nil {
		t.Fatal(err)
	}
	if role := failover.Role(); role != RoleUnknown {
		t.Fatalf("role after Stop %s, want unknown", role)
	}
}

// refusedMessage is the refusal a test daemon reports for a failover's file.
const refusedMessage = "/var/lib/app/main.db differs from the server head; it was left as it is"

// TestFailoverRoleUnknownWhileDaemonStopped checks that an origin whose
// daemon stops reports RoleUnknown at once and IsOrigin false, and that once
// the daemon answers again the follower reads the log's end and the role,
// reports the role, and keeps its cursor: a refusal meanwhile is
// reported, and a role change older than the role read is skipped.
func TestFailoverRoleUnknownWhileDaemonStopped(t *testing.T) {
	daemon := failoverDaemon(t, nil, nil)
	daemon.route("POST /v1/failovers/start", func(request *fakeRequest) (int, any) {
		return http.StatusOK, map[string]any{
			"name": "app/main.db",
			"path": request.Body["path"],
			"role": RoleOrigin,
		}
	})
	var returned atomic.Bool
	logEnd := make(chan string, 4)
	daemon.route("GET /v1/events/list", func(request *fakeRequest) (int, any) {
		after := request.Query["after"]
		if request.Query["wait_millis"] == "" {
			if !returned.Load() {
				return http.StatusOK, map[string]any{"events": []any{}, "next": 7}
			}
			logEnd <- after
			return http.StatusOK, map[string]any{"events": []any{}, "next": 9}
		}
		if returned.Load() && after == "7" {
			return http.StatusOK, map[string]any{"events": []any{
				roleEvent(8, "replica")["events"].([]any)[0],
				map[string]any{
					"id":      9,
					"time":    "2026-11-15T09:02:00Z",
					"type":    EventTypeRefused,
					"message": refusedMessage,
					"name":    "app/main.db",
					"path":    "/var/lib/app/main.db",
				},
			}, "next": 9}
		}
		time.Sleep(50 * time.Millisecond)
		next, _ := strconv.ParseInt(after, 10, 64)
		return http.StatusOK, map[string]any{"events": []any{}, "next": next}
	})
	daemon.route("GET /v1/databases/status", func(*fakeRequest) (int, any) {
		return http.StatusOK, originStatus()
	})
	client := daemon.connect(t)
	roles := make(chan Role, 8)
	refusals := make(chan string, 4)
	reported := make(chan error, 64)
	failover, err := client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
		OnRoleChange: func(role Role) { roles <- role },
		OnRefused:    func(message string) { refusals <- message },
		OnError:      func(err error) { reported <- err },
	})
	if err != nil {
		t.Fatal(err)
	}
	if role := receive(t, roles); role != RoleOrigin {
		t.Fatalf("start role %s, want origin", role)
	}
	daemon.stop()
	stopped := time.Now()
	if role := receive(t, roles); role != RoleUnknown {
		t.Fatalf("role after the daemon stopped %s, want unknown", role)
	}
	if elapsed := time.Since(stopped); elapsed > time.Second {
		t.Fatalf("the role became unknown %s after the daemon stopped, want at once", elapsed)
	}
	if failover.IsOrigin() {
		t.Fatal("IsOrigin is true while the daemon is stopped")
	}
	if err := receive(t, reported); !IsCode(err, CodeConnectionFailed) {
		t.Fatalf("OnError got %v, want connection_failed", err)
	}

	returned.Store(true)
	daemon.serve()
	if cursor := receive(t, logEnd); cursor != "-1" {
		t.Fatalf("the log end read sent after %s, want -1", cursor)
	}
	if role := receive(t, roles); role != RoleOrigin {
		t.Fatalf("role once the daemon answered %s, want origin from the role read", role)
	}
	if !failover.IsOrigin() {
		t.Fatal("IsOrigin is false once the daemon answered origin")
	}
	// The refusal made while the daemon was stopped is older than the role
	// read: it is reported, and leaves the role the read gave.
	if message := receive(t, refusals); message != refusedMessage {
		t.Fatalf("refusal %q, want the one made while the daemon was stopped", message)
	}
	select {
	case role := <-roles:
		t.Fatalf("a role change older than the role read was reported: %s", role)
	case <-time.After(200 * time.Millisecond):
	}
	if err := failover.Stop(t.Context()); err != nil {
		t.Fatal(err)
	}
}

// TestFailoverRoleUnknownAfterConfirmationLimit checks that a daemon that
// answers every read with an error, or answers none, leaves the role
// unconfirmed for at most the confirmation limit.
func TestFailoverRoleUnknownAfterConfirmationLimit(t *testing.T) {
	for _, failing := range []string{"answered errors", "no answer"} {
		t.Run(failing, func(t *testing.T) {
			const limit = 400 * time.Millisecond
			shortenRoleConfirmation(t, 100*time.Millisecond, limit)
			daemon := failoverDaemon(t, nil, nil)
			hang := make(chan struct{})
			t.Cleanup(func() { close(hang) })
			daemon.route("GET /v1/events/list", func(request *fakeRequest) (int, any) {
				if request.Query["wait_millis"] == "" {
					return http.StatusOK, map[string]any{"events": []any{}, "next": 7}
				}
				if failing == "no answer" {
					<-hang
				}
				return http.StatusServiceUnavailable, map[string]any{
					"code":    specCodeUnavailable,
					"message": "the daemon is busy",
				}
			})
			client := daemon.connect(t)
			roles := make(chan Role, 4)
			reported := make(chan error, 64)
			started := time.Now()
			failover, err := client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
				OnRoleChange: func(role Role) { roles <- role },
				OnError:      func(err error) { reported <- err },
			})
			if err != nil {
				t.Fatal(err)
			}
			if role := receive(t, roles); role != RoleReplica {
				t.Fatalf("start role %s, want replica", role)
			}
			if role := receive(t, roles); role != RoleUnknown {
				t.Fatalf("role %s, want unknown", role)
			}
			elapsed := time.Since(started)
			if elapsed < limit || elapsed > limit+500*time.Millisecond {
				t.Fatalf("the role became unknown after %s, want about %s", elapsed, limit)
			}
			wantCode := specCodeUnavailable
			if failing == "no answer" {
				wantCode = CodeTimeout
			}
			if err := receive(t, reported); !IsCode(err, wantCode) {
				t.Fatalf("OnError got %v, want %s", err, wantCode)
			}
			if err := failover.Stop(t.Context()); err != nil {
				t.Fatal(err)
			}
		})
	}
}

// TestFailoverRereadsRacedRole checks that a role read the
// log shows a role change after is not trusted: the follower reads the role
// again, and the long-poll from its kept cursor reports no role change
// older than that read.
func TestFailoverRereadsRacedRole(t *testing.T) {
	daemon := failoverDaemon(t, nil, nil)
	daemon.route("POST /v1/failovers/start", func(request *fakeRequest) (int, any) {
		return http.StatusOK, map[string]any{
			"name": "app/main.db",
			"path": request.Body["path"],
			"role": RoleOrigin,
		}
	})
	var returned atomic.Bool
	var logEndReads atomic.Int32
	racedChanges := map[string]any{"events": []any{
		roleEvent(10, "origin")["events"].([]any)[0],
		roleEvent(11, "replica")["events"].([]any)[0],
	}, "next": 11}
	daemon.route("GET /v1/events/list", func(request *fakeRequest) (int, any) {
		after := request.Query["after"]
		if request.Query["wait_millis"] == "" {
			switch {
			case !returned.Load():
				return http.StatusOK, map[string]any{"events": []any{}, "next": 7}
			case after == "-1" && logEndReads.Add(1) == 1:
				return http.StatusOK, map[string]any{"events": []any{}, "next": 9}
			case after == "-1":
				// The role changes landed after the first log end read.
				return http.StatusOK, map[string]any{"events": []any{}, "next": 11}
			case after == "9":
				return http.StatusOK, racedChanges
			}
			next, _ := strconv.ParseInt(after, 10, 64)
			return http.StatusOK, map[string]any{"events": []any{}, "next": next}
		}
		if returned.Load() && after == "7" {
			return http.StatusOK, racedChanges
		}
		time.Sleep(50 * time.Millisecond)
		next, _ := strconv.ParseInt(after, 10, 64)
		return http.StatusOK, map[string]any{"events": []any{}, "next": next}
	})
	var roleReads atomic.Int32
	daemon.route("GET /v1/databases/status", func(*fakeRequest) (int, any) {
		roleReads.Add(1)
		status := originStatus()
		status["role"] = RoleReplica
		return http.StatusOK, status
	})
	client := daemon.connect(t)
	roles := make(chan Role, 8)
	failover, err := client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
		OnRoleChange: func(role Role) { roles <- role },
	})
	if err != nil {
		t.Fatal(err)
	}
	if role := receive(t, roles); role != RoleOrigin {
		t.Fatalf("start role %s, want origin", role)
	}
	daemon.stop()
	if role := receive(t, roles); role != RoleUnknown {
		t.Fatalf("role after the daemon stopped %s, want unknown", role)
	}
	returned.Store(true)
	daemon.serve()
	if role := receive(t, roles); role != RoleReplica {
		t.Fatalf("role once the daemon answered %s, want replica", role)
	}
	select {
	case role := <-roles:
		t.Fatalf("a role change older than the role read was reported: %s", role)
	case <-time.After(300 * time.Millisecond):
	}
	if reads := roleReads.Load(); reads != 2 {
		t.Fatalf("%d role reads, want 2: the first raced the role changes", reads)
	}
	if err := failover.Stop(t.Context()); err != nil {
		t.Fatal(err)
	}
}

// TestFailoverRereadsRoleRacedByRefusal checks that a refusal the log shows
// after a role read is a role change too: the follower reads the role again,
// which a refused file answers as conflicted, reports no stale origin, and
// hears the refusal once, through the long-poll from its kept cursor.
func TestFailoverRereadsRoleRacedByRefusal(t *testing.T) {
	daemon := failoverDaemon(t, nil, nil)
	daemon.route("POST /v1/failovers/start", func(request *fakeRequest) (int, any) {
		return http.StatusOK, map[string]any{
			"name": "app/main.db",
			"path": request.Body["path"],
			"role": RoleOrigin,
		}
	})
	var returned atomic.Bool
	var logEndReads atomic.Int32
	racedRefusal := map[string]any{"events": []any{
		map[string]any{
			"id":      10,
			"time":    "2026-11-15T09:02:00Z",
			"type":    EventTypeRefused,
			"message": refusedMessage,
			"name":    "app/main.db",
			"path":    "/var/lib/app/main.db",
		},
	}, "next": 10}
	daemon.route("GET /v1/events/list", func(request *fakeRequest) (int, any) {
		after := request.Query["after"]
		if request.Query["wait_millis"] == "" {
			switch {
			case !returned.Load():
				return http.StatusOK, map[string]any{"events": []any{}, "next": 7}
			case after == "-1" && logEndReads.Add(1) == 1:
				return http.StatusOK, map[string]any{"events": []any{}, "next": 9}
			case after == "-1":
				// The refusal landed after the first log end read.
				return http.StatusOK, map[string]any{"events": []any{}, "next": 10}
			case after == "9":
				return http.StatusOK, racedRefusal
			}
			next, _ := strconv.ParseInt(after, 10, 64)
			return http.StatusOK, map[string]any{"events": []any{}, "next": next}
		}
		if returned.Load() && after == "7" {
			return http.StatusOK, racedRefusal
		}
		time.Sleep(50 * time.Millisecond)
		next, _ := strconv.ParseInt(after, 10, 64)
		return http.StatusOK, map[string]any{"events": []any{}, "next": next}
	})
	var roleReads atomic.Int32
	daemon.route("GET /v1/databases/status", func(*fakeRequest) (int, any) {
		status := originStatus()
		if roleReads.Add(1) > 1 {
			status["state"] = DatabaseStatusStateConflicted
		}
		return http.StatusOK, status
	})
	client := daemon.connect(t)
	roles := make(chan Role, 8)
	refusals := make(chan string, 4)
	failover, err := client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
		OnRoleChange: func(role Role) { roles <- role },
		OnRefused:    func(message string) { refusals <- message },
	})
	if err != nil {
		t.Fatal(err)
	}
	if role := receive(t, roles); role != RoleOrigin {
		t.Fatalf("start role %s, want origin", role)
	}
	daemon.stop()
	if role := receive(t, roles); role != RoleUnknown {
		t.Fatalf("role after the daemon stopped %s, want unknown", role)
	}
	returned.Store(true)
	daemon.serve()
	if message := receive(t, refusals); message != refusedMessage {
		t.Fatalf("refusal %q", message)
	}
	select {
	case role := <-roles:
		t.Fatalf("a role the refusal superseded was reported: %s", role)
	case message := <-refusals:
		t.Fatalf("the refusal was reported again: %q", message)
	case <-time.After(300 * time.Millisecond):
	}
	// A role left unknown is read again after every long-poll that brings no
	// role change, so the second read is not the last.
	if reads := roleReads.Load(); reads < 2 {
		t.Fatalf("%d role reads, want at least 2: the first raced the refusal", reads)
	}
	if failover.IsOrigin() {
		t.Fatal("IsOrigin is true after the refusal")
	}
	if err := failover.Stop(t.Context()); err != nil {
		t.Fatal(err)
	}
}

// fixedLogEndEvents is a page of events after after, as the daemon pages
// them: up to eventPage, through newest, with next the last ID delivered.
// All are another database's, except this database's change to origin at
// 2500 when ownChangeInScan is set, and its change to replica at 2601, which
// exists only after the last look past the role read stopped at 2600.
func fixedLogEndEvents(after int64, ownChangeInScan bool, newest int64) map[string]any {
	events := []any{}
	for id := after + 1; id <= min(after+eventPage, newest); id++ {
		event := map[string]any{
			"id":   id,
			"time": "2026-11-15T09:01:00Z",
			"type": EventTypeRoleChanged,
			"name": "other.db",
			"role": RoleOrigin,
		}
		switch {
		case id == 2500 && ownChangeInScan:
			event["name"] = "app/main.db"
		case id == 2601:
			event["name"], event["role"] = "app/main.db", RoleReplica
		}
		events = append(events, event)
	}
	next := after
	if len(events) > 0 {
		next = events[len(events)-1].(map[string]any)["id"].(int64)
	}
	return map[string]any{"events": events, "next": next}
}

// TestFailoverRoleReadThroughFixedLogEnd checks the look past the log
// end after a role read: it pages through the log end read after the role
// read and no further, a role change of this database there has the role
// read again, an expired cursor there starts again from a fresh log end, and
// a role change after that end reaches the callback through the long-poll.
func TestFailoverRoleReadThroughFixedLogEnd(t *testing.T) {
	scanConditions := []string{"unrelated events", "role change on a later page", "expired cursor"}
	for _, scanCondition := range scanConditions {
		t.Run(scanCondition, func(t *testing.T) {
			daemon := failoverDaemon(t, nil, nil)
			var logEndReads, scans, roleReads atomic.Int32
			ownChangeInScan := scanCondition == "role change on a later page"
			wantRoleReads := int32(2)
			if scanCondition == "unrelated events" {
				wantRoleReads = 1
			}
			// The newest event: 2601 once the follower read its last role and the
			// log end after it.
			var newest atomic.Int64
			newest.Store(2600)
			daemon.route("GET /v1/events/list", func(request *fakeRequest) (int, any) {
				after, _ := strconv.ParseInt(request.Query["after"], 10, 64)
				if request.Query["wait_millis"] != "" {
					if after == 7 {
						return http.StatusGone, map[string]any{
							"code":    CodeCursorExpired,
							"message": "events after 7 are no longer kept",
						}
					}
					page := fixedLogEndEvents(after, ownChangeInScan, newest.Load())
					if len(page["events"].([]any)) == 0 {
						time.Sleep(50 * time.Millisecond)
					}
					return http.StatusOK, page
				}
				if after == -1 {
					// The start's log end, the one after the expired cursor,
					// then the end the look past the role read stops at.
					ends := []int64{7, 100}
					read := int(logEndReads.Add(1)) - 1
					if read < len(ends) {
						return http.StatusOK, map[string]any{"events": []any{}, "next": ends[read]}
					}
					end := newest.Load()
					if roleReads.Load() == wantRoleReads {
						// This database becomes a replica after the look stopped.
						newest.Store(2601)
					}
					return http.StatusOK, map[string]any{"events": []any{}, "next": end}
				}
				if scanCondition == "expired cursor" && scans.Add(1) == 1 {
					return http.StatusGone, map[string]any{
						"code":    CodeCursorExpired,
						"message": "pruned",
					}
				}
				return http.StatusOK, fixedLogEndEvents(after, ownChangeInScan, newest.Load())
			})
			daemon.route("GET /v1/databases/status", func(*fakeRequest) (int, any) {
				roleReads.Add(1)
				return http.StatusOK, originStatus()
			})
			client := daemon.connect(t)
			roles := make(chan Role, 8)
			failover, err := client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
				OnRoleChange: func(role Role) { roles <- role },
			})
			if err != nil {
				t.Fatal(err)
			}
			for _, want := range []Role{RoleReplica, RoleOrigin, RoleReplica} {
				if role := receive(t, roles); role != want {
					t.Fatalf("role %s, want %s", role, want)
				}
			}
			select {
			case role := <-roles:
				t.Fatalf("an extra role change: %s", role)
			case <-time.After(200 * time.Millisecond):
			}
			if reads := roleReads.Load(); reads != wantRoleReads {
				t.Fatalf("%d role reads, want %d", reads, wantRoleReads)
			}
			if err := failover.Stop(t.Context()); err != nil {
				t.Fatal(err)
			}
		})
	}
}

// TestClosedReadCallsNoRoleChange checks that a read a closing client ended
// with closed, before the follower stopped, clears the role without a
// callback and ends following.
func TestClosedReadCallsNoRoleChange(t *testing.T) {
	var roleChanges atomic.Int32
	failover := &Failover{
		role:              RoleOrigin,
		cancel:            func() {},
		confirmationLimit: roleConfirmationLimit,
		args:              FailoverArgs{OnRoleChange: func(Role) { roleChanges.Add(1) }},
	}
	progress := &followProgress{delay: firstRetryDelay, confirmed: time.Now()}
	if failover.retryAfterError(t.Context(), closedError(), progress) {
		t.Fatal("following goes on after closed")
	}
	if role := failover.Role(); role != RoleUnknown {
		t.Fatalf("role %s after closed, want unknown", role)
	}
	if changes := roleChanges.Load(); changes != 0 {
		t.Fatalf("closed called OnRoleChange %d times", changes)
	}
}

// drainingEvent is an events page with one draining event of the
// failover's database.
func drainingEvent(id int) map[string]any {
	return map[string]any{"events": []any{
		map[string]any{
			"id":      id,
			"time":    "2026-11-15T09:02:00Z",
			"type":    "draining",
			"message": "this machine is draining",
			"name":    "app/main.db",
		},
	}, "next": id}
}

// TestFailoverDrainingMakesRoleUnknown checks that a draining event makes an
// origin's role RoleUnknown, with a callback and IsOrigin false, that role
// reads while the status says draining keep it unknown, and that the next
// role_changed sets the role again.
func TestFailoverDrainingMakesRoleUnknown(t *testing.T) {
	pages := make(chan map[string]any, 4)
	daemon := failoverDaemon(t, pages, nil)
	daemon.route("GET /v1/databases/status", func(*fakeRequest) (int, any) {
		draining := originStatus()
		draining["draining"] = true
		return http.StatusOK, draining
	})
	client := daemon.connect(t)
	roles := make(chan Role, 8)
	failover, err := client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
		OnRoleChange: func(role Role) { roles <- role },
	})
	if err != nil {
		t.Fatal(err)
	}
	defer failover.Stop(t.Context())
	if role := receive(t, roles); role != RoleReplica {
		t.Fatalf("start role %s, want replica", role)
	}
	pages <- roleEvent(8, "origin")
	if role := receive(t, roles); role != RoleOrigin || !failover.IsOrigin() {
		t.Fatalf("role %s, want origin", role)
	}
	pages <- drainingEvent(9)
	if role := receive(t, roles); role != RoleUnknown || failover.IsOrigin() {
		t.Fatalf("role %s after draining, want unknown", role)
	}
	pages <- roleEvent(10, string(RoleOrigin))
	if role := receive(t, roles); role != RoleOrigin {
		t.Fatalf("role %s after the role change, want origin", role)
	}
}

// TestFailoverRereadsRoleAfterFailedDrain checks that a role unknown since a
// draining event is read again after long-polls that bring no role change:
// once the drain failed and the status no longer says draining, the role is
// origin again without any event.
func TestFailoverRereadsRoleAfterFailedDrain(t *testing.T) {
	pages := make(chan map[string]any, 4)
	daemon := failoverDaemon(t, pages, nil)
	var drainFailed atomic.Bool
	var roleReads atomic.Int32
	daemon.route("GET /v1/databases/status", func(*fakeRequest) (int, any) {
		roleReads.Add(1)
		status := originStatus()
		status["draining"] = !drainFailed.Load()
		return http.StatusOK, status
	})
	client := daemon.connect(t)
	roles := make(chan Role, 8)
	failover, err := client.StartFailover(t.Context(), "/var/lib/app/main.db", &FailoverArgs{
		OnRoleChange: func(role Role) { roles <- role },
	})
	if err != nil {
		t.Fatal(err)
	}
	defer failover.Stop(t.Context())
	if role := receive(t, roles); role != RoleReplica {
		t.Fatalf("start role %s, want replica", role)
	}
	pages <- roleEvent(8, string(RoleOrigin))
	if role := receive(t, roles); role != RoleOrigin {
		t.Fatalf("role %s, want origin", role)
	}
	pages <- drainingEvent(9)
	if role := receive(t, roles); role != RoleUnknown {
		t.Fatalf("role %s after draining, want unknown", role)
	}
	deadline := time.Now().Add(5 * time.Second)
	for roleReads.Load() < 2 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if roleReads.Load() < 2 {
		t.Fatal("the follower did not read the role again while it was unknown")
	}
	drainFailed.Store(true)
	if role := receive(t, roles); role != RoleOrigin || !failover.IsOrigin() {
		t.Fatalf("role %s after the drain failed, want origin", role)
	}
}

// TestStatusRoleWhileDraining checks that a role read reports RoleUnknown
// while the daemon drains the origin or refused its file, and the role
// otherwise.
func TestStatusRoleWhileDraining(t *testing.T) {
	if role := statusRole(&DatabaseStatus{Role: RoleOrigin, Draining: true}); role != RoleUnknown {
		t.Fatalf("a draining origin reads as %s", role)
	}
	refused := &DatabaseStatus{Role: RoleReplica, State: DatabaseStatusStateConflicted}
	if role := statusRole(refused); role != RoleUnknown {
		t.Fatalf("a refused replica reads as %s", role)
	}
	if role := statusRole(&DatabaseStatus{Role: RoleOrigin}); role != RoleOrigin {
		t.Fatalf("an origin reads as %s", role)
	}
}

// TestFailoverSendsDiscardLocal sends discard_local on the start only when
// FailoverArgs.DiscardLocal asks.
func TestFailoverSendsDiscardLocal(t *testing.T) {
	for _, discardLocal := range []bool{false, true} {
		daemon := failoverDaemon(t, make(chan map[string]any), nil)
		client := daemon.connect(t)
		failover, err := client.StartFailover(
			t.Context(),
			"/var/lib/app/main.db",
			&FailoverArgs{DiscardLocal: discardLocal},
		)
		if err != nil {
			t.Fatal(err)
		}
		body := daemon.received("/v1/failovers/start")[0].Body
		sent, found := body["discard_local"]
		if found != discardLocal || (found && sent != true) {
			t.Fatalf("DiscardLocal %t sent the body %v", discardLocal, body)
		}
		if err := failover.Stop(t.Context()); err != nil {
			t.Fatal(err)
		}
	}
}
