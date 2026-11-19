package litebackup

import (
	"context"
	"net/http"
	"sync"
	"time"
)

// eventPage is how many events one failover long-poll reads.
const eventPage = 1000

// FailoverArgs configures a failover.
type FailoverArgs struct {
	// Name is the database name; the file name when empty.
	Name string
	// Timeout bounds the first download of a replica.
	Timeout time.Duration
	// OnRoleChange is called when this machine becomes the origin or a replica.
	OnRoleChange func(role Role)
	// OnConflictSaved is called with the conflict copy when this machine
	// lost the origin role while it had writes the new origin never saw.
	OnConflictSaved func(path string)
	// OnError is called when reading the daemon's events fails; watching retries.
	OnError func(err error)
}

// FailoverRun runs a database on several identical machines: one origin
// backs it up, and the others keep live readonly replicas at the same path.
// Each replica asks for the write lock about once a second, so one of them
// takes over once the origin's lease runs out.
type FailoverRun struct {
	client *Client
	args   FailoverArgs
	name   string
	path   string
	cancel context.CancelFunc

	mutex   sync.Mutex
	role    Role
	stopped bool
}

// Failover starts a failover of the database at path, the same path on every
// machine, and returns once this machine is the origin or holds a replica.
// Callbacks run on one goroutine per failover, in event order.
func (c *Client) Failover(ctx context.Context, path string, args *FailoverArgs) (*FailoverRun, error) {
	if args == nil {
		args = &FailoverArgs{}
	}
	if path == "" {
		return nil, invalidArgument("failover needs a path")
	}
	absolute, err := absolutePath(path)
	if err != nil {
		return nil, err
	}
	// Events after the log's end as of now are this failover's, including
	// those the start request itself records.
	end, err := c.Events(ctx, &EventsArgs{After: -1})
	if err != nil {
		return nil, err
	}
	timeout, err := millis(args.Timeout, "the failover timeout")
	if err != nil {
		return nil, err
	}
	body := FailoverRequest{Path: absolute, Name: args.Name, TimeoutMillis: timeout}
	var info FailoverInfo
	if err := c.call(ctx, http.MethodPost, "/v1/failovers/start", body, &info, args.Timeout); err != nil {
		return nil, err
	}
	watchContext, cancel := context.WithCancel(context.Background())
	failover := &FailoverRun{client: c, args: *args, name: info.Name, path: info.Path, role: info.Role, cancel: cancel}
	if !c.addFailover(failover) {
		cancel()
		return nil, closedError()
	}
	go failover.follow(watchContext, end.Next)
	return failover, nil
}

// Name is the failover's database name.
func (f *FailoverRun) Name() string { return f.name }

// Path is the database file on this machine.
func (f *FailoverRun) Path() string { return f.path }

// Role is this machine's role now.
func (f *FailoverRun) Role() Role {
	f.mutex.Lock()
	defer f.mutex.Unlock()
	return f.role
}

// IsOrigin reports whether this machine writes the database now.
func (f *FailoverRun) IsOrigin() bool { return f.Role() == RoleOrigin }

// Writer returns which machine writes the database and how long its lease runs.
func (f *FailoverRun) Writer(ctx context.Context) (*Writer, error) {
	return f.client.Writer(ctx, f.name)
}

// Stop stops the failover and releases its write lock. The file stays.
func (f *FailoverRun) Stop(ctx context.Context) error {
	_, err := f.client.StopBackup(ctx, f.name)
	f.end()
	f.client.removeFailover(f)
	return err
}

// end stops following events; callbacks are not called afterwards.
func (f *FailoverRun) end() {
	f.mutex.Lock()
	f.stopped = true
	f.mutex.Unlock()
	f.cancel()
}

// changeRole records a role and calls OnRoleChange when it differs.
func (f *FailoverRun) changeRole(role Role) {
	if role == "" {
		return
	}
	f.mutex.Lock()
	changed := f.role != role && !f.stopped
	f.role = role
	f.mutex.Unlock()
	if changed && f.args.OnRoleChange != nil {
		f.args.OnRoleChange(role)
	}
}

// handle applies one event about this failover's database.
func (f *FailoverRun) handle(event *Event) {
	switch event.Type {
	case "role_changed":
		role := Role(event.Role)
		if role == "" {
			role = Role(event.Message)
		}
		f.changeRole(role)
	case "conflict_saved":
		path := event.Path
		if path == "" {
			path = event.Message
		}
		f.mutex.Lock()
		stopped := f.stopped
		f.mutex.Unlock()
		if !stopped && f.args.OnConflictSaved != nil {
			f.args.OnConflictSaved(path)
		}
	}
}

// follow long-polls the daemon's events after cursor until stopped, handing
// this failover's events to it.
func (f *FailoverRun) follow(ctx context.Context, cursor int64) {
	delay := firstRetryDelay
	for ctx.Err() == nil {
		page, err := f.client.Events(ctx, &EventsArgs{After: cursor, Wait: eventWait, Limit: eventPage})
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			if f.args.OnError != nil {
				f.args.OnError(err)
			}
			if IsCode(err, CodeSDKUnsupported) || IsCode(err, CodeClosed) {
				return
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(delay):
			}
			delay = min(delay*2, maximumRetryDelay)
			continue
		}
		delay = firstRetryDelay
		for index := range page.Events {
			event := &page.Events[index]
			cursor = max(cursor, event.ID)
			if event.Name == f.name && ctx.Err() == nil {
				f.handle(event)
			}
		}
		cursor = max(cursor, page.Next)
	}
}
