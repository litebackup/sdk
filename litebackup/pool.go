package litebackup

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const (
	// DefaultCleanupTimeout bounds closing and releasing a leased database
	// when PoolArgs.CleanupTimeout is 0.
	DefaultCleanupTimeout = 10 * time.Minute
	// DefaultOffloadIdleClose is how long a database a backup rule offloads
	// (MaximumLocalDatabases) stays open unused before the pool closes it, so
	// the rule can offload it, when PoolArgs.IdleClose is 0. It is well inside
	// the client's 10-minute idle window.
	DefaultOffloadIdleClose = 2 * time.Minute
	// DefaultIdleRelease is how long a leased database stays leased unused
	// before the pool releases it, when PoolArgs.IdleRelease is 0, so another
	// machine's lease with a LockTimeout gets it.
	DefaultIdleRelease = time.Minute
	// cloudDurabilityPoll is how often a lease with cloud durability asks
	// whether the bucket holds its version yet.
	cloudDurabilityPoll = time.Second
)

// poolCheckInterval is how often a pool looks for idle databases; tests
// shorten it.
var poolCheckInterval = 15 * time.Second

// journalModeRollback is the journal_mode the client reports for a
// rollback-journal database.
const journalModeRollback = "rollback"

// LeaseCleanupStep is the step of a lease release that failed.
type LeaseCleanupStep string

const (
	// LeaseCleanupStepClose means closing the database's handle failed, so
	// it was not released: it stays acquired by this machine with its file
	// as it is. Close every connection, then call Pool.Release again.
	LeaseCleanupStepClose LeaseCleanupStep = "close"
	// LeaseCleanupStepRelease means the release itself failed, as with
	// still_open or an unreachable server. The file is intact; call
	// Pool.Release again before another machine needs the database.
	LeaseCleanupStepRelease LeaseCleanupStep = "release"
)

// LeaseCleanupError is a failed release of a leased database. It unwraps to
// an *Error with CodeLeaseCleanupFailed, whose Err is the cause.
type LeaseCleanupError struct {
	// Name is the database.
	Name string
	// Step is the step that failed.
	Step LeaseCleanupStep
	// Err is the lease_cleanup_failed *Error.
	Err *Error
}

func (e *LeaseCleanupError) Error() string { return e.Err.Error() }

func (e *LeaseCleanupError) Unwrap() error { return e.Err }

// PoolArgs configures NewPool.
type PoolArgs[DB any] struct {
	// Adapter opens and closes each database. It is required; SQLAdapter
	// adapts database/sql.
	Adapter Adapter[DB]
	// IdleClose closes a backup rule's database after this long without a
	// call, to cap open files. 0 keeps it open, except under a rule with
	// MaximumLocalDatabases, where it is DefaultOffloadIdleClose so the rule
	// can offload it.
	IdleClose time.Duration
	// IdleRelease releases a leased database after this long without a call,
	// DefaultIdleRelease when 0; a negative value keeps it until Release or
	// Close. The pool never asks another machine to release: the app
	// coordinates handovers, and another machine's lease with a LockTimeout
	// gets the database once this pool released it.
	IdleRelease time.Duration
	// CleanupTimeout bounds closing and releasing a leased database together;
	// DefaultCleanupTimeout when 0.
	CleanupTimeout time.Duration
	// MachineLabel is what other machines see as the writer of leased
	// databases.
	MachineLabel string
	// OnReleaseError is called, on a goroutine of its own, with each failure
	// of a close or release the pool ran on its own: an idle close of a
	// rule's database, or an IdleRelease (a *LeaseCleanupError). The database
	// stays as it was, and the pool tries again at its next check and on
	// Close.
	OnReleaseError func(err error)
}

// LeaseArgs configures a lease.
type LeaseArgs struct {
	// Path is the local file. When nothing is there, the pool creates an
	// empty file and its directory first, so a database neither the server
	// nor this machine has becomes a new one. The path this machine already
	// uses when empty.
	Path string
	// RequireExisting skips creating Path, so a database neither the server
	// nor this machine has fails with not_found.
	RequireExisting bool
	// Durability makes the call return only once the server, or the bucket
	// with DurabilityCloud, holds the version its work committed, without
	// releasing. Empty returns once the work returns and syncs in the
	// background. The release that ends the lease waits for the same
	// durability, the server when empty.
	Durability Durability
	// Offload removes the local file when the lease ends, once the release's
	// durability holds the final version, unless the last call failed.
	Offload bool
	// LockTimeout is how long the first call waits while another machine
	// holds the write lock, trying again until that machine releases it; 0
	// fails at once with lock_unavailable.
	LockTimeout time.Duration
	// AcquireTimeout bounds downloading the head.
	AcquireTimeout time.Duration
	// Maintenance checkpoints on release and VACUUMs past the freelist
	// threshold.
	Maintenance bool
}

// Pool keeps databases open for calls, the way an app serving many tenants
// needs them:
//
//	pool, err := litebackup.NewPool(client, &litebackup.PoolArgs[*sql.DB]{
//		Adapter: litebackup.SQLAdapter("sqlite", nil),
//	})
//	err = pool.Use(ctx, "tenants/42.sqlite", func(ctx context.Context, db *sql.DB) error {
//		_, err := db.ExecContext(ctx, "INSERT INTO notes (body) VALUES (?)", "hello")
//		return err
//	})
//
// Use is for databases a backup rule on this machine covers: they stay here,
// open between calls. Lease is for databases that move between machines: the
// first call acquires the write lock, and the pool keeps it, with the open
// handle, until IdleRelease runs out, Release, or Close. A pool is safe for
// concurrent use; Close it when done, never from inside a call.
type Pool[DB any] struct {
	client   *Client
	settings PoolArgs[DB]
	// background ends when the pool closes; stop cancels it.
	background context.Context
	stop       context.CancelFunc
	finished   sync.WaitGroup
	// calls counts running Use and Lease calls; Close waits for them.
	calls sync.WaitGroup

	mutex   sync.Mutex
	entries map[string]*poolEntry[DB]
	rules   map[string]BackupRule
	closed  bool
}

// poolEntry is one database of a pool.
type poolEntry[DB any] struct {
	name string
	// turn serializes opening, bringing back, acquiring, lease calls, and
	// releasing; Use calls share the open handle without it.
	turn sync.Mutex
	db   DB
	path string
	// opened is set while db is open.
	opened bool
	// leased is set while this pool holds the write lock through Lease.
	leased bool
	// leaseArgs are the last lease call's settings, used by the release.
	leaseArgs LeaseArgs
	// lastCallFailed keeps the release from offloading after a failed call.
	lastCallFailed bool
	// idleClose is when a rule's database closes unused; 0 never.
	idleClose time.Duration
	// closeAfterUse closes a rule's database after its last running call: a
	// rollback-journal database whose rule offloads such databases.
	closeAfterUse bool
	// The fields below are guarded by the pool's mutex.
	users    int
	lastUsed time.Time
}

// NewPool returns a pool that opens databases with args.Adapter.
func NewPool[DB any](client *Client, args *PoolArgs[DB]) (*Pool[DB], error) {
	if client == nil || args == nil || args.Adapter == nil {
		return nil, invalidArgument("a pool needs a client and an adapter")
	}
	if args.IdleClose < 0 || args.CleanupTimeout < 0 {
		return nil, invalidArgument("a pool's idle close and cleanup timeout must not be negative")
	}
	settings := *args
	if settings.CleanupTimeout == 0 {
		settings.CleanupTimeout = DefaultCleanupTimeout
	}
	if settings.IdleRelease == 0 {
		settings.IdleRelease = DefaultIdleRelease
	}
	background, stop := context.WithCancel(context.Background())
	pool := &Pool[DB]{
		client:     client,
		settings:   settings,
		background: background,
		stop:       stop,
		entries:    map[string]*poolEntry[DB]{},
		rules:      map[string]BackupRule{},
	}
	pool.finished.Go(pool.closeIdleDatabases)
	return pool, nil
}

// Use runs work with a database a backup rule on this machine covers. The
// pool opens it on first use and keeps it open; an offloaded copy is brought
// back first, never recreated empty. Calls on one database run concurrently
// on the same handle. A name no rule on this machine covers is refused with
// invalid_argument; use Lease for it.
func (p *Pool[DB]) Use(ctx context.Context, name string, work func(ctx context.Context, db DB) error) error {
	if work == nil {
		return invalidArgument("Use needs a function to run")
	}
	entry, err := p.join(name)
	if err != nil {
		return err
	}
	// Deferred in this order, so the close runs once the call left.
	defer p.closeAfterLastUse(entry)
	defer p.leave(entry)
	entry.turn.Lock()
	if entry.leased {
		entry.turn.Unlock()
		return invalidArgument(name + " is leased in this pool; use Lease for it")
	}
	if !entry.opened {
		if err := p.openRuleDatabase(ctx, entry); err != nil {
			entry.turn.Unlock()
			return err
		}
	}
	db := entry.db
	entry.turn.Unlock()
	return work(ctx, db)
}

// openRuleDatabase opens a rule's database, bringing an offloaded copy back
// first, and tries once more when the copy was offloaded between the two.
func (p *Pool[DB]) openRuleDatabase(ctx context.Context, entry *poolEntry[DB]) error {
	for attempt := 0; ; attempt++ {
		status, err := p.client.Database(ctx, entry.name)
		if err != nil {
			return err
		}
		if status.Role != DatabaseStatusRoleOrigin || status.Acquired || status.Failover ||
			status.BackupRuleID == "" {
			return invalidArgument(fmt.Sprintf(
				"%s is not a backup rule's database on this machine; use Lease for a database that moves between machines",
				entry.name,
			))
		}
		if status.State == DatabaseStatusStateOffloaded {
			if _, err := p.client.Acquire(ctx, entry.name, nil); err != nil {
				return err
			}
		}
		db, openErr := p.settings.Adapter.Open(ctx, status.Path)
		if openErr != nil {
			_, statErr := os.Stat(status.Path)
			if attempt == 0 && errors.Is(statErr, fs.ErrNotExist) {
				continue
			}
			return openErr
		}
		entry.db, entry.path, entry.opened = db, status.Path, true
		rule, err := p.backupRule(ctx, status.BackupRuleID)
		if err != nil {
			return err
		}
		entry.idleClose = p.settings.IdleClose
		if entry.idleClose == 0 && rule.MaximumLocalDatabases > 0 {
			entry.idleClose = DefaultOffloadIdleClose
		}
		// Rule offload takes such a database on the word that nothing keeps it
		// open while idle, which no lock shows, so the pool keeps that word.
		entry.closeAfterUse = status.JournalMode == journalModeRollback && rule.OffloadRollbackJournal
		return nil
	}
}

// backupRule returns a rule of this machine, listing the rules again when it
// is not known yet.
func (p *Pool[DB]) backupRule(ctx context.Context, ruleID string) (BackupRule, error) {
	p.mutex.Lock()
	rule, known := p.rules[ruleID]
	p.mutex.Unlock()
	if !known {
		rules, err := p.client.BackupRules(ctx)
		if err != nil {
			return BackupRule{}, err
		}
		p.mutex.Lock()
		for _, listed := range rules {
			p.rules[listed.ID] = listed
		}
		rule = p.rules[ruleID]
		p.mutex.Unlock()
	}
	return rule, nil
}

// closeAfterLastUse closes a rule's rollback-journal database whose rule
// offloads such databases once no Use call runs on it, so it is never open
// while the client counts it idle. A failed close keeps the entry for the
// idle check and Close to try again.
func (p *Pool[DB]) closeAfterLastUse(entry *poolEntry[DB]) {
	if !entry.closeAfterUse {
		return
	}
	entry.turn.Lock()
	defer entry.turn.Unlock()
	p.mutex.Lock()
	callsActive := entry.users > 0
	p.mutex.Unlock()
	if callsActive || !entry.opened || entry.leased {
		return
	}
	cleanupContext, cancel := context.WithTimeout(context.Background(), p.settings.CleanupTimeout)
	defer cancel()
	if err := p.settings.Adapter.Close(cleanupContext, entry.db); err != nil {
		p.reportReleaseError(fmt.Errorf("closing %s after its last call failed; the pool tries again: %w", entry.name, err))
		return
	}
	var closedDB DB
	entry.db, entry.opened = closedDB, false
	p.mutex.Lock()
	if p.entries[entry.name] == entry && entry.users == 0 {
		delete(p.entries, entry.name)
	}
	p.mutex.Unlock()
}

// Lease runs work with a database that moves between machines, exclusively:
// calls on one name run one at a time, and no other machine writes while the
// pool keeps the lock. The first call acquires the write lock, creating an
// empty file at args.Path when nothing is there; later calls reuse the lock
// and the open handle after checking this machine still holds it. With
// args.Durability the call returns only once the server, or the bucket,
// holds what the work committed.
//
// The lock is released, after a final sync and closing the handle, after
// IdleRelease, on Release, or on Close; the pool never gives it up because
// another machine waits. A call that finds the lock lost fails with
// lock_lost without running work, and the next call acquires again. The
// adapters never commit for the work: commit what it should keep.
func (p *Pool[DB]) Lease(
	ctx context.Context,
	name string,
	args *LeaseArgs,
	work func(ctx context.Context, db DB) error,
) error {
	if args == nil {
		args = &LeaseArgs{}
	}
	if work == nil {
		return invalidArgument("Lease needs a function to run")
	}
	if args.Durability != "" && !args.Durability.Valid() {
		return invalidArgument("a lease's durability must be server or cloud")
	}
	if args.LockTimeout < 0 || args.AcquireTimeout < 0 {
		return invalidArgument("a lease's lock timeout and acquire timeout must not be negative")
	}
	if ctx.Err() != nil {
		return &Error{
			Code:    CodeCancelled,
			Message: "the lease was cancelled before it started",
			Err:     ctx.Err(),
		}
	}
	entry, err := p.join(name)
	if err != nil {
		return err
	}
	defer p.leave(entry)
	entry.turn.Lock()
	defer entry.turn.Unlock()
	if entry.opened && !entry.leased {
		return invalidArgument(name + " is a backup rule's database in this pool; use Use for it")
	}
	entry.leaseArgs = *args
	if entry.leased {
		if err := p.requireLease(ctx, entry); err != nil {
			return err
		}
	} else if err := p.acquireLease(ctx, entry, args); err != nil {
		return err
	}
	workErr := p.callLeaseWork(ctx, entry, work)
	if workErr == nil && ctx.Err() != nil {
		workErr = &Error{
			Code:    CodeCancelled,
			Message: "the lease was cancelled while its work ran",
			Err:     ctx.Err(),
		}
	}
	// The call counts as failed until its work and its durability wait both
	// finished.
	entry.lastCallFailed = true
	if workErr != nil {
		return workErr
	}
	if args.Durability != "" {
		if err := p.waitDurable(ctx, name, args.Durability); err != nil {
			return err
		}
	}
	entry.lastCallFailed = false
	return nil
}

// requireLease checks that this machine still holds a leased database's lock
// and that its handle is open, reopening a handle a failed release closed.
// A lost lock drops the lease and fails with lock_lost. The caller holds
// entry.turn.
func (p *Pool[DB]) requireLease(ctx context.Context, entry *poolEntry[DB]) error {
	status, err := p.client.Database(ctx, entry.name)
	if err != nil {
		return err
	}
	if !status.Acquired || status.State == DatabaseStatusStateConflicted {
		p.dropLostLease(entry)
		return &Error{
			Code: CodeLockLost,
			Message: fmt.Sprintf(
				"this machine lost the write lock of %s, so its copy is %s; the next lease acquires it again",
				entry.name,
				status.State,
			),
		}
	}
	if entry.opened {
		return nil
	}
	db, err := p.settings.Adapter.Open(ctx, entry.path)
	if err != nil {
		return err
	}
	entry.db, entry.opened = db, true
	return nil
}

// callLeaseWork calls work and marks the call failed when it panics or calls
// runtime.Goexit, as t.FailNow does, before letting either go on; the lease
// stays, and its release will not offload.
func (p *Pool[DB]) callLeaseWork(
	ctx context.Context,
	entry *poolEntry[DB],
	work func(ctx context.Context, db DB) error,
) error {
	workDone := false
	defer func() {
		if !workDone {
			entry.lastCallFailed = true
		}
	}()
	workErr := work(ctx, entry.db)
	workDone = true
	return workErr
}

// acquireLease acquires the write lock and opens the database; a database
// that opened nothing is released again at once.
func (p *Pool[DB]) acquireLease(ctx context.Context, entry *poolEntry[DB], args *LeaseArgs) error {
	if args.Path != "" && !args.RequireExisting {
		if err := createEmptyFile(args.Path); err != nil {
			return err
		}
	}
	acquired, err := p.client.Acquire(ctx, entry.name, &AcquireArgs{
		Path:         args.Path,
		Maintenance:  args.Maintenance,
		MachineLabel: p.settings.MachineLabel,
		Timeout:      args.AcquireTimeout,
		LockTimeout:  args.LockTimeout,
	})
	if err != nil {
		return err
	}
	if !acquired.Database.Acquired {
		return invalidArgument(entry.name + " is a backup rule's database on this machine; use Use for it")
	}
	path := acquired.Database.Path
	if path == "" {
		path, _ = absolutePath(args.Path)
	}
	db, err := p.settings.Adapter.Open(ctx, path)
	if err != nil {
		// Nothing is open: release the lock again, without offload.
		entry.path, entry.leased, entry.lastCallFailed = path, true, true
		return errors.Join(err, p.releaseLease(ctx, entry))
	}
	entry.db, entry.path, entry.opened, entry.leased = db, path, true, true
	return nil
}

// waitDurable syncs the database now and returns once the server, or the
// bucket with DurabilityCloud, holds the version the sync reached.
func (p *Pool[DB]) waitDurable(ctx context.Context, name string, durability Durability) error {
	status, err := p.client.Sync(ctx, name)
	if err != nil {
		return err
	}
	if durability != DurabilityCloud {
		return nil
	}
	target := status.SyncedServer.Version
	for status.SyncedCloud.Version < target {
		timer := time.NewTimer(cloudDurabilityPoll)
		select {
		case <-ctx.Done():
			timer.Stop()
			return &Error{
				Code:    CodeCancelled,
				Message: "the lease was cancelled while it waited for the bucket",
				Err:     ctx.Err(),
			}
		case <-timer.C:
		}
		status, err = p.client.Database(ctx, name)
		if err != nil {
			return err
		}
	}
	return nil
}

// Release ends the lease of a database: it closes the handle and releases the
// write lock once the final version reaches the lease's durability, after a
// running call returns. It fails with a *LeaseCleanupError when closing or
// releasing fails, and with invalid_argument for a database the pool does not
// lease.
func (p *Pool[DB]) Release(ctx context.Context, name string) error {
	p.mutex.Lock()
	entry := p.entries[name]
	p.mutex.Unlock()
	if entry == nil {
		return invalidArgument(name + " is not leased in this pool")
	}
	entry.turn.Lock()
	defer entry.turn.Unlock()
	if !entry.leased {
		return invalidArgument(name + " is not leased in this pool")
	}
	return p.releaseLease(ctx, entry)
}

// releaseLease closes the handle and releases the lock, within one cleanup
// timeout that runs even after ctx ends. The caller holds entry.turn.
func (p *Pool[DB]) releaseLease(ctx context.Context, entry *poolEntry[DB]) error {
	deadline := time.Now().Add(p.settings.CleanupTimeout)
	cleanupContext, cancel := context.WithDeadline(context.WithoutCancel(ctx), deadline)
	defer cancel()
	if entry.opened {
		if err := p.settings.Adapter.Close(cleanupContext, entry.db); err != nil {
			return &LeaseCleanupError{Name: entry.name, Step: LeaseCleanupStepClose, Err: &Error{
				Code: CodeLeaseCleanupFailed,
				Message: fmt.Sprintf(
					"closing %s failed, so it was not released and stays acquired by this machine: %v. Close every connection to %s, then call Release again",
					entry.name,
					err,
					entry.path,
				),
				Details: map[string]any{
					"step":     string(LeaseCleanupStepClose),
					"name":     entry.name,
					"path":     entry.path,
					"acquired": true,
				},
				Err: err,
			}}
		}
		var closedDB DB
		entry.db, entry.opened = closedDB, false
	}
	durability := entry.leaseArgs.Durability
	if durability == "" {
		durability = DurabilityServer
	}
	timeout := max(time.Until(deadline).Round(time.Millisecond), time.Millisecond)
	releaseContext, cancelRelease := context.WithTimeout(context.WithoutCancel(ctx), timeout+deadlineGrace)
	defer cancelRelease()
	_, err := p.client.Release(releaseContext, entry.name, &ReleaseArgs{
		Durability: durability,
		Offload:    entry.leaseArgs.Offload && !entry.lastCallFailed,
		Timeout:    timeout,
	})
	if err != nil {
		return &LeaseCleanupError{Name: entry.name, Step: LeaseCleanupStepRelease, Err: &Error{
			Code: CodeLeaseCleanupFailed,
			Message: fmt.Sprintf(
				"releasing %s failed: %v. Its file at %s is intact and this machine may still hold its write lock; call Release again",
				entry.name,
				err,
				entry.path,
			),
			Details: map[string]any{
				"step":       string(LeaseCleanupStepRelease),
				"name":       entry.name,
				"path":       entry.path,
				"durability": string(durability),
			},
			Err: err,
		}}
	}
	entry.leased = false
	p.mutex.Lock()
	if p.entries[entry.name] == entry && entry.users == 0 {
		delete(p.entries, entry.name)
	}
	p.mutex.Unlock()
	return nil
}

// dropLostLease closes the handle of a database whose lock was lost and
// forgets the lease; the copy stays conflicted, its file as it was. The
// caller holds entry.turn.
func (p *Pool[DB]) dropLostLease(entry *poolEntry[DB]) {
	if entry.opened {
		cleanupContext, cancel := context.WithTimeout(context.Background(), p.settings.CleanupTimeout)
		_ = p.settings.Adapter.Close(cleanupContext, entry.db)
		cancel()
		var closedDB DB
		entry.db, entry.opened = closedDB, false
	}
	entry.leased = false
}

// Close refuses new calls, waits for running ones, then releases every
// leased database and closes every open one. It returns every failure
// joined; a *LeaseCleanupError names a database that stays acquired. Never
// call it from inside a Use or Lease call.
func (p *Pool[DB]) Close(ctx context.Context) error {
	p.mutex.Lock()
	if p.closed {
		p.mutex.Unlock()
		return nil
	}
	p.closed = true
	p.mutex.Unlock()
	p.stop()
	p.finished.Wait()
	p.calls.Wait()
	p.mutex.Lock()
	entries := make([]*poolEntry[DB], 0, len(p.entries))
	for _, entry := range p.entries {
		entries = append(entries, entry)
	}
	p.mutex.Unlock()
	var failures []error
	for _, entry := range entries {
		entry.turn.Lock()
		switch {
		case entry.leased:
			failures = append(failures, p.releaseLease(ctx, entry))
		case entry.opened:
			cleanupContext, cancel := context.WithTimeout(context.WithoutCancel(ctx), p.settings.CleanupTimeout)
			failures = append(failures, p.settings.Adapter.Close(cleanupContext, entry.db))
			cancel()
			entry.opened = false
		}
		entry.turn.Unlock()
	}
	return errors.Join(failures...)
}

// join counts a call on the name's entry, creating it.
func (p *Pool[DB]) join(name string) (*poolEntry[DB], error) {
	if name == "" {
		return nil, invalidArgument("a pool call needs a database name")
	}
	p.mutex.Lock()
	defer p.mutex.Unlock()
	if p.closed {
		return nil, &Error{Code: CodeClosed, Message: "the pool is closed"}
	}
	entry := p.entries[name]
	if entry == nil {
		entry = &poolEntry[DB]{name: name}
		p.entries[name] = entry
	}
	entry.users++
	p.calls.Add(1)
	return entry, nil
}

// leave ends a call on an entry.
func (p *Pool[DB]) leave(entry *poolEntry[DB]) {
	p.mutex.Lock()
	entry.users--
	entry.lastUsed = time.Now()
	p.mutex.Unlock()
	p.calls.Done()
}

// closeIdleDatabases closes rule databases past their idle close and releases
// leases past IdleRelease, until the pool closes.
func (p *Pool[DB]) closeIdleDatabases() {
	ticker := time.NewTicker(poolCheckInterval)
	defer ticker.Stop()
	for {
		select {
		case <-p.background.Done():
			return
		case <-ticker.C:
		}
		now := time.Now()
		var idle []*poolEntry[DB]
		p.mutex.Lock()
		for _, entry := range p.entries {
			if entry.users == 0 && !entry.lastUsed.IsZero() {
				idle = append(idle, entry)
			}
		}
		p.mutex.Unlock()
		for _, entry := range idle {
			p.reportReleaseError(p.closeIdleEntry(entry, now))
		}
	}
}

// closeIdleEntry closes or releases one entry when it is still idle. A
// failed close keeps the entry and its handle, so the next check and Close
// try again.
func (p *Pool[DB]) closeIdleEntry(entry *poolEntry[DB], now time.Time) error {
	if !entry.turn.TryLock() {
		return nil
	}
	defer entry.turn.Unlock()
	p.mutex.Lock()
	idleTime := now.Sub(entry.lastUsed)
	callsActive := entry.users > 0
	p.mutex.Unlock()
	if callsActive {
		return nil
	}
	switch {
	case entry.leased && p.settings.IdleRelease > 0 && idleTime >= p.settings.IdleRelease:
		return p.releaseLease(p.background, entry)
	case !entry.leased && entry.opened && entry.idleClose > 0 && idleTime >= entry.idleClose:
		cleanupContext, cancel := context.WithTimeout(p.background, p.settings.CleanupTimeout)
		defer cancel()
		if err := p.settings.Adapter.Close(cleanupContext, entry.db); err != nil {
			return fmt.Errorf("closing %s after it was idle failed; the pool tries again: %w", entry.name, err)
		}
		var closedDB DB
		entry.db, entry.opened = closedDB, false
		p.mutex.Lock()
		if p.entries[entry.name] == entry && entry.users == 0 {
			delete(p.entries, entry.name)
		}
		p.mutex.Unlock()
	}
	return nil
}

// reportReleaseError hands a failed background close or release to
// OnReleaseError on a goroutine of its own, outside every lock and outside
// what Close waits for.
func (p *Pool[DB]) reportReleaseError(err error) {
	if err != nil && p.settings.OnReleaseError != nil {
		go p.settings.OnReleaseError(err)
	}
}

// createEmptyFile creates an empty file and its directory unless something
// is already at path.
func createEmptyFile(path string) error {
	absolute, err := absolutePath(path)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(absolute), 0o755); err != nil {
		return &Error{
			Code:    CodeInvalidArgument,
			Message: "creating " + absolute + " failed: " + err.Error(),
			Err:     err,
		}
	}
	file, err := os.OpenFile(absolute, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if errors.Is(err, fs.ErrExist) {
		return nil
	}
	if err != nil {
		return &Error{
			Code:    CodeInvalidArgument,
			Message: "creating " + absolute + " failed: " + err.Error(),
			Err:     err,
		}
	}
	return file.Close()
}
