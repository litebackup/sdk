// Package litebackup backs up, replicates, and restores SQLite databases with
// Litebackup from a Go program. It talks to the litebackup daemon of a session
// over its unix socket, as the litebackup command does, and starts the
// daemon when it is not running, with the credential in LITEBACKUP_API_KEY.
//
// The everyday calls are package functions over a shared client:
//
//	_, err := litebackup.StartBackup(ctx, "/var/lib/app/main.db", nil)
//
// Connect returns a Client for everything else. Managed runs code against a
// database it acquires, opens, closes, and releases.
package litebackup

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// SDKVersion is the version sent with every call. Daemons refuse SDKs older
// than their minimum with sdk_unsupported.
const SDKVersion = "1.4.0"

// MinimumDaemonVersion is the oldest daemon this SDK talks to. Connect
// refuses an older one with daemon_unsupported.
const MinimumDaemonVersion = "1.1.0"

const (
	sdkVersionHeader    = "Litebackup-SDK-Version"
	clientVersionHeader = "Litebackup-Client-Version"
	// responseLimit bounds one decoded response.
	responseLimit = 64 << 20
	// deadlineGrace is added to a call's server-side timeout for its
	// client-side deadline, so the daemon's own timeout answer arrives first.
	deadlineGrace = 30 * time.Second
)

// ConnectArgs selects the session a Client talks to.
type ConnectArgs struct {
	// APIKey is the credential the daemon uses when it starts: an API key,
	// or a join token it exchanges once for a key of this machine, then @
	// and the server's address. It overrides LITEBACKUP_API_KEY. The daemon
	// keeps the key it saved unless this holds a different credential, which
	// the launch refuses with CodeDrainRequired while the client holds write
	// locks; a daemon already running keeps its key until it restarts.
	// Foreground backups use it too.
	APIKey string
	// Session names the session; LITEBACKUP_SESSION, then "default", when empty.
	Session string
	// SocketPath connects to this socket and never starts a daemon.
	SocketPath string
	// BinaryPath is the litebackup binary that starts the daemon; see Binary
	// for the search when empty.
	BinaryPath string
}

// Client talks to the daemon of one session.
type Client struct {
	customSocket bool
	session      string
	socketPath   string
	binaryPath   string
	// apiKey is ConnectArgs.APIKey, passed to foreground backups.
	apiKey string
	http   *http.Client

	mutex         sync.Mutex
	clientVersion string
	failovers     map[*Failover]struct{}
	closed        bool
	// lifetime ends with Close, which aborts calls in flight.
	lifetime context.Context
	end      context.CancelFunc
}

// Connect connects to a session's daemon, starting it like the litebackup
// command does when nothing listens on its socket. It refuses a daemon older
// than MinimumDaemonVersion with daemon_unsupported.
func Connect(ctx context.Context, args *ConnectArgs) (*Client, error) {
	if args == nil {
		args = &ConnectArgs{}
	}
	if _, err := checkCredential(apiKeyCredential(args)); err != nil {
		return nil, err
	}
	session := args.Session
	socket := args.SocketPath
	if socket == "" {
		resolved, path, err := SessionSocket(args.Session)
		if err != nil {
			return nil, invalidArgument(err.Error())
		}
		session, socket = resolved, path
		if err := launchDaemon(ctx, session, socket, args.APIKey, args.BinaryPath); err != nil {
			return nil, err
		}
	}
	lifetime, end := context.WithCancel(context.Background())
	client := &Client{
		customSocket: args.SocketPath != "",
		session:      session,
		socketPath:   socket,
		binaryPath:   args.BinaryPath,
		apiKey:       args.APIKey,
		lifetime:     lifetime,
		end:          end,
		http: &http.Client{Transport: &http.Transport{
			DialContext: func(ctx context.Context, _ string, _ string) (net.Conn, error) {
				var dialer net.Dialer
				return dialer.DialContext(ctx, "unix", socket)
			},
			MaxIdleConns: 4,
		}},
	}
	if err := client.checkDaemon(ctx); err != nil {
		client.http.CloseIdleConnections()
		end()
		return nil, err
	}
	return client, nil
}

// checkDaemon sends the connect probe and refuses a daemon older than
// MinimumDaemonVersion.
func (c *Client) checkDaemon(ctx context.Context) error {
	var status Status
	if err := c.call(ctx, http.MethodGet, "/v1/status/show?limit=1", nil, &status, 0); err != nil {
		return err
	}
	if c.session == "" {
		c.session = status.Session
	}
	version := c.ClientVersion()
	if version != "" && compareVersions(version, MinimumDaemonVersion) >= 0 {
		return nil
	}
	reported := "does not report its version"
	if version != "" {
		reported = "is version " + version
	}
	restart := "Stop it with `litebackup drain` so the next call starts the new binary"
	if c.session != "" {
		restart = "Stop it with `litebackup drain --session " + c.session + "` so the next call starts the new binary"
	}
	return &Error{
		Code: CodeDaemonUnsupported,
		Message: fmt.Sprintf(
			"the litebackup daemon %s, and this SDK needs %s or later. %s, or restart the service if `litebackup install service` runs it",
			reported,
			MinimumDaemonVersion,
			restart,
		),
	}
}

// compareVersions orders dotted numeric versions, ignoring any -suffix;
// unparsable parts count as zero.
func compareVersions(left string, right string) int {
	leftParts := strings.Split(strings.SplitN(left, "-", 2)[0], ".")
	rightParts := strings.Split(strings.SplitN(right, "-", 2)[0], ".")
	for index := range max(len(leftParts), len(rightParts)) {
		var leftNumber, rightNumber int
		if index < len(leftParts) {
			leftNumber, _ = strconv.Atoi(leftParts[index])
		}
		if index < len(rightParts) {
			rightNumber, _ = strconv.Atoi(rightParts[index])
		}
		if leftNumber != rightNumber {
			return leftNumber - rightNumber
		}
	}
	return 0
}

// Session is the session this client talks to.
func (c *Client) Session() string { return c.session }

// SocketPath is the daemon's socket.
func (c *Client) SocketPath() string { return c.socketPath }

// ClientVersion is the daemon's version, from its last answer.
func (c *Client) ClientVersion() string {
	c.mutex.Lock()
	defer c.mutex.Unlock()
	return c.clientVersion
}

// Close stops following the client's failovers, ends its calls in flight, and
// closes its connections. The daemon keeps running, and so do the failovers
// and any acquired databases. Calls after Close fail with closed.
func (c *Client) Close() error {
	c.mutex.Lock()
	failovers := c.failovers
	c.failovers = nil
	c.closed = true
	c.mutex.Unlock()
	// Calls in flight end first, so a failover callback waiting on one
	// returns and stopFollowing does not wait on it forever.
	c.end()
	for failover := range failovers {
		failover.stopFollowing()
	}
	c.http.CloseIdleConnections()
	return nil
}

// isClosed reports whether Close ran.
func (c *Client) isClosed() bool {
	c.mutex.Lock()
	defer c.mutex.Unlock()
	return c.closed
}

// closedError is the error of a call on a closed client.
func closedError() *Error {
	return &Error{Code: CodeClosed, Message: "the client is closed"}
}

// addFailover records a failover so Close stops following it; it reports
// false once the client is closed.
func (c *Client) addFailover(failover *Failover) bool {
	c.mutex.Lock()
	defer c.mutex.Unlock()
	if c.closed {
		return false
	}
	if c.failovers == nil {
		c.failovers = map[*Failover]struct{}{}
	}
	c.failovers[failover] = struct{}{}
	return true
}

// removeFailover forgets a stopped failover.
func (c *Client) removeFailover(failover *Failover) {
	c.mutex.Lock()
	defer c.mutex.Unlock()
	delete(c.failovers, failover)
}

// call sends one request and decodes its JSON answer into result. A positive
// serverTimeout gives the call a client-side deadline of that plus
// deadlineGrace; otherwise only ctx and Close end the wait.
func (c *Client) call(
	ctx context.Context,
	method string,
	path string,
	body any,
	answer any,
	serverTimeout time.Duration,
) error {
	if c.isClosed() {
		return closedError()
	}
	callContext, cancel := context.WithCancel(ctx)
	defer cancel()
	stop := context.AfterFunc(c.lifetime, cancel)
	defer stop()
	if serverTimeout > 0 {
		var cancelDeadline context.CancelFunc
		callContext, cancelDeadline = context.WithTimeout(callContext, serverTimeout+deadlineGrace)
		defer cancelDeadline()
	}
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return invalidArgument(err.Error())
		}
		reader = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(
		callContext,
		method,
		"http://litebackup"+path,
		reader,
	)
	if err != nil {
		return invalidArgument(err.Error())
	}
	request.Header.Set(sdkVersionHeader, "go/"+SDKVersion)
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := c.http.Do(request)
	if err != nil {
		return c.transportError(ctx, callContext, err)
	}
	defer response.Body.Close()
	version := response.Header.Get(clientVersionHeader)
	c.mutex.Lock()
	c.clientVersion = version
	c.mutex.Unlock()
	content, err := io.ReadAll(io.LimitReader(response.Body, responseLimit))
	if err != nil {
		return c.transportError(ctx, callContext, err)
	}
	if response.StatusCode != http.StatusOK {
		return responseError(response.StatusCode, content)
	}
	if answer == nil {
		return nil
	}
	if err := json.Unmarshal(content, answer); err != nil {
		return &Error{
			Code:    CodeInvalidResponse,
			Message: fmt.Sprintf("decoding %s: %v", path, err),
			Status:  response.StatusCode,
			Err:     err,
		}
	}
	return nil
}

// transportError names why a call ended without an answer: the caller
// cancelled, the client closed, the deadline passed, or the connection failed.
func (c *Client) transportError(ctx context.Context, callContext context.Context, err error) error {
	switch {
	case ctx.Err() != nil:
		return &Error{
			Code:    CodeCancelled,
			Message: "the call was cancelled; the daemon may still finish the operation",
			Err:     ctx.Err(),
		}
	case c.isClosed():
		return closedError()
	case callContext.Err() != nil:
		return &Error{
			Code:    CodeTimeout,
			Message: "the daemon did not answer within the call's timeout; it may still finish the operation",
			Err:     context.DeadlineExceeded,
		}
	default:
		return &Error{Code: CodeConnectionFailed, Message: err.Error(), Err: err}
	}
}

// absolutePath resolves a relative path against the working directory and
// cleans it; the daemon needs absolute paths.
func absolutePath(path string) (string, error) {
	if path == "" {
		return "", nil
	}
	if filepath.IsAbs(path) {
		return filepath.Clean(path), nil
	}
	directory, err := os.Getwd()
	if err != nil {
		return "", err
	}
	return filepath.Join(directory, path), nil
}

// millis renders a duration as the API's milliseconds, refusing a negative one.
func millis(duration time.Duration, name string) (int64, error) {
	if duration < 0 {
		return 0, invalidArgument(name + " must not be negative")
	}
	return duration.Milliseconds(), nil
}

// Status returns the session, its server, and the first page of databases on this machine.
func (c *Client) Status(ctx context.Context) (*Status, error) {
	return c.StatusPage(ctx, nil)
}

// StatusPageArgs selects a page of Status. Nil reads the first page of 100.
type StatusPageArgs struct {
	// After returns databases whose names sort after this one; pass Next to continue.
	After string
	// Limit returns at most this many databases, up to 1000; 100 when 0.
	Limit int
}

// StatusPage returns the session, its server, and one page of databases.
func (c *Client) StatusPage(ctx context.Context, args *StatusPageArgs) (*Status, error) {
	if args == nil {
		args = &StatusPageArgs{}
	}
	query, err := pageQuery(args.After, args.Limit)
	if err != nil {
		return nil, err
	}
	var status Status
	if err := c.call(ctx, http.MethodGet, "/v1/status/show?"+query, nil, &status, 0); err != nil {
		return nil, err
	}
	return &status, nil
}

// defaultPageLimit is the page size when a page's Limit is 0.
const defaultPageLimit = 100

// maximumPageLimit is the largest page of databases the daemon returns.
const maximumPageLimit = 1000

// pageQuery encodes a name cursor and a limit, defaulting the limit to 100.
func pageQuery(after string, limit int) (string, error) {
	if limit < 0 || limit > maximumPageLimit {
		return "", invalidArgument("the limit must be from 1 to 1000, or 0 for 100")
	}
	if limit == 0 {
		limit = defaultPageLimit
	}
	return url.Values{"after": {after}, "limit": {strconv.Itoa(limit)}}.Encode(), nil
}

// Databases returns every database. AllDatabases iterates with bounded memory.
func (c *Client) Databases(ctx context.Context) ([]DatabaseStatus, error) {
	var databases []DatabaseStatus
	for database, err := range c.AllDatabases(ctx) {
		if err != nil {
			return nil, err
		}
		databases = append(databases, database)
	}
	return databases, nil
}

// DatabasesPageArgs selects a page of databases. Nil reads the first page of 100.
type DatabasesPageArgs struct {
	// After returns databases whose names sort after this one; pass Next to continue.
	After string
	// Limit returns at most this many databases, up to 1000; 100 when 0.
	Limit int
}

// DatabasesPage returns one page of databases. Pass Next as After to continue.
func (c *Client) DatabasesPage(
	ctx context.Context,
	args *DatabasesPageArgs,
) (*DatabaseStatusList, error) {
	if args == nil {
		args = &DatabasesPageArgs{}
	}
	query, err := pageQuery(args.After, args.Limit)
	if err != nil {
		return nil, err
	}
	var page DatabaseStatusList
	if err := c.call(ctx, http.MethodGet, "/v1/databases/list?"+query, nil, &page, 0); err != nil {
		return nil, err
	}
	return &page, nil
}

// Database returns one database's role, state, and synced versions.
func (c *Client) Database(ctx context.Context, name string) (*DatabaseStatus, error) {
	var status DatabaseStatus
	if err := c.call(ctx, http.MethodGet, "/v1/databases/status?"+url.Values{"name": {name}}.Encode(), nil, &status, 0); err != nil {
		return nil, err
	}
	return &status, nil
}

// Sync syncs a database now and returns once the server acknowledged it.
func (c *Client) Sync(ctx context.Context, name string) (*DatabaseStatus, error) {
	var status DatabaseStatus
	if err := c.call(ctx, http.MethodPost, "/v1/databases/sync", NameRequest{Name: name}, &status, 0); err != nil {
		return nil, err
	}
	return &status, nil
}

// VersionsArgs pages through versions.
type VersionsArgs struct {
	// After skips versions up to and including this one.
	After int64
	// Limit returns at most this many versions; every version when 0.
	Limit int
}

// Versions lists the versions of a database the server can restore, oldest
// first. AllVersions iterates with bounded memory.
func (c *Client) Versions(
	ctx context.Context,
	name string,
	args *VersionsArgs,
) ([]RestorableVersion, error) {
	versions := []RestorableVersion{}
	for version, err := range c.AllVersions(ctx, name, args) {
		if err != nil {
			return nil, err
		}
		versions = append(versions, version)
	}
	return versions, nil
}

// VersionsPage returns one page of versions after args.After, at most
// args.Limit when positive. Pass Next as After to continue; 0 means no more.
func (c *Client) VersionsPage(
	ctx context.Context,
	name string,
	args *VersionsArgs,
) (*VersionList, error) {
	if args == nil {
		args = &VersionsArgs{}
	}
	query := url.Values{"name": {name}}
	if args.After > 0 {
		query.Set("after", strconv.FormatInt(args.After, 10))
	}
	if args.Limit > 0 {
		query.Set("limit", strconv.Itoa(args.Limit))
	}
	var page VersionList
	if err := c.call(ctx, http.MethodGet, "/v1/databases/versions?"+query.Encode(), nil, &page, 0); err != nil {
		return nil, err
	}
	return &page, nil
}

// RestoreArgs picks the file and the version a restore writes.
type RestoreArgs struct {
	// Path is the new file; relative paths resolve against the working
	// directory. Its directory must exist, and an existing file is never
	// overwritten (file_exists).
	Path string
	// Version restores this version; the head when neither Version nor Time is set.
	Version int64
	// Time restores the newest version at or before this time.
	Time time.Time
	// Umask is the file mode creation mask the new file is created under:
	// it gets 0666 less it, as a file the caller created would. This
	// process's umask when nil.
	Umask *fs.FileMode
}

// Restore writes a version of a database to a new file and checks it.
func (c *Client) Restore(
	ctx context.Context,
	name string,
	args *RestoreArgs,
) (*RestoreResult, error) {
	if err := validateRestore(args); err != nil {
		return nil, err
	}
	path, err := absolutePath(args.Path)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"name": name, "path": path}
	if args.Version != 0 {
		body["version"] = args.Version
	}
	if !args.Time.IsZero() {
		body["time"] = args.Time.UTC().Format(time.RFC3339Nano)
	}
	umask, err := requestUmask(args.Umask)
	if err != nil {
		return nil, err
	}
	body["umask"] = *umask
	var restored RestoreResult
	if err := c.call(ctx, http.MethodPost, "/v1/databases/restore", body, &restored, 0); err != nil {
		return nil, err
	}
	return &restored, nil
}

// RevertArgs picks the version a revert publishes again: Version or Time,
// one of them.
type RevertArgs struct {
	// Version is the version whose content becomes the new head.
	Version int64
	// Time picks the newest version at or before it.
	Time time.Time
}

// Revert rolls a database back under its own name: the content of a past
// version becomes a new head, one past the current head, and every later
// version stays restorable. The database must be one this machine acquired
// or backs up, and its file closed (still_open otherwise); a replica or a
// failover standby is refused (invalid_argument), and a file holding writes
// the server lacks is refused with conflict and left untouched. The daemon
// takes the write lock when it does not hold it and gives it back once the
// new head is synced. When that sync fails for any reason but a lost lock or
// a refused file, which are final, the daemon keeps the lock and syncs again
// on its own, so release an acquired database afterwards.
func (c *Client) Revert(
	ctx context.Context,
	name string,
	args *RevertArgs,
) (*RevertResult, error) {
	if err := validateRevert(args); err != nil {
		return nil, err
	}
	body := map[string]any{"name": name}
	if args.Version != 0 {
		body["version"] = args.Version
	}
	if !args.Time.IsZero() {
		body["time"] = args.Time.UTC().Format(time.RFC3339Nano)
	}
	var reverted RevertResult
	if err := c.call(ctx, http.MethodPost, "/v1/databases/revert", body, &reverted, 0); err != nil {
		return nil, err
	}
	return &reverted, nil
}

// AcquireArgs configures an acquired database.
type AcquireArgs struct {
	// Path is the local file; the path this machine already uses when empty,
	// which brings an offloaded database back.
	Path string
	// Maintenance checkpoints on release and VACUUMs past the freelist threshold.
	Maintenance bool
	// OffloadOnRelease lets Litebackup remove the local copy once
	// OffloadDurability holds the released version; the copy stays when false.
	OffloadOnRelease bool
	// OffloadDurability is what an offload of the released copy waits for:
	// DurabilityServer, the default when empty, once the server holds its
	// last version, or DurabilityCloud once the bucket holds it too.
	OffloadDurability Durability
	// MachineLabel is what other machines see as the writer.
	MachineLabel string
	// Timeout bounds downloading the head.
	Timeout time.Duration
	// LockTimeout is how long to wait while another machine holds the write
	// lock, trying again until it is released; 0 fails at once with
	// lock_unavailable. The daemon tries again with backoff, and the call may
	// take Timeout plus LockTimeout.
	LockTimeout time.Duration
	// Umask is the file mode creation mask a file the acquire creates gets:
	// 0666 less it, as a file the caller created would. A copy the head
	// replaces keeps its bits. This process's umask when nil.
	Umask *fs.FileMode
	// DiscardLocal replaces a local file that diverged from the server head
	// with the head, discarding its writes the head lacks. Without it such a
	// file is refused with conflict and left untouched.
	DiscardLocal bool
}

// acquireRequest builds the wire request for name and args.
func acquireRequest(name string, args *AcquireArgs) (*AcquireRequest, error) {
	path, err := absolutePath(args.Path)
	if err != nil {
		return nil, err
	}
	timeout, err := millis(args.Timeout, "the acquire timeout")
	if err != nil {
		return nil, err
	}
	lockTimeout, err := millis(args.LockTimeout, "the lock timeout")
	if err != nil {
		return nil, err
	}
	body := &AcquireRequest{
		Name:              name,
		Path:              path,
		Maintenance:       args.Maintenance,
		DiscardLocal:      args.DiscardLocal,
		MachineLabel:      args.MachineLabel,
		TimeoutMillis:     timeout,
		LockTimeoutMillis: lockTimeout,
	}
	if err := validateOffloadDurability(args.OffloadDurability); err != nil {
		return nil, err
	}
	body.Umask, err = requestUmask(args.Umask)
	if err != nil {
		return nil, err
	}
	// The defaults stay off the wire, as the other SDKs leave them.
	body.OffloadOnRelease = args.OffloadOnRelease
	if args.OffloadDurability == DurabilityCloud {
		body.OffloadDurability = DurabilityCloud
	}
	return body, nil
}

// Acquire takes the write lock on a database and makes the local file the
// server's head, for worker pools that move databases between machines.
// Managed acquires, opens, closes, and releases around a function.
func (c *Client) Acquire(
	ctx context.Context,
	name string,
	args *AcquireArgs,
) (*AcquireResult, error) {
	if args == nil {
		args = &AcquireArgs{}
	}
	body, err := acquireRequest(name, args)
	if err != nil {
		return nil, err
	}
	var acquired AcquireResult
	if err := c.call(ctx, http.MethodPost, "/v1/databases/acquire", body, &acquired, acquireCallTimeout(args)); err != nil {
		return nil, err
	}
	return &acquired, nil
}

// acquireCallTimeout is how long an acquire's call may take: no bound when
// Timeout is 0, as the daemon then applies its own, and otherwise Timeout
// plus LockTimeout, the most the daemon spends waiting and downloading.
func acquireCallTimeout(args *AcquireArgs) time.Duration {
	if args.Timeout == 0 {
		return 0
	}
	return args.Timeout + args.LockTimeout
}

// ReleaseArgs configures a release.
type ReleaseArgs struct {
	// Durability is server or cloud; the daemon's default when empty.
	Durability Durability
	// Offload removes the local file once Durability holds the released version:
	// the server with server, the bucket with cloud.
	Offload bool
	// Timeout bounds waiting for the database to close and for Durability.
	Timeout time.Duration
}

// Release syncs an acquired database a final time, runs its maintenance, and
// releases its write lock. Every connection to it must be closed first, or
// it fails with still_open once Timeout runs out.
func (c *Client) Release(
	ctx context.Context,
	name string,
	args *ReleaseArgs,
) (*ReleaseResult, error) {
	if args == nil {
		args = &ReleaseArgs{}
	}
	timeout, err := millis(args.Timeout, "the release timeout")
	if err != nil {
		return nil, err
	}
	body := ReleaseRequest{Name: name, Offload: args.Offload, TimeoutMillis: timeout}
	if args.Durability != "" && args.Durability != DurabilityNone {
		body.Durability = args.Durability
	}
	var released ReleaseResult
	if err := c.call(ctx, http.MethodPost, "/v1/databases/release", body, &released, args.Timeout); err != nil {
		return nil, err
	}
	return &released, nil
}

// OffloadArgs configures an offload.
type OffloadArgs struct {
	// Durability is what the offload waits for before it removes the file:
	// DurabilityServer, the daemon's default when empty, once the server
	// holds the last version, or DurabilityCloud once the bucket holds it too.
	Durability Durability
}

// Offload removes the local file of a released database once the server
// holds its last version, or the bucket with DurabilityCloud; until then it
// fails with not_durable. Acquire brings it back.
func (c *Client) Offload(
	ctx context.Context,
	name string,
	args *OffloadArgs,
) (*DatabaseStatus, error) {
	if args == nil {
		args = &OffloadArgs{}
	}
	if err := validateOffloadDurability(args.Durability); err != nil {
		return nil, err
	}
	body := OffloadRequest{Name: name, Durability: args.Durability}
	var status DatabaseStatus
	if err := c.call(ctx, http.MethodPost, "/v1/databases/offload", body, &status, 0); err != nil {
		return nil, err
	}
	return &status, nil
}

// Writer returns which machine writes a database and how long its lease runs.
func (c *Client) Writer(ctx context.Context, name string) (*Writer, error) {
	var writer Writer
	if err := c.call(ctx, http.MethodGet, "/v1/databases/writer?"+url.Values{"name": {name}}.Encode(), nil, &writer, 0); err != nil {
		return nil, err
	}
	return &writer, nil
}

// BackupRules lists every backup and failover rule on this machine.
func (c *Client) BackupRules(ctx context.Context) ([]BackupRule, error) {
	var list BackupList
	if err := c.call(ctx, http.MethodGet, "/v1/backup-rules/list", nil, &list, 0); err != nil {
		return nil, err
	}
	return list.Rules, nil
}

// validateStopBackup checks what StopBackup needs before anything connects.
func validateStopBackup(name string) error {
	if name == "" {
		return invalidArgument("stopping a backup needs its database name")
	}
	return nil
}

// validateStopBackupRule checks what StopBackupRule needs before anything connects.
func validateStopBackupRule(id string) error {
	if id == "" {
		return invalidArgument("stopping a backup rule needs its ID")
	}
	return nil
}

// StopBackup stops the single-file backup for an exact remote name on this machine.
func (c *Client) StopBackup(ctx context.Context, name string) (*StopBackupResult, error) {
	if err := validateStopBackup(name); err != nil {
		return nil, err
	}
	return c.stopBackup(ctx, StopBackupRequest{Name: name})
}

// StopBackupRule stops a whole rule and its future listings using its saved ID.
func (c *Client) StopBackupRule(ctx context.Context, id string) (*StopBackupResult, error) {
	if err := validateStopBackupRule(id); err != nil {
		return nil, err
	}
	return c.stopBackup(ctx, StopBackupRequest{RuleID: id})
}

func (c *Client) stopBackup(
	ctx context.Context,
	selector StopBackupRequest,
) (*StopBackupResult, error) {
	var stopped StopBackupResult
	if err := c.call(ctx, http.MethodPost, "/v1/backup-rules/stop", selector, &stopped, 0); err != nil {
		return nil, err
	}
	return &stopped, nil
}

// Replicas lists prefix replicas and every replica on this machine.
func (c *Client) Replicas(ctx context.Context) (*ReplicaList, error) {
	var list ReplicaList
	if err := c.call(ctx, http.MethodGet, "/v1/replicas/list", nil, &list, 0); err != nil {
		return nil, err
	}
	return &list, nil
}

// ReplicateArgs configures a replica.
type ReplicateArgs struct {
	// Path is the replica file; the path this machine uses when empty.
	Path string
	// WriteProtection is lock (the daemon's default), detect, or none.
	WriteProtection WriteProtection
	// Timeout bounds downloading the first version.
	Timeout time.Duration
}

// Replicate keeps a live readonly replica of a database that streams from
// the server, and returns once its first version is local.
func (c *Client) Replicate(
	ctx context.Context,
	name string,
	args *ReplicateArgs,
) (*DatabaseStatus, error) {
	if args == nil {
		args = &ReplicateArgs{}
	}
	path, err := absolutePath(args.Path)
	if err != nil {
		return nil, err
	}
	timeout, err := millis(args.Timeout, "the replica timeout")
	if err != nil {
		return nil, err
	}
	body := ReplicaRequest{
		Name:            name,
		Path:            path,
		WriteProtection: args.WriteProtection,
		TimeoutMillis:   timeout,
	}
	var replica ReplicaResult
	if err := c.call(ctx, http.MethodPost, "/v1/replicas/start", body, &replica, args.Timeout); err != nil {
		return nil, err
	}
	if replica.Database.Name == "" {
		return nil, &Error{
			Code:    CodeInvalidResponse,
			Message: "the daemon did not return the replica",
			Status:  http.StatusOK,
		}
	}
	return &replica.Database, nil
}

// StopReplica stops a replica. The file stays, unlocked, at its last version.
func (c *Client) StopReplica(ctx context.Context, name string) error {
	return c.call(ctx, http.MethodPost, "/v1/replicas/stop", StopReplicaRequest{Name: name}, nil, 0)
}

// validateRestore checks what Restore needs before anything connects.
func validateRestore(args *RestoreArgs) error {
	if args == nil || args.Path == "" {
		return invalidArgument("restore needs a path to write to")
	}
	if args.Version != 0 && !args.Time.IsZero() {
		return invalidArgument("restore takes a version or a time, not both")
	}
	return nil
}

// ReplicatePrefixArgs configures live replicas of a namespace.
type ReplicatePrefixArgs struct {
	// Directory receives each database at its name relative to the prefix.
	Directory string
	// WriteProtection is lock (the daemon's default), detect, or none.
	WriteProtection WriteProtection
}

// replicaPrefixRequest starts or stops a prefix replica. Unlike the generated
// request types it always sends prefix, so an empty prefix reads as all databases.
type replicaPrefixRequest struct {
	Prefix          string          `json:"prefix"`
	Directory       string          `json:"directory"`
	WriteProtection WriteProtection `json:"write_protection,omitempty"`
}

// ReplicatePrefix registers live readonly replicas of current and future
// databases under prefix. An empty prefix follows all databases. Downloads
// happen in the background; returning does not mean every replica is local.
func (c *Client) ReplicatePrefix(
	ctx context.Context,
	prefix string,
	args *ReplicatePrefixArgs,
) (*PrefixReplica, error) {
	if err := validateReplicatePrefix(args); err != nil {
		return nil, err
	}
	directory, err := absolutePath(args.Directory)
	if err != nil {
		return nil, err
	}
	body := replicaPrefixRequest{
		Prefix:          prefix,
		Directory:       directory,
		WriteProtection: args.WriteProtection,
	}
	var replica ReplicaResult
	if err := c.call(ctx, http.MethodPost, "/v1/replicas/start", body, &replica, 0); err != nil {
		return nil, err
	}
	if replica.PrefixReplica.ID == "" {
		return nil, &Error{
			Code:    CodeInvalidResponse,
			Message: "the daemon did not return the prefix replica",
			Status:  http.StatusOK,
		}
	}
	return &replica.PrefixReplica, nil
}

// validateReplicatePrefix checks what ReplicatePrefix needs before anything connects.
func validateReplicatePrefix(args *ReplicatePrefixArgs) error {
	if args == nil || args.Directory == "" {
		return invalidArgument("replicating a prefix needs a directory")
	}
	return nil
}

// StopReplicaPrefixArgs names the prefix replica to stop.
type StopReplicaPrefixArgs struct {
	// Directory is the one the prefix replica was started with. It is required.
	Directory string
}

// StopReplicaPrefix stops a prefix replica and its replicas. Files stay,
// unlocked, at their last versions. Use the same prefix and directory as at start.
func (c *Client) StopReplicaPrefix(
	ctx context.Context,
	prefix string,
	args *StopReplicaPrefixArgs,
) error {
	if args == nil || args.Directory == "" {
		return invalidArgument("stopping a prefix replica needs a directory")
	}
	directory, err := absolutePath(args.Directory)
	if err != nil {
		return err
	}
	return c.call(
		ctx,
		http.MethodPost,
		"/v1/replicas/stop",
		replicaPrefixRequest{Prefix: prefix, Directory: directory},
		nil,
		0,
	)
}

// EventsArgs selects a page of events.
type EventsArgs struct {
	// After returns events with IDs greater than this one; -1 returns no
	// events and, as Next, where the log ends, to follow events from now on.
	After int64
	// Wait waits up to this long for the first new event.
	Wait time.Duration
	// Limit returns at most this many events.
	Limit int
}

// Events returns one page of events, oldest first, waiting up to Wait for
// the first new one. WatchEvents follows them.
func (c *Client) Events(ctx context.Context, args *EventsArgs) (*EventList, error) {
	if args == nil {
		args = &EventsArgs{}
	}
	waitMillis, err := millis(args.Wait, "the event wait")
	if err != nil {
		return nil, err
	}
	query := url.Values{}
	if args.After != 0 {
		query.Set("after", strconv.FormatInt(args.After, 10))
	}
	if waitMillis > 0 {
		query.Set("wait_millis", strconv.FormatInt(waitMillis, 10))
	}
	if args.Limit > 0 {
		query.Set("limit", strconv.Itoa(args.Limit))
	}
	path := "/v1/events/list"
	if len(query) > 0 {
		path += "?" + query.Encode()
	}
	var list EventList
	if err := c.call(ctx, http.MethodGet, path, nil, &list, args.Wait); err != nil {
		return nil, err
	}
	return &list, nil
}

// ShutdownArgs configures a shutdown.
type ShutdownArgs struct {
	// Durability is how far every final sync must reach.
	Durability Durability
	// Timeout bounds the shutdown.
	Timeout time.Duration
}

// Shutdown syncs every database a final time, releases every write lock,
// ends the session, and stops the daemon. A backup another machine writes
// holds no lock and stays waiting. Each database it could not sync or
// release is in Failed, with its code, and keeps any write lock it holds; the session and
// the daemon then stay. An empty Failed is the only complete shutdown.
// Close never does this.
func (c *Client) Shutdown(ctx context.Context, args *ShutdownArgs) (*ShutdownResult, error) {
	if args == nil {
		args = &ShutdownArgs{}
	}
	timeout, err := millis(args.Timeout, "the shutdown timeout")
	if err != nil {
		return nil, err
	}
	body := ShutdownRequest{Durability: ShutdownRequestDurability(args.Durability), TimeoutMillis: timeout}
	var shutdown ShutdownResult
	if err := c.call(ctx, http.MethodPost, "/v1/session/shutdown", body, &shutdown, args.Timeout); err != nil {
		return nil, err
	}
	return &shutdown, nil
}

// validateOffloadDurability refuses a durability an offload cannot wait for:
// only DurabilityServer, DurabilityCloud, or empty for the daemon's default.
func validateOffloadDurability(durability Durability) error {
	if durability != "" && !durability.Valid() {
		return invalidArgument("offload durability must be server or cloud")
	}
	return nil
}

// validateRevert checks what Revert needs before anything connects.
func validateRevert(args *RevertArgs) error {
	if args == nil || (args.Version == 0) == args.Time.IsZero() {
		return invalidArgument("revert takes a version or a time, one of them")
	}
	if args.Version < 0 {
		return invalidArgument("revert needs a positive version")
	}
	return nil
}
