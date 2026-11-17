// Package litebackup backs up, replicates, and restores SQLite databases with
// Litebackup from a Go program. It talks to the litebackup daemon of a session
// over its unix socket, as the litebackup command does, and starts the
// daemon when it is not running, joining with LITEBACKUP_TOKEN the first time.
//
// The everyday calls are package functions over a shared client:
//
//	backup, err := litebackup.Backup(ctx, "/var/lib/app/main.db", litebackup.BackupOptions{})
//	err = backup.Wait(ctx, litebackup.DurabilityServer)
//
// Connect returns a Client for everything else.
package litebackup

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
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
const SDKVersion = "0.3.31"

const (
	sdkVersionHeader    = "X-Litebackup-SDK-Version"
	clientVersionHeader = "X-Litebackup-Client-Version"
	// responseLimit bounds one decoded response.
	responseLimit = 64 << 20
)

// Error is an error the daemon answered with.
type Error struct {
	// Code is such as name_in_use, exists, in_use, not_found, still_open, timeout, or sdk_unsupported.
	Code    string
	Message string
	// Status is the HTTP status of the answer.
	Status int
}

// Error returns the code and message.
func (e *Error) Error() string {
	return e.Code + ": " + e.Message
}

// IsCode reports whether err is an *Error with the given code.
func IsCode(err error, code string) bool {
	var answered *Error
	return errors.As(err, &answered) && answered.Code == code
}

// Options selects the session a Client talks to.
type Options struct {
	// Token is the join token, TOKEN@HOST, the daemon joins with on its first
	// start. It overrides LITEBACKUP_TOKEN. A daemon that already joined
	// keeps its join.
	Token string
	// Session names the session; LITEBACKUP_SESSION, then "default", when empty.
	Session string
	// SocketPath connects to this socket and never starts a daemon.
	SocketPath string
}

// Client talks to the daemon of one session.
type Client struct {
	session    string
	socketPath string
	http       *http.Client

	mutex         sync.Mutex
	clientVersion string
	failovers     map[*FailoverRun]struct{}
	closed        bool
}

// Connect connects to a session's daemon, starting it like the litebackup
// command does when nothing listens on its socket.
func Connect(ctx context.Context, options Options) (*Client, error) {
	session := options.Session
	socket := options.SocketPath
	if socket == "" {
		resolved, path, err := SessionSocket(options.Session)
		if err != nil {
			return nil, err
		}
		session, socket = resolved, path
		if err := launchDaemon(ctx, session, socket, options.Token); err != nil {
			return nil, err
		}
	}
	client := &Client{
		session:    session,
		socketPath: socket,
		http: &http.Client{Transport: &http.Transport{
			DialContext: func(ctx context.Context, _ string, _ string) (net.Conn, error) {
				var dialer net.Dialer
				return dialer.DialContext(ctx, "unix", socket)
			},
			MaxIdleConns: 4,
		}},
	}
	if _, err := client.Status(ctx); err != nil {
		client.http.CloseIdleConnections()
		return nil, err
	}
	return client, nil
}

// Session is the session this client talks to.
func (c *Client) Session() string { return c.session }

// SocketPath is the daemon's socket.
func (c *Client) SocketPath() string { return c.socketPath }

// ClientVersion is the daemon's version, known after the first call.
func (c *Client) ClientVersion() string {
	c.mutex.Lock()
	defer c.mutex.Unlock()
	return c.clientVersion
}

// Close stops following the client's failovers and closes its connections.
// The daemon keeps running, and so do the failovers.
func (c *Client) Close() error {
	c.mutex.Lock()
	failovers := c.failovers
	c.failovers = nil
	c.closed = true
	c.mutex.Unlock()
	for failover := range failovers {
		failover.end()
	}
	c.http.CloseIdleConnections()
	return nil
}

// addFailover records a failover so Close stops following it; it reports
// false once the client is closed.
func (c *Client) addFailover(failover *FailoverRun) bool {
	c.mutex.Lock()
	defer c.mutex.Unlock()
	if c.closed {
		return false
	}
	if c.failovers == nil {
		c.failovers = map[*FailoverRun]struct{}{}
	}
	c.failovers[failover] = struct{}{}
	return true
}

// removeFailover forgets a stopped failover.
func (c *Client) removeFailover(failover *FailoverRun) {
	c.mutex.Lock()
	defer c.mutex.Unlock()
	delete(c.failovers, failover)
}

// call sends one request and decodes its JSON answer into result.
func (c *Client) call(ctx context.Context, method string, path string, body any, result any) error {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, "http://litebackup"+path, reader)
	if err != nil {
		return err
	}
	request.Header.Set(sdkVersionHeader, "go/"+SDKVersion)
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := c.http.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	version := response.Header.Get(clientVersionHeader)
	if version != "" {
		c.mutex.Lock()
		c.clientVersion = version
		c.mutex.Unlock()
	}
	content, err := io.ReadAll(io.LimitReader(response.Body, responseLimit))
	if err != nil {
		return err
	}
	if response.StatusCode != http.StatusOK {
		return responseError(response.StatusCode, content)
	}
	if result == nil {
		return nil
	}
	if err := json.Unmarshal(content, result); err != nil {
		return fmt.Errorf("decoding %s: %w", path, err)
	}
	return nil
}

// responseError turns a non-200 answer into an *Error.
func responseError(status int, content []byte) *Error {
	answered := &Error{Code: "http_" + strconv.Itoa(status), Message: strings.TrimSpace(string(content)), Status: status}
	var decoded struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	}
	if json.Unmarshal(content, &decoded) == nil {
		if decoded.Code != "" {
			answered.Code = decoded.Code
		}
		if decoded.Message != "" {
			answered.Message = decoded.Message
		}
	}
	return answered
}

// absolutePath resolves a relative path against the working directory; the
// daemon needs absolute paths.
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

// millis renders a duration as the API's milliseconds, or 0 for none.
func millis(duration time.Duration) int64 {
	return duration.Milliseconds()
}

// Status returns the session, its server, and every database on this machine.
func (c *Client) Status(ctx context.Context) (*Status, error) {
	var status Status
	if err := c.call(ctx, http.MethodGet, "/v1/status/show", nil, &status); err != nil {
		return nil, err
	}
	return &status, nil
}

// Databases returns every database on this machine with its role and state.
func (c *Client) Databases(ctx context.Context) ([]DatabaseStatus, error) {
	var list struct {
		Databases []DatabaseStatus `json:"databases"`
	}
	if err := c.call(ctx, http.MethodGet, "/v1/databases/list", nil, &list); err != nil {
		return nil, err
	}
	return list.Databases, nil
}

// Database returns one database's role, state, and synced versions.
func (c *Client) Database(ctx context.Context, name string) (*DatabaseStatus, error) {
	var status DatabaseStatus
	if err := c.call(ctx, http.MethodGet, "/v1/databases/status?"+url.Values{"name": {name}}.Encode(), nil, &status); err != nil {
		return nil, err
	}
	return &status, nil
}

// Sync syncs a database now and returns once the server acknowledged it.
func (c *Client) Sync(ctx context.Context, name string) (*DatabaseStatus, error) {
	var status DatabaseStatus
	if err := c.call(ctx, http.MethodPost, "/v1/databases/sync", map[string]string{"name": name}, &status); err != nil {
		return nil, err
	}
	return &status, nil
}

// VersionsOptions pages through versions.
type VersionsOptions struct {
	// After skips versions up to and including this one.
	After int64
	// Limit returns at most this many versions; every version when 0.
	Limit int
}

// Versions lists the versions of a database the server can restore, oldest first.
func (c *Client) Versions(ctx context.Context, name string, options VersionsOptions) ([]Version, error) {
	versions := []Version{}
	after := options.After
	for {
		query := url.Values{"name": {name}}
		if after > 0 {
			query.Set("after", strconv.FormatInt(after, 10))
		}
		if options.Limit > 0 {
			query.Set("limit", strconv.Itoa(options.Limit-len(versions)))
		}
		var page struct {
			Versions []Version `json:"versions"`
			Next     int64     `json:"next"`
		}
		if err := c.call(ctx, http.MethodGet, "/v1/databases/versions?"+query.Encode(), nil, &page); err != nil {
			return nil, err
		}
		versions = append(versions, page.Versions...)
		if page.Next == 0 || page.Next <= after || len(page.Versions) == 0 || options.Limit > 0 && len(versions) >= options.Limit {
			return versions, nil
		}
		after = page.Next
	}
}

// RestoreOptions picks the file and the version a restore writes.
type RestoreOptions struct {
	// To is the new file; relative paths resolve against the working
	// directory. Its directory must exist, and an existing file is never
	// overwritten (exists).
	To string
	// Version restores this version; the head when neither Version nor Time is set.
	Version int64
	// Time restores the newest version at or before this time.
	Time time.Time
}

// Restore writes a version of a database to a new file and checks it.
func (c *Client) Restore(ctx context.Context, name string, options RestoreOptions) (*RestoreResult, error) {
	if options.To == "" {
		return nil, errors.New("restore needs a path to write to")
	}
	if options.Version != 0 && !options.Time.IsZero() {
		return nil, errors.New("restore takes a version or a time, not both")
	}
	path, err := absolutePath(options.To)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"name": name, "path": path}
	if options.Version != 0 {
		body["version"] = options.Version
	}
	if !options.Time.IsZero() {
		body["time"] = options.Time.UTC().Format(time.RFC3339Nano)
	}
	var result RestoreResult
	if err := c.call(ctx, http.MethodPost, "/v1/databases/restore", body, &result); err != nil {
		return nil, err
	}
	return &result, nil
}

// AcquireOptions configures an acquired database.
type AcquireOptions struct {
	// To is the local file; the path this machine already uses when empty,
	// which brings an offloaded database back.
	To string
	// Maintenance checkpoints on release and VACUUMs past the freelist threshold.
	Maintenance bool
	// OffloadReleased lets Litebackup remove the released copy once the bucket holds it.
	OffloadReleased bool
	// MachineLabel is what other machines see as the writer.
	MachineLabel string
	// BlockOnLost blocks local writes while the write lock is lost, instead
	// of keeping the copy as conflicted.
	BlockOnLost bool
	// Timeout bounds downloading the head.
	Timeout time.Duration
}

// Acquire takes the write lock on a database and makes the local file the
// server's head, for worker pools that move databases between machines.
func (c *Client) Acquire(ctx context.Context, name string, options AcquireOptions) (*AcquireResult, error) {
	path, err := absolutePath(options.To)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"name": name}
	if path != "" {
		body["path"] = path
	}
	if options.Maintenance {
		body["maintenance"] = true
	}
	if options.OffloadReleased {
		body["offload"] = "released"
	}
	if options.MachineLabel != "" {
		body["machine_label"] = options.MachineLabel
	}
	if options.BlockOnLost {
		body["on_lost"] = "block"
	}
	if options.Timeout > 0 {
		body["timeout_millis"] = millis(options.Timeout)
	}
	var result AcquireResult
	if err := c.call(ctx, http.MethodPost, "/v1/databases/acquire", body, &result); err != nil {
		return nil, err
	}
	return &result, nil
}

// ReleaseOptions configures a release.
type ReleaseOptions struct {
	// Wait is server or cloud; the daemon's default when empty.
	Wait Durability
	// Offload removes the local file once the bucket holds the released version.
	Offload bool
	// Timeout bounds waiting for the database to close and for Wait.
	Timeout time.Duration
}

// Release syncs an acquired database a final time, runs its maintenance, and
// releases its write lock.
func (c *Client) Release(ctx context.Context, name string, options ReleaseOptions) (*ReleaseResult, error) {
	body := map[string]any{"name": name}
	if options.Wait != "" && options.Wait != DurabilityNone {
		body["wait"] = options.Wait
	}
	if options.Offload {
		body["offload"] = true
	}
	if options.Timeout > 0 {
		body["timeout_millis"] = millis(options.Timeout)
	}
	var result ReleaseResult
	if err := c.call(ctx, http.MethodPost, "/v1/databases/release", body, &result); err != nil {
		return nil, err
	}
	return &result, nil
}

// Offload removes the local file of a released database once the bucket
// holds its last version. Acquire brings it back.
func (c *Client) Offload(ctx context.Context, name string) (*DatabaseStatus, error) {
	var status DatabaseStatus
	if err := c.call(ctx, http.MethodPost, "/v1/databases/offload", map[string]string{"name": name}, &status); err != nil {
		return nil, err
	}
	return &status, nil
}

// Writer returns which machine writes a database and how long its lease runs.
func (c *Client) Writer(ctx context.Context, name string) (*Writer, error) {
	var writer Writer
	if err := c.call(ctx, http.MethodGet, "/v1/databases/writer?"+url.Values{"name": {name}}.Encode(), nil, &writer); err != nil {
		return nil, err
	}
	return &writer, nil
}

// Backups lists every backup and failover on this machine.
func (c *Client) Backups(ctx context.Context) ([]BackupInfo, error) {
	var list struct {
		Backups []BackupInfo `json:"backups"`
	}
	if err := c.call(ctx, http.MethodGet, "/v1/backups/list", nil, &list); err != nil {
		return nil, err
	}
	return list.Backups, nil
}

// StopBackupOptions names the backup or failover to stop by one of its ID,
// a database name it syncs, or its path.
type StopBackupOptions struct {
	ID   string
	Name string
	// Path is the path or glob the backup was started with, or a file it syncs.
	Path string
}

// StopBackup stops a backup or failover and releases its write locks. The
// files stay.
func (c *Client) StopBackup(ctx context.Context, options StopBackupOptions) error {
	body := map[string]string{}
	switch {
	case options.ID != "":
		body["id"] = options.ID
	case options.Name != "":
		body["name"] = options.Name
	case options.Path != "":
		path, err := absolutePath(options.Path)
		if err != nil {
			return err
		}
		body["path"] = path
	default:
		return errors.New("stopping a backup needs its ID, a name, or a path")
	}
	return c.call(ctx, http.MethodPost, "/v1/backups/stop", body, nil)
}

// Replicas lists prefix replicas and every replica on this machine.
func (c *Client) Replicas(ctx context.Context) (*ReplicaList, error) {
	var list ReplicaList
	if err := c.call(ctx, http.MethodGet, "/v1/replicas/list", nil, &list); err != nil {
		return nil, err
	}
	return &list, nil
}

// ReplicateOptions configures a replica.
type ReplicateOptions struct {
	// To is the replica file; the path this machine uses when empty.
	To string
	// WriteProtection is lock (the daemon's default), detect, or none.
	WriteProtection WriteProtection
	// Timeout bounds downloading the first version.
	Timeout time.Duration
}

// Replicate keeps a live readonly replica of a database that streams from
// the server, and returns once its first version is local.
func (c *Client) Replicate(ctx context.Context, name string, options ReplicateOptions) (*DatabaseStatus, error) {
	path, err := absolutePath(options.To)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"name": name}
	if path != "" {
		body["to"] = path
	}
	if options.WriteProtection != "" {
		body["write_protection"] = options.WriteProtection
	}
	if options.Timeout > 0 {
		body["timeout_millis"] = millis(options.Timeout)
	}
	var result struct {
		Database *DatabaseStatus `json:"database"`
	}
	if err := c.call(ctx, http.MethodPost, "/v1/replicas/start", body, &result); err != nil {
		return nil, err
	}
	if result.Database == nil {
		return nil, &Error{Code: "invalid_response", Message: "the daemon did not return the replica", Status: http.StatusOK}
	}
	return result.Database, nil
}

// StopReplica stops a replica. The file stays, unlocked, at its last version.
func (c *Client) StopReplica(ctx context.Context, name string) error {
	return c.call(ctx, http.MethodPost, "/v1/replicas/stop", map[string]string{"name": name}, nil)
}

// ReplicatePrefixOptions configures live replicas of a namespace.
type ReplicatePrefixOptions struct {
	// To is the directory receiving each database at its name relative to the prefix.
	To string
	// WriteProtection is lock (the daemon's default), detect, or none.
	WriteProtection WriteProtection
}

// ReplicatePrefix registers live readonly replicas of current and future
// databases under prefix. An empty prefix follows all databases. Downloads
// happen in the background; returning does not mean every replica is local.
func (c *Client) ReplicatePrefix(ctx context.Context, prefix string, options ReplicatePrefixOptions) (*PrefixReplica, error) {
	if options.To == "" {
		return nil, &Error{Code: "invalid_argument", Message: "replicating a prefix needs a directory"}
	}
	path, err := absolutePath(options.To)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"prefix": prefix, "to": path}
	if options.WriteProtection != "" {
		body["write_protection"] = options.WriteProtection
	}
	var result struct {
		Prefix *PrefixReplica `json:"prefix"`
	}
	if err := c.call(ctx, http.MethodPost, "/v1/replicas/start", body, &result); err != nil {
		return nil, err
	}
	if result.Prefix == nil {
		return nil, &Error{Code: "invalid_response", Message: "the daemon did not return the prefix replica", Status: http.StatusOK}
	}
	return result.Prefix, nil
}

// StopReplicaPrefix stops a prefix replica and its replicas. Files stay,
// unlocked, at their last versions. Use the same prefix and directory as at start.
func (c *Client) StopReplicaPrefix(ctx context.Context, prefix string, to string) error {
	if to == "" {
		return &Error{Code: "invalid_argument", Message: "stopping a prefix replica needs a directory"}
	}
	path, err := absolutePath(to)
	if err != nil {
		return err
	}
	return c.call(ctx, http.MethodPost, "/v1/replicas/stop", map[string]string{"prefix": prefix, "to": path}, nil)
}

// Conflicts lists conflict copies: local copies saved because they diverged from the server.
func (c *Client) Conflicts(ctx context.Context) ([]Conflict, error) {
	var list struct {
		Conflicts []Conflict `json:"conflicts"`
	}
	if err := c.call(ctx, http.MethodGet, "/v1/conflicts/list", nil, &list); err != nil {
		return nil, err
	}
	return list.Conflicts, nil
}

// DeleteConflict deletes a conflict copy and its -wal and -shm files.
func (c *Client) DeleteConflict(ctx context.Context, id string) error {
	return c.call(ctx, http.MethodPost, "/v1/conflicts/delete", map[string]string{"id": id}, nil)
}

// EventsOptions selects a page of events.
type EventsOptions struct {
	// After returns events with IDs greater than this one; -1 returns no
	// events and, as Next, where the log ends, to follow events from now on.
	After int64
	// Wait waits up to this long for the first new event.
	Wait time.Duration
	// Limit returns at most this many events.
	Limit int
}

// Events returns one page of events, oldest first, waiting up to Wait for
// the first new one.
func (c *Client) Events(ctx context.Context, options EventsOptions) (*EventList, error) {
	query := url.Values{}
	if options.After != 0 {
		query.Set("after", strconv.FormatInt(options.After, 10))
	}
	if options.Wait > 0 {
		query.Set("wait_millis", strconv.FormatInt(millis(options.Wait), 10))
	}
	if options.Limit > 0 {
		query.Set("limit", strconv.Itoa(options.Limit))
	}
	path := "/v1/events/list"
	if len(query) > 0 {
		path += "?" + query.Encode()
	}
	var list EventList
	if err := c.call(ctx, http.MethodGet, path, nil, &list); err != nil {
		return nil, err
	}
	return &list, nil
}

// ShutdownOptions configures a shutdown.
type ShutdownOptions struct {
	// Wait is how far every final sync must reach.
	Wait Durability
	// Timeout bounds the shutdown.
	Timeout time.Duration
}

// Shutdown syncs every database a final time, releases every write lock,
// ends the session, and stops the daemon.
func (c *Client) Shutdown(ctx context.Context, options ShutdownOptions) (*ShutdownResult, error) {
	body := map[string]any{}
	if options.Wait != "" {
		body["wait"] = options.Wait
	}
	if options.Timeout > 0 {
		body["timeout_millis"] = millis(options.Timeout)
	}
	var result ShutdownResult
	if err := c.call(ctx, http.MethodPost, "/v1/session/shutdown", body, &result); err != nil {
		return nil, err
	}
	return &result, nil
}
