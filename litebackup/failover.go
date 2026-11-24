package litebackup

import (
	"bytes"
	"context"
	"errors"
	"net/http"
	"runtime"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// eventPage is how many events one failover long-poll reads.
const eventPage = 1000

// failoverEventWait is how long one failover long-poll waits. It is shorter
// than roleConfirmationLimit, so a daemon that answers confirms the role in
// time. Tests shorten both.
var failoverEventWait = 10 * time.Second

// roleConfirmationLimit is the longest a failover keeps reporting a role the
// daemon has not confirmed: 15 s, the default client heartbeat interval and a
// quarter of the default 60 s session lease another machine waits out before
// it takes over. A read the daemon has not answered by then, or a run of
// failed reads that lasts that long, makes the role RoleUnknown.
var roleConfirmationLimit = 15 * time.Second

// FailoverArgs configures a failover.
type FailoverArgs struct {
	// Name is the database name; the file name when empty.
	Name string
	// Timeout bounds the first download of a replica.
	Timeout time.Duration
	// DiscardLocal replaces a file at the path that differs from the server
	// head with the head, discarding its writes the head lacks. Without it
	// such a file is refused with conflict and left untouched.
	DiscardLocal bool
	// OnRoleChange is called once with the role the start answered, then
	// whenever this machine becomes the origin or a replica, and with
	// RoleUnknown when the daemon stops answering. Once the daemon answers
	// again, the role is read and reported. The first call may come before
	// StartFailover returns, so use its role argument, not the *Failover.
	OnRoleChange func(role Role)
	// OnRefused is called with the daemon's message when it refused this
	// machine's file and the failover stays conflicted there, leaving the
	// file as it is: a file that differs from the server head when this machine starts
	// as the origin, or one that holds writes the new origin never saw when
	// this machine lost the origin role. The database's state is conflicted,
	// and the role is RoleUnknown, except for a refusal older than the last
	// role read after the daemon answered again, which leaves the role that
	// read gave.
	OnRefused func(message string)
	// OnError is called when reading the daemon's events fails; watching retries.
	// An expired cursor (cursor_expired) is reported too, and following
	// resumes at the log's end after re-reading the role.
	OnError func(err error)
}

// Failover runs a database on several identical machines: one origin
// backs it up, and the others keep live readonly replicas at the same path.
// Each replica asks for the write lock about once a second, so one of them
// takes over once the origin's lease runs out.
type Failover struct {
	client *Client
	args   FailoverArgs
	name   string
	path   string
	cancel context.CancelFunc
	// eventWait and confirmationLimit are failoverEventWait and
	// roleConfirmationLimit when the failover started.
	eventWait         time.Duration
	confirmationLimit time.Duration

	mutex   sync.Mutex
	role    Role
	stopped bool

	// callbackMutex is held while a callback runs, so stopFollowing can wait
	// for it.
	callbackMutex sync.Mutex
	// follower is the ID of the goroutine that runs the callbacks.
	follower atomic.Uint64
}

// validateFailover checks what Failover needs before anything connects.
func validateFailover(path string, args *FailoverArgs) error {
	if path == "" {
		return invalidArgument("failover needs a path")
	}
	if args != nil && args.Timeout < 0 {
		return invalidArgument("the failover timeout must not be negative")
	}
	return nil
}

// StartFailover starts a failover of the database at path, the same path on every
// machine, and returns once this machine is the origin or holds a replica.
// Callbacks run on one goroutine per failover, in event order; the first is
// OnRoleChange with the role the start answered.
func (c *Client) StartFailover(
	ctx context.Context,
	path string,
	args *FailoverArgs,
) (*Failover, error) {
	if args == nil {
		args = &FailoverArgs{}
	}
	if err := validateFailover(path, args); err != nil {
		return nil, err
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
	timeout, _ := millis(args.Timeout, "the failover timeout")
	body := FailoverRequest{
		Path:          absolute,
		Name:          args.Name,
		TimeoutMillis: timeout,
		DiscardLocal:  args.DiscardLocal,
	}
	var failoverStatus FailoverStatus
	if err := c.call(ctx, http.MethodPost, "/v1/failovers/start", body, &failoverStatus, args.Timeout); err != nil {
		return nil, err
	}
	watchContext, cancel := context.WithCancel(context.Background())
	failover := &Failover{
		client:            c,
		args:              *args,
		name:              failoverStatus.Name,
		path:              failoverStatus.Path,
		role:              failoverStatus.Role,
		cancel:            cancel,
		eventWait:         failoverEventWait,
		confirmationLimit: roleConfirmationLimit,
	}
	if !c.addFailover(failover) {
		cancel()
		return nil, closedError()
	}
	go failover.follow(watchContext, end.Next)
	return failover, nil
}

// Name is the failover's database name.
func (f *Failover) Name() string { return f.name }

// Path is the database file on this machine.
func (f *Failover) Path() string { return f.path }

// Role is this machine's role now: RoleOrigin, RoleReplica, or RoleUnknown
// while the daemon does not answer and once the failover stopped.
func (f *Failover) Role() Role {
	f.mutex.Lock()
	defer f.mutex.Unlock()
	return f.role
}

// IsOrigin reports whether this machine writes the database now. It is false
// while the role is RoleUnknown.
func (f *Failover) IsOrigin() bool { return f.Role() == RoleOrigin }

// Writer returns which machine writes the database and how long its lease runs.
func (f *Failover) Writer(ctx context.Context) (*Writer, error) {
	return f.client.Writer(ctx, f.name)
}

// Stop stops the failover and releases its write lock. The file stays, and
// the role is RoleUnknown from then on.
// No callback runs once Stop returns, nor while it waits for the daemon.
func (f *Failover) Stop(ctx context.Context) error {
	f.stopFollowing()
	_, err := f.client.StopBackup(ctx, f.name)
	f.client.removeFailover(f)
	return err
}

// stopFollowing stops following events and waits for a callback in flight,
// unless it runs inside that callback; callbacks are not called afterwards.
// Nothing follows the role any more, so it is RoleUnknown.
func (f *Failover) stopFollowing() {
	f.mutex.Lock()
	f.stopped = true
	f.role = RoleUnknown
	f.mutex.Unlock()
	f.cancel()
	if goroutineID() != f.follower.Load() {
		f.callbackMutex.Lock()
		f.callbackMutex.Unlock()
	}
}

// notify runs a callback unless the failover stopped.
func (f *Failover) notify(callback func()) {
	f.callbackMutex.Lock()
	defer f.callbackMutex.Unlock()
	f.mutex.Lock()
	stopped := f.stopped
	f.mutex.Unlock()
	if !stopped {
		callback()
	}
}

// goroutineID returns the running goroutine's ID from its stack header,
// "goroutine N [...]".
func goroutineID() uint64 {
	var header [64]byte
	written := runtime.Stack(header[:], false)
	fields := bytes.Fields(bytes.TrimPrefix(header[:written], []byte("goroutine ")))
	if len(fields) == 0 {
		return 0
	}
	id, _ := strconv.ParseUint(string(fields[0]), 10, 64)
	return id
}

// changeRole saves a role and calls OnRoleChange when it differs. It reports
// whether the role changed.
func (f *Failover) changeRole(role Role) bool {
	if role == "" {
		return false
	}
	f.mutex.Lock()
	changed := f.role != role && !f.stopped
	if changed {
		f.role = role
	}
	f.mutex.Unlock()
	if changed && f.args.OnRoleChange != nil {
		f.notify(func() { f.args.OnRoleChange(role) })
	}
	return changed
}

// reportStartRole calls OnRoleChange with the role the start answered, so an
// app can route writes from its callback alone.
func (f *Failover) reportStartRole() {
	role := f.Role()
	if f.args.OnRoleChange != nil {
		f.notify(func() { f.args.OnRoleChange(role) })
	}
}

// applyEvent applies one event about this failover's database. An event
// older than the last role read changes no role, because that read holds it;
// a refusal among them is still reported.
func (f *Failover) applyEvent(event *Event, olderThanRoleRead bool) {
	if olderThanRoleRead && event.Type != EventTypeRefused {
		return
	}
	switch event.Type {
	case EventTypeRoleChanged:
		role := Role(event.Role)
		if role == "" {
			role = Role(event.Message)
		}
		f.changeRole(role)
	case EventTypeDraining:
		// The daemon drains: it stops being the origin before it releases the
		// lock, so the app stops writing while no machine may take over yet.
		f.changeRole(RoleUnknown)
	case EventTypeRefused:
		// The daemon refused this machine's file and the failover stays
		// conflicted there: this machine neither writes nor follows the database.
		if !olderThanRoleRead {
			f.changeRole(RoleUnknown)
		}
		message := event.Message
		if f.args.OnRefused != nil {
			f.notify(func() { f.args.OnRefused(message) })
		}
	}
}

// statusRole is the role a status read reports: RoleUnknown while the daemon
// drains the origin, as after a draining event, and while the failover's file
// is refused (conflicted), as after a refused event.
func statusRole(status *DatabaseStatus) Role {
	if status.Draining || status.State == DatabaseStatusStateConflicted {
		return RoleUnknown
	}
	return status.Role
}

// changesRole reports an event type that sets a failover's role: role_changed,
// and draining and refused, which make it RoleUnknown.
func changesRole(eventType EventType) bool {
	return eventType == EventTypeRoleChanged || eventType == EventTypeDraining ||
		eventType == EventTypeRefused
}

// followProgress is what a failover's follower carries from one read to the next.
type followProgress struct {
	// cursor is the newest event ID handed to the failover.
	cursor int64
	// delay is the backoff after the next failed read.
	delay time.Duration
	// confirmed is when the daemon last confirmed the role: the start, a
	// long-poll, or a role read answered.
	confirmed time.Time
	// roleReadPending is set when the role is read before the next long-poll:
	// after an expired cursor, and after the role became RoleUnknown.
	roleReadPending bool
	// logEndPending is set when the log's end is read before that role read.
	logEndPending bool
	// roleReadLogEnd is the log's end read before the last role read:
	// role_changed events up to it are older than that read and are skipped.
	roleReadLogEnd int64
}

// follow long-polls the daemon's events after cursor until stopped, handing
// this failover's events to it. It first reports the role the start answered.
//
// An expired cursor does not end following: it moves to the log's end, then
// the role is read again before the next long-poll, so a role change in
// events the daemon pruned is still reported. The cursor is taken first and
// kept while the role read is retried, so no event after it is lost.
//
// A read the daemon did not answer (connection_failed, timeout,
// daemon_unsupported: any error with Status 0) makes the role RoleUnknown at
// once, and so does no answer within confirmationLimit of the last
// confirmation, or failed reads the daemon answered for that long. Once the
// daemon answers again, the follower reads the log's end, then the role,
// then the log's end again and the events up to it: when one changed the
// role, the role read may predate it, so it reads the role again. It
// keeps its cursor: refusals meanwhile are still reported, and
// role changes up to the end it looked at are skipped because the role read
// holds them. A closed client makes the role RoleUnknown without a callback.
func (f *Failover) follow(ctx context.Context, cursor int64) {
	f.follower.Store(goroutineID())
	f.reportStartRole()
	progress := &followProgress{cursor: cursor, delay: firstRetryDelay, confirmed: time.Now()}
	for ctx.Err() == nil {
		if err := f.readDaemon(ctx, progress); err != nil {
			if !f.retryAfterError(ctx, err, progress) {
				return
			}
			continue
		}
		progress.delay = firstRetryDelay
	}
}

// readDaemon makes the follower's next read: the log's end or the role when
// a role read is pending, otherwise one long-poll.
func (f *Failover) readDaemon(ctx context.Context, progress *followProgress) error {
	f.loseUnconfirmedRole(progress)
	if progress.roleReadPending {
		if progress.logEndPending {
			var end *EventList
			err := f.deadlineRead(ctx, progress, func(readContext context.Context) error {
				var endErr error
				end, endErr = f.client.Events(readContext, &EventsArgs{After: -1})
				return endErr
			})
			if err != nil {
				return err
			}
			progress.roleReadLogEnd = end.Next
			progress.logEndPending = false
		}
		var status *DatabaseStatus
		err := f.deadlineRead(ctx, progress, func(readContext context.Context) error {
			var statusErr error
			status, statusErr = f.client.Database(readContext, f.name)
			return statusErr
		})
		if err != nil {
			return err
		}
		superseded, err := f.roleSuperseded(ctx, progress)
		if err != nil {
			return err
		}
		if superseded {
			// The role read may predate a role change the log now holds:
			// read the role again, then look past the new log end.
			return nil
		}
		progress.confirmed = time.Now()
		progress.roleReadPending = false
		if ctx.Err() == nil {
			f.changeRole(statusRole(status))
		}
		return nil
	}
	var page *EventList
	err := f.deadlineRead(ctx, progress, func(readContext context.Context) error {
		var pageErr error
		page, pageErr = f.client.Events(
			readContext,
			&EventsArgs{After: progress.cursor, Wait: f.eventWait, Limit: eventPage},
		)
		return pageErr
	})
	if err != nil && IsCode(err, CodeCursorExpired) && ctx.Err() == nil {
		f.reportError(err)
		var end *EventList
		err = f.deadlineRead(ctx, progress, func(readContext context.Context) error {
			var endErr error
			end, endErr = f.client.Events(readContext, &EventsArgs{After: -1})
			return endErr
		})
		if err != nil {
			return err
		}
		progress.cursor = end.Next
		progress.roleReadLogEnd = end.Next
		progress.roleReadPending = true
		progress.logEndPending = false
		return nil
	}
	if err != nil {
		return err
	}
	progress.confirmed = time.Now()
	roleEvents := 0
	for index := range page.Events {
		event := &page.Events[index]
		progress.cursor = max(progress.cursor, event.ID)
		olderThanRoleRead := changesRole(event.Type) && event.ID <= progress.roleReadLogEnd
		if event.Name == f.name && ctx.Err() == nil {
			f.applyEvent(event, olderThanRoleRead)
			if changesRole(event.Type) && !olderThanRoleRead {
				roleEvents++
			}
		}
	}
	progress.cursor = max(progress.cursor, page.Next)
	if roleEvents == 0 && f.Role() == RoleUnknown {
		// A role unknown since a draining event is read again after every
		// long-poll that brought no role change: a drain that failed says
		// nothing more, and its status shows the role once it stopped.
		progress.roleReadPending = true
		progress.logEndPending = true
	}
	return nil
}

// roleSuperseded reads the log's end as of now, then the events from
// roleReadLogEnd through it, without waiting, and reports whether one changed
// this database's role, so a role read that raced it is not trusted.
// roleReadLogEnd moves to that end; events after it came after the role read.
// When pruning passed roleReadLogEnd, it reports true with logEndPending set,
// so the follower starts again from a fresh log end.
func (f *Failover) roleSuperseded(
	ctx context.Context,
	progress *followProgress,
) (bool, error) {
	var end *EventList
	err := f.deadlineRead(ctx, progress, func(readContext context.Context) error {
		var endErr error
		end, endErr = f.client.Events(readContext, &EventsArgs{After: -1})
		return endErr
	})
	if err != nil {
		return false, err
	}
	superseded := false
	for progress.roleReadLogEnd < end.Next {
		var page *EventList
		err := f.deadlineRead(ctx, progress, func(readContext context.Context) error {
			var pageErr error
			page, pageErr = f.client.Events(
				readContext,
				&EventsArgs{After: progress.roleReadLogEnd, Limit: eventPage},
			)
			return pageErr
		})
		if IsCode(err, CodeCursorExpired) {
			progress.logEndPending = true
			return true, nil
		}
		if err != nil {
			return false, err
		}
		for index := range page.Events {
			event := &page.Events[index]
			if event.ID > end.Next {
				break
			}
			if event.Name == f.name && changesRole(event.Type) {
				superseded = true
			}
		}
		if len(page.Events) == 0 {
			break
		}
		// Never past end: a role change after it came after the role read,
		// and the long-poll applies it.
		progress.roleReadLogEnd = max(
			progress.roleReadLogEnd,
			min(page.Events[len(page.Events)-1].ID, end.Next),
		)
	}
	progress.roleReadLogEnd = max(progress.roleReadLogEnd, end.Next)
	return superseded, nil
}

// deadlineRead runs one read with a deadline of confirmationLimit after the
// last confirmation, or after now once the role is RoleUnknown. A read the
// deadline ended is timeout.
func (f *Failover) deadlineRead(
	ctx context.Context,
	progress *followProgress,
	read func(readContext context.Context) error,
) error {
	start := progress.confirmed
	if f.Role() == RoleUnknown {
		start = time.Now()
	}
	readContext, cancel := context.WithDeadline(ctx, start.Add(f.confirmationLimit))
	defer cancel()
	err := read(readContext)
	if err != nil && ctx.Err() == nil && readContext.Err() != nil {
		return &Error{
			Code: CodeTimeout,
			Message: "the daemon did not answer within " + f.confirmationLimit.String() +
				", so the failover's role is unknown",
			Err: context.DeadlineExceeded,
		}
	}
	return err
}

// loseUnconfirmedRole makes the role RoleUnknown once the daemon has not
// confirmed it for confirmationLimit.
func (f *Failover) loseUnconfirmedRole(progress *followProgress) {
	if f.Role() != RoleUnknown && time.Since(progress.confirmed) >= f.confirmationLimit {
		f.loseRole(progress)
	}
}

// clearRole makes the role RoleUnknown without calling OnRoleChange.
func (f *Failover) clearRole() {
	f.mutex.Lock()
	f.role = RoleUnknown
	f.mutex.Unlock()
}

// loseRole makes the role RoleUnknown and, when that changed it, has the
// follower read the log's end and the role once the daemon answers again.
func (f *Failover) loseRole(progress *followProgress) {
	if f.changeRole(RoleUnknown) {
		progress.roleReadPending = true
		progress.logEndPending = true
	}
}

// unanswered reports whether a read failed without an answer from the daemon:
// an error the SDK raised itself, with Status 0.
func unanswered(err error) bool {
	var raised *Error
	return !errors.As(err, &raised) || raised == nil || raised.Status == 0
}

// retryAfterError makes the role RoleUnknown when the daemon did not answer
// or following ends, reports the failed read to OnError, and waits out the
// backoff, doubling it; while the role is known, the wait ends when the
// confirmation limit passes. It returns false when following ends: ctx
// ended, or the error is sdk_unsupported or closed.
func (f *Failover) retryAfterError(ctx context.Context, err error, progress *followProgress) bool {
	if ctx.Err() != nil {
		return false
	}
	closed := IsCode(err, CodeClosed)
	ending := closed || IsCode(err, CodeSDKUnsupported)
	switch {
	case closed:
		// The client is closing and stops following next: no role callback.
		f.clearRole()
	case ending || unanswered(err):
		f.loseRole(progress)
	}
	f.reportError(err)
	if ending {
		return false
	}
	delay := progress.delay
	if f.Role() != RoleUnknown {
		delay = max(0, min(delay, time.Until(progress.confirmed.Add(f.confirmationLimit))))
	}
	select {
	case <-ctx.Done():
		return false
	case <-time.After(delay):
	}
	progress.delay = min(progress.delay*2, maximumRetryDelay)
	return true
}

// reportError hands a failed read to OnError.
func (f *Failover) reportError(err error) {
	if f.args.OnError != nil {
		f.notify(func() { f.args.OnError(err) })
	}
}
