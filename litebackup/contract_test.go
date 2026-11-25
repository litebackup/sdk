package litebackup

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// The contract tests run the language-neutral fixtures in
// ../testdata/contract, which the TypeScript and Python SDKs run too.

// contractExchange is one request a fixture expects and the answer it gets.
type contractExchange struct {
	Request  contractRequest `json:"request"`
	Response struct {
		Status      int             `json:"status"`
		ContentType string          `json:"content_type"`
		Body        json.RawMessage `json:"body"`
	} `json:"response"`
}

// contractRequest is a request as fixtures write it.
type contractRequest struct {
	Method string            `json:"method"`
	Path   string            `json:"path"`
	Query  map[string]string `json:"query"`
	Body   json.RawMessage   `json:"body"`
}

// receivedRequest is a request the contract daemon received.
type receivedRequest struct {
	Method string
	Path   string
	Query  map[string]string
	Body   []byte
}

// contractDaemon answers the connect probe itself and hands every other
// request to answer.
type contractDaemon struct {
	t             *testing.T
	socket        string
	clientVersion *string

	mutex    sync.Mutex
	received []receivedRequest
	answer   func(request receivedRequest) (status int, contentType string, body []byte)
}

// newContractDaemon serves on a fresh unix socket until the test ends.
func newContractDaemon(
	t *testing.T,
	clientVersion *string,
	answer func(request receivedRequest) (int, string, []byte),
) *contractDaemon {
	t.Helper()
	daemon := &contractDaemon{
		t:             t,
		socket:        filepath.Join(shortDirectory(t), "client.sock"),
		clientVersion: clientVersion,
		answer:        answer,
	}
	listener, err := net.Listen("unix", daemon.socket)
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: daemon}
	go func() {
		if serveErr := server.Serve(listener); serveErr != nil &&
			!errors.Is(serveErr, http.ErrServerClosed) {
			t.Error(serveErr)
		}
	}()
	t.Cleanup(func() { _ = server.Close() })
	return daemon
}

// ServeHTTP records a request and answers it.
func (d *contractDaemon) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	body, err := io.ReadAll(request.Body)
	if err != nil {
		d.t.Error(err)
	}
	received := receivedRequest{
		Method: request.Method,
		Path:   request.URL.Path,
		Query:  map[string]string{},
		Body:   body,
	}
	for key, values := range request.URL.Query() {
		received.Query[key] = values[0]
	}
	if request.Header.Get(sdkVersionHeader) != "go/"+SDKVersion {
		d.t.Errorf("%s sent SDK version %q", request.URL.Path, request.Header.Get(sdkVersionHeader))
	}
	if d.clientVersion != nil {
		writer.Header().Set(clientVersionHeader, *d.clientVersion)
	}
	if received.Method == http.MethodGet && received.Path == "/v1/status/show" &&
		reflect.DeepEqual(received.Query, map[string]string{"limit": "1"}) {
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write(
			[]byte(
				`{"session":"contract","server":"http://example","client_version":"1.5.0","session_id":"s","watch":"inotify","databases":[]}`,
			),
		)
		return
	}
	d.mutex.Lock()
	d.received = append(d.received, received)
	d.mutex.Unlock()
	status, contentType, answer := d.answer(received)
	writer.Header().Set("Content-Type", contentType)
	writer.WriteHeader(status)
	_, _ = writer.Write(answer)
}

// requests returns what the daemon received besides the connect probe.
func (d *contractDaemon) requests() []receivedRequest {
	d.mutex.Lock()
	defer d.mutex.Unlock()
	return append([]receivedRequest(nil), d.received...)
}

// connect connects a client and closes it when the test ends.
func (d *contractDaemon) connect(t *testing.T) *Client {
	t.Helper()
	client, err := Connect(t.Context(), &ConnectArgs{SocketPath: d.socket})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return client
}

// contractRoot is the fixture directory, absolute so tests may change directory.
var contractRoot = func() string {
	directory, err := os.Getwd()
	if err != nil {
		panic(err)
	}
	return filepath.Join(directory, "..", "testdata", "contract")
}()

// contractFixtures decodes every fixture in one fixture directory, with {cwd}
// and "{umask}" replaced.
func contractFixtures[Fixture any](
	t *testing.T,
	fixtureDirectory string,
	cwd string,
) map[string]Fixture {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join(contractRoot, fixtureDirectory, "*.json"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no %s fixtures: %v", fixtureDirectory, err)
	}
	fixtures := map[string]Fixture{}
	for _, path := range paths {
		content, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		// JSON-escape the directory before substituting it into JSON text.
		quoted, _ := json.Marshal(cwd)
		content = []byte(
			strings.ReplaceAll(string(content), "{cwd}", strings.Trim(string(quoted), `"`)),
		)
		content = []byte(
			strings.ReplaceAll(string(content), `"{umask}"`, strconv.Itoa(fixtureUmask())),
		)
		var fixture Fixture
		if err := json.Unmarshal(content, &fixture); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		fixtures[strings.TrimSuffix(filepath.Base(path), ".json")] = fixture
	}
	return fixtures
}

// fixtureUmask reads this process's umask by setting it and putting it
// back, apart from how the SDK reads it.
func fixtureUmask() int {
	previous := syscall.Umask(0)
	syscall.Umask(previous)
	return previous
}

// skipInGo skips a fixture that says why Go differs.
func skipInGo(t *testing.T, skip map[string]string) {
	t.Helper()
	if reason := skip["go"]; reason != "" {
		t.Skip(reason)
	}
}

// contractDirectory is a working directory for one fixture, its symlinks resolved.
func contractDirectory(t *testing.T) string {
	t.Helper()
	directory, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return directory
}

// fixtureArgs are an operation's language-neutral arguments.
type fixtureArgs map[string]any

func (a fixtureArgs) text(t *testing.T, key string) string {
	t.Helper()
	switch value := a[key].(type) {
	case nil:
		return ""
	case string:
		return value
	case map[string]any:
		path, _ := value["$path"].(string)
		return path
	default:
		t.Fatalf("argument %s is %T", key, value)
		return ""
	}
}

func (a fixtureArgs) number(key string) int64 {
	value, _ := a[key].(float64)
	return int64(value)
}

// umask is a umask argument, nil when absent; a negative one wraps past
// 0777, as the other SDKs refuse it.
func (a fixtureArgs) umask(key string) *fs.FileMode {
	umaskNumber, ok := a[key].(float64)
	if !ok {
		return nil
	}
	mask := fs.FileMode(int64(umaskNumber))
	return &mask
}

func (a fixtureArgs) flag(key string) bool {
	value, _ := a[key].(bool)
	return value
}

func (a fixtureArgs) duration(t *testing.T, key string) time.Duration {
	t.Helper()
	text := a.text(t, key)
	if text == "" {
		return 0
	}
	duration, err := time.ParseDuration(text)
	if err != nil {
		t.Fatal(err)
	}
	return duration
}

func (a fixtureArgs) moment(t *testing.T, key string) time.Time {
	t.Helper()
	text := a.text(t, key)
	if text == "" {
		return time.Time{}
	}
	moment, err := time.Parse(time.RFC3339Nano, text)
	if err != nil {
		t.Fatal(err)
	}
	return moment
}

// invokeOperation runs a fixture operation with the Go API.
func invokeOperation(
	t *testing.T,
	ctx context.Context,
	client *Client,
	operation string,
	args fixtureArgs,
) error {
	t.Helper()
	var err error
	switch operation {
	case "session.status":
		if args["after"] != nil || args["limit"] != nil {
			_, err = client.StatusPage(
				ctx,
				&StatusPageArgs{After: args.text(t, "after"), Limit: int(args.number("limit"))},
			)
		} else {
			_, err = client.Status(ctx)
		}
	case "session.shutdown":
		_, err = client.Shutdown(
			ctx,
			&ShutdownArgs{
				Durability: Durability(args.text(t, "durability")),
				Timeout:    args.duration(t, "timeout"),
			},
		)
	case "databases.page":
		_, err = client.DatabasesPage(
			ctx,
			&DatabasesPageArgs{After: args.text(t, "after"), Limit: int(args.number("limit"))},
		)
	case "databases.get":
		_, err = client.Database(ctx, args.text(t, "name"))
	case "databases.sync":
		_, err = client.Sync(ctx, args.text(t, "name"))
	case "databases.offload":
		_, err = client.Offload(
			ctx,
			args.text(t, "name"),
			&OffloadArgs{Durability: Durability(args.text(t, "durability"))},
		)
	case "databases.writer":
		_, err = client.Writer(ctx, args.text(t, "name"))
	case "databases.restore":
		_, err = client.Restore(
			ctx,
			args.text(t, "name"),
			&RestoreArgs{
				Path:    args.text(t, "path"),
				Version: args.number("version"),
				Time:    args.moment(t, "time"),
				Umask:   args.umask("umask"),
			},
		)
	case "databases.revert":
		_, err = client.Revert(
			ctx,
			args.text(t, "name"),
			&RevertArgs{Version: args.number("version"), Time: args.moment(t, "time")},
		)
	case "databases.acquire":
		_, err = client.Acquire(ctx, args.text(t, "name"), &AcquireArgs{
			Path: args.text(
				t,
				"path",
			), Maintenance: args.flag("maintenance"), OffloadOnRelease: args.flag("offload_on_release"),
			OffloadDurability: Durability(args.text(t, "offload_durability")),
			MachineLabel: args.text(
				t,
				"machine_label",
			), Timeout: args.duration(t, "timeout"),
			LockTimeout:  args.duration(t, "lock_timeout"),
			Umask:        args.umask("umask"),
			DiscardLocal: args.flag("discard_local"),
		})
	case "databases.release":
		_, err = client.Release(
			ctx,
			args.text(t, "name"),
			&ReleaseArgs{
				Durability: Durability(args.text(t, "durability")),
				Offload:    args.flag("offload"),
				Timeout:    args.duration(t, "timeout"),
			},
		)
	case "backups.start":
		_, err = client.StartBackup(
			ctx,
			args.text(t, "source"),
			&StartBackupArgs{
				Name:                   args.text(t, "name"),
				Prefix:                 args.text(t, "prefix"),
				MaximumLocalDatabases:  args.number("maximum_local_databases"),
				OffloadDurability:      Durability(args.text(t, "offload_durability")),
				OffloadRollbackJournal: args.flag("offload_rollback_journal"),
			},
		)
	case "backups.stop":
		_, err = client.StopBackup(ctx, args.text(t, "name"))
	case "rules.list":
		_, err = client.BackupRules(ctx)
	case "rules.stop":
		_, err = client.StopBackupRule(ctx, args.text(t, "id"))
	case "replicas.start":
		_, err = client.Replicate(
			ctx,
			args.text(t, "name"),
			&ReplicateArgs{
				Path:            args.text(t, "path"),
				WriteProtection: WriteProtection(args.text(t, "write_protection")),
				Timeout:         args.duration(t, "timeout"),
			},
		)
	case "replicas.start_prefix":
		_, err = client.ReplicatePrefix(
			ctx,
			args.text(t, "prefix"),
			&ReplicatePrefixArgs{
				Directory:       args.text(t, "directory"),
				WriteProtection: WriteProtection(args.text(t, "write_protection")),
			},
		)
	case "replicas.list":
		_, err = client.Replicas(ctx)
	case "replicas.stop":
		err = client.StopReplica(ctx, args.text(t, "name"))
	case "replicas.stop_prefix":
		err = client.StopReplicaPrefix(
			ctx,
			args.text(t, "prefix"),
			&StopReplicaPrefixArgs{Directory: args.text(t, "directory")},
		)
	case "events.page":
		_, err = client.Events(
			ctx,
			&EventsArgs{
				After: args.number("after"),
				Wait:  args.duration(t, "wait"),
				Limit: int(args.number("limit")),
			},
		)
	default:
		t.Fatalf("no Go mapping for operation %s", operation)
	}
	return err
}

// sameJSON reports whether two JSON documents decode to equal values.
func sameJSON(t *testing.T, left []byte, right []byte) bool {
	t.Helper()
	var leftValue, rightValue any
	if err := json.Unmarshal(left, &leftValue); err != nil {
		t.Fatalf("decoding %s: %v", left, err)
	}
	if err := json.Unmarshal(right, &rightValue); err != nil {
		t.Fatalf("decoding %s: %v", right, err)
	}
	return reflect.DeepEqual(leftValue, rightValue)
}

// withSpentCleanup reads a release timeout_millis up to a second short of
// the fixture's full cleanup timeout as the fixture's value, since the
// release gets what closing left.
func withSpentCleanup(t *testing.T, body []byte, expected []byte) []byte {
	t.Helper()
	var sent, want map[string]any
	if json.Unmarshal(body, &sent) != nil || json.Unmarshal(expected, &want) != nil {
		return body
	}
	sentMillis, sentOK := sent["timeout_millis"].(float64)
	wantMillis, wantOK := want["timeout_millis"].(float64)
	if !sentOK || !wantOK || wantMillis-sentMillis < 0 || wantMillis-sentMillis > 1000 {
		return body
	}
	sent["timeout_millis"] = wantMillis
	adjusted, err := json.Marshal(sent)
	if err != nil {
		t.Fatal(err)
	}
	return adjusted
}

// checkRequest compares a received request with a fixture's.
func checkRequest(t *testing.T, received receivedRequest, expected contractRequest) {
	t.Helper()
	if received.Method != expected.Method || received.Path != expected.Path {
		t.Fatalf(
			"sent %s %s, want %s %s",
			received.Method,
			received.Path,
			expected.Method,
			expected.Path,
		)
	}
	query := expected.Query
	if query == nil {
		query = map[string]string{}
	}
	if !reflect.DeepEqual(received.Query, query) {
		t.Fatalf("sent query %v, want %v", received.Query, query)
	}
	if expected.Body == nil {
		if len(received.Body) > 0 {
			t.Fatalf("sent body %s, want none", received.Body)
		}
		return
	}
	if !sameJSON(t, received.Body, expected.Body) {
		t.Fatalf("sent body %s, want %s", received.Body, expected.Body)
	}
}

func TestContractRequests(t *testing.T) {
	type requestFixture struct {
		Operation string           `json:"operation"`
		Args      fixtureArgs      `json:"args"`
		Request   *contractRequest `json:"request"`
		Response  json.RawMessage  `json:"response"`
		Error     *struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	for name := range contractFixtures[requestFixture](t, "requests", "/") {
		t.Run(name, func(t *testing.T) {
			cwd := contractDirectory(t)
			t.Chdir(cwd)
			fixture := contractFixtures[requestFixture](t, "requests", cwd)[name]
			version := "1.5.0"
			daemon := newContractDaemon(t, &version, func(receivedRequest) (int, string, []byte) {
				return http.StatusOK, "application/json", fixture.Response
			})
			client := daemon.connect(t)
			err := invokeOperation(t, t.Context(), client, fixture.Operation, fixture.Args)
			sent := daemon.requests()
			if fixture.Error != nil {
				if !IsCode(err, fixture.Error.Code) || len(sent) != 0 {
					t.Fatalf(
						"got %v after %d requests, want %s before any",
						err,
						len(sent),
						fixture.Error.Code,
					)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if len(sent) != 1 {
				t.Fatalf("sent %d requests, want 1", len(sent))
			}
			checkRequest(t, sent[0], *fixture.Request)
		})
	}
}

func TestContractErrors(t *testing.T) {
	type errorFixture struct {
		Response struct {
			Status      int    `json:"status"`
			ContentType string `json:"content_type"`
			Body        string `json:"body"`
		} `json:"response"`
		Expect struct {
			Code    string         `json:"code"`
			Message string         `json:"message"`
			Status  int            `json:"status"`
			Details map[string]any `json:"details"`
		} `json:"expect"`
	}
	for name, fixture := range contractFixtures[errorFixture](t, "errors", "/") {
		t.Run(name, func(t *testing.T) {
			version := "1.5.0"
			daemon := newContractDaemon(t, &version, func(receivedRequest) (int, string, []byte) {
				return fixture.Response.Status, fixture.Response.ContentType, []byte(
					fixture.Response.Body,
				)
			})
			_, err := daemon.connect(t).BackupRules(t.Context())
			var answered *Error
			if !errors.As(err, &answered) {
				t.Fatalf("got %v", err)
			}
			if answered.Code != fixture.Expect.Code || answered.Message != fixture.Expect.Message ||
				answered.Status != fixture.Expect.Status {
				t.Fatalf(
					"got %q %q %d, want %q %q %d",
					answered.Code,
					answered.Message,
					answered.Status,
					fixture.Expect.Code,
					fixture.Expect.Message,
					fixture.Expect.Status,
				)
			}
			if !reflect.DeepEqual(answered.Details, fixture.Expect.Details) {
				t.Fatalf("details %v, want %v", answered.Details, fixture.Expect.Details)
			}
		})
	}
}

func TestContractVersion(t *testing.T) {
	type versionFixture struct {
		ClientVersion *string `json:"client_version"`
		Accepted      bool    `json:"accepted"`
		Expect        struct {
			Code string `json:"code"`
		} `json:"expect"`
	}
	for name, fixture := range contractFixtures[versionFixture](t, "version", "/") {
		t.Run(name, func(t *testing.T) {
			daemon := newContractDaemon(t, fixture.ClientVersion, nil)
			client, err := Connect(t.Context(), &ConnectArgs{SocketPath: daemon.socket})
			if fixture.Accepted {
				if err != nil {
					t.Fatal(err)
				}
				_ = client.Close()
				return
			}
			if !IsCode(err, fixture.Expect.Code) ||
				!strings.Contains(err.Error(), MinimumDaemonVersion) ||
				!strings.Contains(err.Error(), "litebackup drain") {
				t.Fatalf(
					"got %v, want %s naming %s and how to restart",
					err,
					fixture.Expect.Code,
					MinimumDaemonVersion,
				)
			}
		})
	}
}

// exchangeDaemon answers requests from a fixture's exchanges, in order.
func exchangeDaemon(t *testing.T, exchanges []contractExchange) *contractDaemon {
	t.Helper()
	var mutex sync.Mutex
	next := 0
	version := "1.5.0"
	return newContractDaemon(t, &version, func(received receivedRequest) (int, string, []byte) {
		mutex.Lock()
		defer mutex.Unlock()
		if next >= len(exchanges) {
			t.Errorf("unexpected request %s %s %v", received.Method, received.Path, received.Query)
			return http.StatusInternalServerError, "application/json", []byte(
				`{"code":"unexpected","message":"no more exchanges"}`,
			)
		}
		exchange := exchanges[next]
		next++
		if received.Method != exchange.Request.Method || received.Path != exchange.Request.Path ||
			!reflect.DeepEqual(received.Query, exchange.Request.Query) {
			t.Errorf(
				"request %d: sent %s %s %v, want %s %s %v",
				next,
				received.Method,
				received.Path,
				received.Query,
				exchange.Request.Method,
				exchange.Request.Path,
				exchange.Request.Query,
			)
		}
		return exchange.Response.Status, "application/json", exchange.Response.Body
	})
}

func TestContractPaginationAndEvents(t *testing.T) {
	type pageFixture struct {
		Operation string             `json:"operation"`
		Args      fixtureArgs        `json:"args"`
		ItemKey   string             `json:"item_key"`
		ItemLimit int                `json:"item_limit"`
		Exchanges []contractExchange `json:"exchanges"`
		Skip      map[string]string  `json:"skip"`
		Expect    struct {
			Items []any `json:"items"`
			Error *struct {
				Code string `json:"code"`
			} `json:"error"`
		} `json:"expect"`
	}
	for _, fixtureDirectory := range []string{"pagination", "events"} {
		for name, fixture := range contractFixtures[pageFixture](t, fixtureDirectory, "/") {
			t.Run(fixtureDirectory+"/"+name, func(t *testing.T) {
				skipInGo(t, fixture.Skip)
				daemon := exchangeDaemon(t, fixture.Exchanges)
				client := daemon.connect(t)
				items := []any{}
				var lastErr error
				appendItem := func(key any, err error) bool {
					if err != nil {
						lastErr = err
						return false
					}
					items = append(items, key)
					return fixture.ItemLimit == 0 || len(items) < fixture.ItemLimit
				}
				ctx := t.Context()
				switch fixture.Operation {
				case "databases.all":
					for database, err := range client.AllDatabases(ctx) {
						if !appendItem(database.Name, err) {
							break
						}
					}
				case "databases.versions":
					for version, err := range client.AllVersions(ctx, fixture.Args.text(t, "name"), &VersionsArgs{After: fixture.Args.number("after"), Limit: int(fixture.Args.number("limit"))}) {
						if !appendItem(versionItem(t, version, fixture.ItemKey), err) {
							break
						}
					}
				case "events.watch":
					for event, err := range client.WatchEvents(ctx, &WatchEventsArgs{After: fixture.Args.number("after"), Wait: fixture.Args.duration(t, "wait")}) {
						if !appendItem(float64(event.ID), err) {
							break
						}
					}
				default:
					t.Fatalf("no Go mapping for %s", fixture.Operation)
				}
				expected := fixture.Expect.Items
				if expected == nil {
					expected = []any{}
				}
				if !reflect.DeepEqual(items, expected) {
					t.Fatalf("yielded %v, want %v", items, expected)
				}
				if fixture.Expect.Error != nil {
					if !IsCode(lastErr, fixture.Expect.Error.Code) {
						t.Fatalf("ended with %v, want %s", lastErr, fixture.Expect.Error.Code)
					}
				} else if lastErr != nil {
					t.Fatal(lastErr)
				}
				if sent := len(daemon.requests()); sent != len(fixture.Exchanges) {
					t.Fatalf("sent %d requests, want %d", sent, len(fixture.Exchanges))
				}
			})
		}
	}
}

// versionItem is the field of a listed version a pagination fixture's
// item_key names, as JSON decodes it.
func versionItem(t *testing.T, version RestorableVersion, itemKey string) any {
	t.Helper()
	switch itemKey {
	case "version":
		return float64(version.Version)
	case "archived":
		if version.Archived.IsZero() {
			return nil
		}
		return version.Archived.UTC().Format(time.RFC3339)
	}
	t.Fatalf("no Go mapping for the version field %s", itemKey)
	return nil
}

// scopeSteps records the steps of a managed scope in order.
type scopeSteps struct {
	mutex sync.Mutex
	steps []string
}

func (s *scopeSteps) add(step string) {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	s.steps = append(s.steps, step)
}

func (s *scopeSteps) all() []string {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	return append([]string{}, s.steps...)
}

// stepLoggingAdapter is an Adapter that logs its steps and fails on demand.
type stepLoggingAdapter struct {
	steps     *scopeSteps
	failOpen  error
	failClose error
	openPath  string
}

func (a *stepLoggingAdapter) Open(_ context.Context, path string) (string, error) {
	a.steps.add("open")
	a.openPath = path
	return "connection", a.failOpen
}

func (a *stepLoggingAdapter) Close(context.Context, string) error {
	a.steps.add("close")
	return a.failClose
}

func TestContractLifecycle(t *testing.T) {
	type raisedError struct {
		Source  string         `json:"source"`
		Code    string         `json:"code"`
		Step    string         `json:"step"`
		Cause   string         `json:"cause"`
		Details map[string]any `json:"details"`
	}
	type lifecycleFixture struct {
		Lease struct {
			Name            string `json:"name"`
			Path            string `json:"path"`
			Durability      string `json:"durability"`
			Offload         bool   `json:"offload"`
			CleanupTimeout  string `json:"cleanup_timeout"`
			RequireExisting bool   `json:"require_existing"`
		} `json:"lease"`
		Fail            []string          `json:"fail"`
		Cancel          string            `json:"cancel"`
		AcquireResponse json.RawMessage   `json:"acquire_response"`
		Skip            map[string]string `json:"skip"`
		Expect          struct {
			Steps       []string        `json:"steps"`
			AcquireBody json.RawMessage `json:"acquire_body"`
			OpenPath    string          `json:"open_path"`
			ReleaseBody json.RawMessage `json:"release_body"`
			Raised      []raisedError   `json:"raised"`
			CloseRaised []raisedError   `json:"close_raised"`
			CreatedFile string          `json:"created_file"`
			MissingFile string          `json:"missing_file"`
		} `json:"expect"`
	}
	for name := range contractFixtures[lifecycleFixture](t, "lifecycle", "/") {
		t.Run(name, func(t *testing.T) {
			cwd := contractDirectory(t)
			t.Chdir(cwd)
			fixture := contractFixtures[lifecycleFixture](t, "lifecycle", cwd)[name]
			skipInGo(t, fixture.Skip)
			failing := map[string]bool{}
			for _, step := range fixture.Fail {
				failing[step] = true
			}
			steps := &scopeSteps{}
			version := "1.5.0"
			daemon := newContractDaemon(
				t,
				&version,
				func(received receivedRequest) (int, string, []byte) {
					if !strings.HasPrefix(received.Path, "/v1/databases/") {
						// The pool's watch for lock events: nothing happens.
						time.Sleep(50 * time.Millisecond)
						return http.StatusOK, "application/json", []byte(`{"events":[],"next":0}`)
					}
					step := strings.TrimPrefix(received.Path, "/v1/databases/")
					steps.add(step)
					if failing[step] {
						return http.StatusServiceUnavailable, "application/json", []byte(
							`{"code":"unavailable","message":"` + step + ` failed"}`,
						)
					}
					switch step {
					case "acquire":
						return http.StatusOK, "application/json", fixture.AcquireResponse
					case "sync":
						return http.StatusOK, "application/json", []byte(
							`{"name":"agents/42.sqlite","path":"/x","role":"origin","state":"synced","acquired":true}`,
						)
					}
					return http.StatusOK, "application/json", []byte(
						`{"database":{"name":"agents/42.sqlite","path":"/x","role":"origin","state":"synced"},"checkpointed":false,"vacuumed":false}`,
					)
				},
			)
			client := daemon.connect(t)
			openErr := errors.New("open failed")
			closeErr := errors.New("close failed")
			adapter := &stepLoggingAdapter{steps: steps}
			if failing["open"] {
				adapter.failOpen = openErr
			}
			if failing["close"] {
				adapter.failClose = closeErr
			}
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			if fixture.Cancel == "before" {
				cancel()
			}
			var workErr error
			cleanupTimeout := time.Duration(0)
			if fixture.Lease.CleanupTimeout != "" {
				cleanupTimeout, _ = time.ParseDuration(fixture.Lease.CleanupTimeout)
			}
			pool, err := NewPool(client, &PoolArgs[string]{
				Adapter:        adapter,
				CleanupTimeout: cleanupTimeout,
			})
			var closeRaisedErr error
			if err == nil {
				err = pool.Lease(ctx, fixture.Lease.Name, &LeaseArgs{
					Path:            fixture.Lease.Path,
					Durability:      Durability(fixture.Lease.Durability),
					Offload:         fixture.Lease.Offload,
					RequireExisting: fixture.Lease.RequireExisting,
				}, func(ctx context.Context, connection string) error {
					steps.add("work")
					switch {
					case fixture.Cancel == "work":
						cancel()
						workErr = ctx.Err()
					case fixture.Cancel == "work-returns":
						cancel()
					case failing["work"]:
						workErr = errors.New("work failed")
					}
					return workErr
				})
				closeRaisedErr = pool.Close(t.Context())
			}
			expectedSteps := fixture.Expect.Steps
			if expectedSteps == nil {
				expectedSteps = []string{}
			}
			if !reflect.DeepEqual(steps.all(), expectedSteps) {
				t.Fatalf("steps %v, want %v", steps.all(), expectedSteps)
			}
			if fixture.Expect.CreatedFile != "" {
				if _, statErr := os.Stat(fixture.Expect.CreatedFile); statErr != nil {
					t.Fatalf("the lease did not create %s: %v", fixture.Expect.CreatedFile, statErr)
				}
			}
			if fixture.Expect.MissingFile != "" {
				if _, statErr := os.Stat(fixture.Expect.MissingFile); statErr == nil {
					t.Fatalf("the lease created %s", fixture.Expect.MissingFile)
				}
			}
			if fixture.Expect.OpenPath != "" && adapter.openPath != fixture.Expect.OpenPath {
				t.Fatalf("opened %s, want %s", adapter.openPath, fixture.Expect.OpenPath)
			}
			for _, received := range daemon.requests() {
				var expected json.RawMessage
				switch received.Path {
				case "/v1/databases/acquire":
					expected = fixture.Expect.AcquireBody
				case "/v1/databases/release":
					expected = fixture.Expect.ReleaseBody
				default:
					continue
				}
				if !sameJSON(t, withSpentCleanup(t, received.Body, expected), expected) {
					t.Fatalf("%s sent %s, want %s", received.Path, received.Body, expected)
				}
			}
			requireRaised := func(call string, err error, expectedErrors []raisedError) {
				t.Helper()
				raised := []error{}
				if err != nil {
					raised = append(raised, err)
					var joined interface{ Unwrap() []error }
					if errors.As(err, &joined) {
						raised = joined.Unwrap()
					}
				}
				if len(raised) != len(expectedErrors) {
					t.Fatalf("%s raised %v, want %d errors", call, err, len(expectedErrors))
				}
				for index, expected := range expectedErrors {
					actual := raised[index]
					switch {
					case expected.Source == "work":
						if actual != workErr {
							t.Fatalf("%s error %d is %v, want the work's error %v", call, index, actual, workErr)
						}
					case expected.Source == "open":
						if actual != openErr {
							t.Fatalf("%s error %d is %v, want the open error", call, index, actual)
						}
					case expected.Code != "":
						var answered *Error
						if !errors.As(actual, &answered) || answered.Code != expected.Code {
							t.Fatalf("%s error %d is %v, want %s", call, index, actual, expected.Code)
						}
						if expected.Details != nil &&
							!reflect.DeepEqual(answered.Details, expected.Details) {
							t.Fatalf("details %v, want %v", answered.Details, expected.Details)
						}
						if expected.Step != "" {
							var cleanup *LeaseCleanupError
							if !errors.As(actual, &cleanup) || string(cleanup.Step) != expected.Step {
								t.Fatalf("%s error %d is %v, want a %s LeaseCleanupError", call, index, actual, expected.Step)
							}
						}
						if expected.Cause == "close" && !errors.Is(actual, closeErr) ||
							expected.Cause == "release" && !IsCode(answered.Err, "unavailable") {
							t.Fatalf(
								"%s error %d's cause is %v, want the %s error",
								call,
								index,
								answered.Err,
								expected.Cause,
							)
						}
					}
				}
			}
			requireRaised("the lease", err, fixture.Expect.Raised)
			requireRaised("closing the pool", closeRaisedErr, fixture.Expect.CloseRaised)
		})
	}
}

// TestContractCredentials checks that a LITEBACKUP_API_KEY value is told
// apart by prefix, and that a refusal never repeats the credential.
func TestContractCredentials(t *testing.T) {
	type credentialFixture struct {
		APIKey string          `json:"api_key"`
		Type   *credentialType `json:"type"`
		Expect *struct {
			Code string `json:"code"`
		} `json:"expect"`
	}
	for name, fixture := range contractFixtures[credentialFixture](t, "credentials", "/") {
		t.Run(name, func(t *testing.T) {
			found, err := checkCredential(fixture.APIKey)
			if fixture.Expect != nil {
				if !IsCode(err, fixture.Expect.Code) {
					t.Fatalf("got %v, want %s", err, fixture.Expect.Code)
				}
				secret, _, _ := strings.Cut(strings.TrimSpace(fixture.APIKey), "@")
				if secret != "" && strings.Contains(err.Error(), secret) {
					t.Fatalf("the refusal %q repeats the credential", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			expected := credentialNone
			if fixture.Type != nil {
				expected = *fixture.Type
			}
			if found != expected {
				t.Fatalf("got %q, want %q", found, expected)
			}
		})
	}
}

// TestContractStatusTimes decodes the databases-get fixture's answer: the
// server time under synced_server.time and the archive time under
// synced_cloud.archived.
func TestContractStatusTimes(t *testing.T) {
	type answerFixture struct {
		Response json.RawMessage `json:"response"`
	}
	fixture := contractFixtures[answerFixture](t, "requests", "/")["databases-get"]
	var status DatabaseStatus
	if err := json.Unmarshal(fixture.Response, &status); err != nil {
		t.Fatal(err)
	}
	serverTime := time.Date(2026, 11, 15, 9, 31, 0, 0, time.UTC)
	archived := time.Date(2026, 11, 15, 9, 30, 0, 0, time.UTC)
	if !status.SyncedServer.Time.Equal(serverTime) || !status.SyncedCloud.Archived.Equal(archived) {
		t.Fatalf("synced_server %+v, synced_cloud %+v", status.SyncedServer, status.SyncedCloud)
	}
}

// TestContractStateTimes decodes each state's timestamp: the databases-offload
// fixture's offloaded database, the databases-sync one's retrying origin, and
// the databases-get-pending one's pending origin.
func TestContractStateTimes(t *testing.T) {
	type answerFixture struct {
		Response json.RawMessage `json:"response"`
	}
	fixtures := contractFixtures[answerFixture](t, "requests", "/")
	fixtureTime := func(minute, second int) time.Time {
		return time.Date(2026, 11, 15, 9, minute, second, 0, time.UTC)
	}
	var offloaded DatabaseStatus
	if err := json.Unmarshal(fixtures["databases-offload"].Response, &offloaded); err != nil {
		t.Fatal(err)
	}
	if !offloaded.Offloaded.Equal(fixtureTime(32, 0)) || !offloaded.Synced.Equal(fixtureTime(31, 0)) ||
		!offloaded.Released.Equal(fixtureTime(31, 30)) {
		t.Fatalf("offloaded %v, synced %v, released %v", offloaded.Offloaded, offloaded.Synced, offloaded.Released)
	}
	var retrying DatabaseStatus
	if err := json.Unmarshal(fixtures["databases-sync"].Response, &retrying); err != nil {
		t.Fatal(err)
	}
	if retrying.State != "retrying" || !retrying.Retrying.Equal(fixtureTime(25, 0)) ||
		!retrying.Failed.Equal(fixtureTime(29, 0)) || !retrying.Syncing.Equal(fixtureTime(20, 0)) ||
		retrying.Error != "unavailable: the server did not answer" {
		t.Fatalf("retrying status %+v", retrying)
	}
	var pending DatabaseStatus
	if err := json.Unmarshal(fixtures["databases-get-pending"].Response, &pending); err != nil {
		t.Fatal(err)
	}
	if pending.State != "pending" || !pending.Pending.Equal(fixtureTime(33, 0)) || !pending.Syncing.IsZero() {
		t.Fatalf("pending status %+v", pending)
	}
}

// TestContractReleaseOffloaded decodes the release-all fixture's answer, a
// release that offloaded, with when it removed the file, and the
// release-no-offload one, which kept it and has none.
func TestContractReleaseOffloaded(t *testing.T) {
	type answerFixture struct {
		Response json.RawMessage `json:"response"`
	}
	fixtures := contractFixtures[answerFixture](t, "requests", "/")
	var offloaded ReleaseResult
	if err := json.Unmarshal(fixtures["release-all"].Response, &offloaded); err != nil {
		t.Fatal(err)
	}
	if want := time.Date(2026, 11, 15, 9, 32, 0, 0, time.UTC); !offloaded.Offloaded.Equal(want) {
		t.Fatalf("offloaded %v, want %v", offloaded.Offloaded, want)
	}
	var noOffload ReleaseResult
	if err := json.Unmarshal(fixtures["release-no-offload"].Response, &noOffload); err != nil {
		t.Fatal(err)
	}
	if !noOffload.Offloaded.IsZero() {
		t.Fatalf("offloaded %v after a release that kept the file", noOffload.Offloaded)
	}
}

// TestContractShutdownFailed decodes the shutdown-all fixture's answer, a
// database the drain could not drain with its code and message, and the
// shutdown-default one, an empty list.
func TestContractShutdownFailed(t *testing.T) {
	type answerFixture struct {
		Response json.RawMessage `json:"response"`
	}
	fixture := contractFixtures[answerFixture](t, "requests", "/")["shutdown-all"]
	var shutdown ShutdownResult
	if err := json.Unmarshal(fixture.Response, &shutdown); err != nil {
		t.Fatal(err)
	}
	var defaultShutdown ShutdownResult
	if err := json.Unmarshal(
		contractFixtures[answerFixture](t, "requests", "/")["shutdown-default"].Response,
		&defaultShutdown,
	); err != nil {
		t.Fatal(err)
	}
	failed := []DrainFailure{{
		Name:    "orders.sqlite",
		Code:    CodeLockUnavailable,
		Message: "another machine writes the database (worker-3)",
	}}
	if shutdown.ReleasedCount != 2 || !reflect.DeepEqual(shutdown.Failures, failed) {
		t.Fatalf("released %d, failed %+v, want 2 and %+v", shutdown.ReleasedCount, shutdown.Failures, failed)
	}
	if defaultShutdown.Failures == nil || len(defaultShutdown.Failures) != 0 {
		t.Fatalf("failed %#v after a drain that released everything", defaultShutdown.Failures)
	}
}

// TestContractStatusOffloadDurability decodes the acquire-all fixture's answer, an
// acquired database with offload_durability cloud, and the databases-get one, a
// database that is not acquired and has none.
func TestContractStatusOffloadDurability(t *testing.T) {
	type answerFixture struct {
		Response json.RawMessage `json:"response"`
	}
	fixtures := contractFixtures[answerFixture](t, "requests", "/")
	var acquired AcquireResult
	if err := json.Unmarshal(fixtures["acquire-all"].Response, &acquired); err != nil {
		t.Fatal(err)
	}
	if acquired.Database.OffloadDurability != DurabilityCloud {
		t.Fatalf(
			"an acquired database's offload_durability %q",
			acquired.Database.OffloadDurability,
		)
	}
	var status DatabaseStatus
	if err := json.Unmarshal(fixtures["databases-get"].Response, &status); err != nil {
		t.Fatal(err)
	}
	if status.OffloadDurability != "" {
		t.Fatalf(
			"a database that is not acquired has offload_durability %q",
			status.OffloadDurability,
		)
	}
}

// missingOverrideFixture is binary/override-missing.json: an explicit binary
// path that names no file.
type missingOverrideFixture struct {
	MissingFile     string   `json:"missing_file"`
	Overrides       []string `json:"overrides"`
	Code            string   `json:"code"`
	MessageContains string   `json:"message_contains"`
	DeniedFile      string   `json:"denied_file"`
	Interpreter     string   `json:"interpreter_script"`
	InterpreterCode string   `json:"interpreter_code"`
	InterpreterText string   `json:"interpreter_message_contains"`
	NotDirectory    string   `json:"not_directory_file"`
}

// TestContractMissingOverride runs binary/override-missing.json: the option
// and LITEBACKUP_PATH naming no file are binary_not_found and name the path,
// even with a litebackup on PATH to fall back to.
func TestContractMissingOverride(t *testing.T) {
	content, err := os.ReadFile(filepath.Join(contractRoot, "binary", "override-missing.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture missingOverrideFixture
	if err := json.Unmarshal(content, &fixture); err != nil {
		t.Fatal(err)
	}
	onPath := t.TempDir()
	onPathBinary := filepath.Join(onPath, "litebackup")
	if err := os.WriteFile(onPathBinary, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", onPath)
	for _, override := range fixture.Overrides {
		t.Run(override, func(t *testing.T) {
			missing := filepath.Join(t.TempDir(), filepath.FromSlash(fixture.MissingFile))
			binaryPathArgument := ""
			switch override {
			case "option":
				t.Setenv(BinaryPathEnvironment, "")
				binaryPathArgument = missing
			case "environment":
				t.Setenv(BinaryPathEnvironment, missing)
			default:
				t.Fatalf("unknown override %q", override)
			}
			binary, err := resolveBinary(binaryPathArgument)
			var sdkError *Error
			if !errors.As(err, &sdkError) || sdkError.Code != fixture.Code ||
				!strings.Contains(sdkError.Message, fixture.MessageContains) ||
				!strings.Contains(sdkError.Message, missing) {
				t.Fatalf("a missing %s override resolved to %q, %v", override, binary, err)
			}
		})
	}
}

// TestContractOverrideThatExists runs the rest of
// binary/override-missing.json: a file behind a directory this user may not
// enter resolves, and a foreground backup of a script whose interpreter is
// missing is launch_failed, not binary_not_found.
func TestContractOverrideThatExists(t *testing.T) {
	content, err := os.ReadFile(filepath.Join(contractRoot, "binary", "override-missing.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture missingOverrideFixture
	if err := json.Unmarshal(content, &fixture); err != nil {
		t.Fatal(err)
	}
	if os.Geteuid() != 0 {
		denied := filepath.Join(t.TempDir(), filepath.FromSlash(fixture.DeniedFile))
		if err := os.MkdirAll(filepath.Dir(denied), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(denied, []byte("#!/bin/sh\n"), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(filepath.Dir(denied), 0); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = os.Chmod(filepath.Dir(denied), 0o755) })
		binary, err := resolveBinary(denied)
		if err != nil || binary != denied {
			t.Fatalf("a file behind a denied directory resolved to %q, %v", binary, err)
		}
	}
	client := foregroundClient(t, "exit 0")
	if err := os.WriteFile(client.binaryPath, []byte(fixture.Interpreter), 0o755); err != nil {
		t.Fatal(err)
	}
	err = client.Backup(t.Context(), "a.db", nil)
	if !IsCode(err, fixture.InterpreterCode) ||
		!strings.Contains(err.Error(), fixture.InterpreterText) {
		t.Fatalf("a script whose interpreter is missing: %v", err)
	}
}

// TestContractOverrideUnderFile runs the not_directory_file case of
// binary/override-missing.json: an override under a regular file names no
// file, so a foreground backup is binary_not_found. A start that fails with
// ENOTDIR or ENOENT is binary_not_found only when the file is gone.
func TestContractOverrideUnderFile(t *testing.T) {
	content, err := os.ReadFile(filepath.Join(contractRoot, "binary", "override-missing.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture missingOverrideFixture
	if err := json.Unmarshal(content, &fixture); err != nil {
		t.Fatal(err)
	}
	directory := t.TempDir()
	underFile := filepath.Join(directory, filepath.FromSlash(fixture.NotDirectory))
	plainFile := []byte("a file, not a directory")
	if err := os.WriteFile(filepath.Dir(underFile), plainFile, 0o644); err != nil {
		t.Fatal(err)
	}
	client := foregroundClient(t, "exit 0")
	existing := client.binaryPath
	client.binaryPath = underFile
	err = client.Backup(t.Context(), "a.db", nil)
	if !IsCode(err, fixture.Code) {
		t.Fatalf("a foreground backup of an override under a file: %v", err)
	}
	notDirectory := &fs.PathError{Op: "fork/exec", Path: underFile, Err: syscall.ENOTDIR}
	if !binaryMissing(underFile, notDirectory) {
		t.Fatal("ENOTDIR for a path under a file is not a missing binary")
	}
	if binaryMissing(existing, notDirectory) {
		t.Fatal("ENOTDIR for a file that exists is a missing binary")
	}
}
