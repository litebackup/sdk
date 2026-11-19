package litebackup

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
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
func newContractDaemon(t *testing.T, clientVersion *string, answer func(request receivedRequest) (int, string, []byte)) *contractDaemon {
	t.Helper()
	daemon := &contractDaemon{t: t, socket: filepath.Join(shortDirectory(t), "client.sock"), clientVersion: clientVersion, answer: answer}
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
	t.Cleanup(func() { _ = server.Close() })
	return daemon
}

// ServeHTTP records a request and answers it.
func (d *contractDaemon) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	body, err := io.ReadAll(request.Body)
	if err != nil {
		d.t.Error(err)
	}
	received := receivedRequest{Method: request.Method, Path: request.URL.Path, Query: map[string]string{}, Body: body}
	for key, values := range request.URL.Query() {
		received.Query[key] = values[0]
	}
	if request.Header.Get(sdkVersionHeader) != "go/"+SDKVersion {
		d.t.Errorf("%s sent SDK version %q", request.URL.Path, request.Header.Get(sdkVersionHeader))
	}
	if d.clientVersion != nil {
		writer.Header().Set(clientVersionHeader, *d.clientVersion)
	}
	if received.Method == http.MethodGet && received.Path == "/v1/status/show" && reflect.DeepEqual(received.Query, map[string]string{"limit": "1"}) {
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"session":"contract","server":"http://example","client_version":"1.0.0","session_id":"s","watch":"inotify","databases":[]}`))
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

// contractFixtures decodes every fixture of a kind, with {cwd} replaced.
func contractFixtures[Fixture any](t *testing.T, kind string, cwd string) map[string]Fixture {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join(contractRoot, kind, "*.json"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no %s fixtures: %v", kind, err)
	}
	fixtures := map[string]Fixture{}
	for _, path := range paths {
		content, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		// JSON-escape the directory before substituting it into JSON text.
		quoted, _ := json.Marshal(cwd)
		content = []byte(strings.ReplaceAll(string(content), "{cwd}", strings.Trim(string(quoted), `"`)))
		var fixture Fixture
		if err := json.Unmarshal(content, &fixture); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		fixtures[strings.TrimSuffix(filepath.Base(path), ".json")] = fixture
	}
	return fixtures
}

// skippedForGo skips a fixture that records why Go differs.
func skippedForGo(t *testing.T, skip map[string]string) {
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
func invokeOperation(t *testing.T, ctx context.Context, client *Client, operation string, args fixtureArgs) error {
	t.Helper()
	var err error
	switch operation {
	case "session.status":
		if args["after"] != nil || args["limit"] != nil {
			_, err = client.StatusPage(ctx, args.text(t, "after"), int(args.number("limit")))
		} else {
			_, err = client.Status(ctx)
		}
	case "session.shutdown":
		_, err = client.Shutdown(ctx, &ShutdownArgs{Wait: Durability(args.text(t, "wait")), Timeout: args.duration(t, "timeout")})
	case "databases.page":
		limit := int(args.number("limit"))
		if limit == 0 {
			limit = 100
		}
		_, err = client.DatabasesPage(ctx, args.text(t, "after"), limit)
	case "databases.get":
		_, err = client.Database(ctx, args.text(t, "name"))
	case "databases.sync":
		_, err = client.Sync(ctx, args.text(t, "name"))
	case "databases.offload":
		_, err = client.Offload(ctx, args.text(t, "name"))
	case "databases.writer":
		_, err = client.Writer(ctx, args.text(t, "name"))
	case "databases.restore":
		_, err = client.Restore(ctx, args.text(t, "name"), &RestoreArgs{To: args.text(t, "to"), Version: args.number("version"), Time: args.moment(t, "time")})
	case "databases.acquire":
		_, err = client.Acquire(ctx, args.text(t, "name"), &AcquireArgs{
			To: args.text(t, "to"), Maintenance: args.flag("maintenance"), OffloadReleased: args.text(t, "offload") == "released",
			MachineLabel: args.text(t, "machine_label"), BlockOnLost: args.text(t, "on_lost") == "block", Timeout: args.duration(t, "timeout"),
		})
	case "databases.release":
		_, err = client.Release(ctx, args.text(t, "name"), &ReleaseArgs{Wait: Durability(args.text(t, "wait")), Offload: args.flag("offload"), Timeout: args.duration(t, "timeout")})
	case "backups.start":
		_, err = client.StartBackup(ctx, args.text(t, "source"), &StartBackupArgs{Name: args.text(t, "name"), Prefix: args.text(t, "prefix"), MaximumLocalDatabases: args.number("maximum_local_databases")})
	case "backups.stop":
		_, err = client.StopBackup(ctx, args.text(t, "name"))
	case "rules.list":
		_, err = client.Backups(ctx)
	case "rules.stop":
		_, err = client.StopBackupRule(ctx, args.text(t, "id"))
	case "replicas.start":
		_, err = client.Replicate(ctx, args.text(t, "name"), &ReplicateArgs{To: args.text(t, "to"), WriteProtection: WriteProtection(args.text(t, "write_protection")), Timeout: args.duration(t, "timeout")})
	case "replicas.start_prefix":
		_, err = client.ReplicatePrefix(ctx, args.text(t, "prefix"), &ReplicatePrefixArgs{To: args.text(t, "to"), WriteProtection: WriteProtection(args.text(t, "write_protection"))})
	case "replicas.list":
		_, err = client.Replicas(ctx)
	case "replicas.stop":
		err = client.StopReplica(ctx, args.text(t, "name"))
	case "replicas.stop_prefix":
		err = client.StopReplicaPrefix(ctx, args.text(t, "prefix"), args.text(t, "to"))
	case "conflicts.list":
		_, err = client.Conflicts(ctx)
	case "conflicts.delete":
		err = client.DeleteConflict(ctx, args.text(t, "id"))
	case "events.page":
		_, err = client.Events(ctx, &EventsArgs{After: args.number("after"), Wait: args.duration(t, "wait"), Limit: int(args.number("limit"))})
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

// checkRequest compares a received request with a fixture's.
func checkRequest(t *testing.T, received receivedRequest, expected contractRequest) {
	t.Helper()
	if received.Method != expected.Method || received.Path != expected.Path {
		t.Fatalf("sent %s %s, want %s %s", received.Method, received.Path, expected.Method, expected.Path)
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
			version := "1.0.0"
			daemon := newContractDaemon(t, &version, func(receivedRequest) (int, string, []byte) {
				return http.StatusOK, "application/json", fixture.Response
			})
			client := daemon.connect(t)
			err := invokeOperation(t, t.Context(), client, fixture.Operation, fixture.Args)
			sent := daemon.requests()
			if fixture.Error != nil {
				if !IsCode(err, fixture.Error.Code) || len(sent) != 0 {
					t.Fatalf("got %v after %d requests, want %s before any", err, len(sent), fixture.Error.Code)
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
			version := "1.0.0"
			daemon := newContractDaemon(t, &version, func(receivedRequest) (int, string, []byte) {
				return fixture.Response.Status, fixture.Response.ContentType, []byte(fixture.Response.Body)
			})
			_, err := daemon.connect(t).Backups(t.Context())
			var answered *Error
			if !errors.As(err, &answered) {
				t.Fatalf("got %v", err)
			}
			if answered.Code != fixture.Expect.Code || answered.Message != fixture.Expect.Message || answered.Status != fixture.Expect.Status {
				t.Fatalf("got %q %q %d, want %q %q %d", answered.Code, answered.Message, answered.Status, fixture.Expect.Code, fixture.Expect.Message, fixture.Expect.Status)
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
			if !IsCode(err, fixture.Expect.Code) || !strings.Contains(err.Error(), MinimumDaemonVersion) || !strings.Contains(err.Error(), "litebackup drain") {
				t.Fatalf("got %v, want %s naming %s and how to restart", err, fixture.Expect.Code, MinimumDaemonVersion)
			}
		})
	}
}

// exchangeDaemon answers requests from a fixture's exchanges, in order.
func exchangeDaemon(t *testing.T, exchanges []contractExchange) *contractDaemon {
	t.Helper()
	var mutex sync.Mutex
	next := 0
	version := "1.0.0"
	return newContractDaemon(t, &version, func(received receivedRequest) (int, string, []byte) {
		mutex.Lock()
		defer mutex.Unlock()
		if next >= len(exchanges) {
			t.Errorf("unexpected request %s %s %v", received.Method, received.Path, received.Query)
			return http.StatusInternalServerError, "application/json", []byte(`{"code":"unexpected","message":"no more exchanges"}`)
		}
		exchange := exchanges[next]
		next++
		if received.Method != exchange.Request.Method || received.Path != exchange.Request.Path || !reflect.DeepEqual(received.Query, exchange.Request.Query) {
			t.Errorf("request %d: sent %s %s %v, want %s %s %v", next, received.Method, received.Path, received.Query, exchange.Request.Method, exchange.Request.Path, exchange.Request.Query)
		}
		return exchange.Response.Status, "application/json", exchange.Response.Body
	})
}

func TestContractPaginationAndEvents(t *testing.T) {
	type pageFixture struct {
		Operation string             `json:"operation"`
		Args      fixtureArgs        `json:"args"`
		ItemKey   string             `json:"item_key"`
		Take      int                `json:"take"`
		Exchanges []contractExchange `json:"exchanges"`
		Skip      map[string]string  `json:"skip"`
		Expect    struct {
			Items []any `json:"items"`
			Error *struct {
				Code string `json:"code"`
			} `json:"error"`
		} `json:"expect"`
	}
	for _, kind := range []string{"pagination", "events"} {
		for name, fixture := range contractFixtures[pageFixture](t, kind, "/") {
			t.Run(kind+"/"+name, func(t *testing.T) {
				skippedForGo(t, fixture.Skip)
				daemon := exchangeDaemon(t, fixture.Exchanges)
				client := daemon.connect(t)
				items := []any{}
				var lastErr error
				collect := func(key any, err error) bool {
					if err != nil {
						lastErr = err
						return false
					}
					items = append(items, key)
					return fixture.Take == 0 || len(items) < fixture.Take
				}
				ctx := t.Context()
				switch fixture.Operation {
				case "databases.iterate":
					for database, err := range client.AllDatabases(ctx) {
						if !collect(database.Name, err) {
							break
						}
					}
				case "databases.versions":
					for version, err := range client.AllVersions(ctx, fixture.Args.text(t, "name"), &VersionsArgs{After: fixture.Args.number("after"), Limit: int(fixture.Args.number("limit"))}) {
						if !collect(float64(version.Version), err) {
							break
						}
					}
				case "events.watch":
					for event, err := range client.WatchEvents(ctx, &WatchEventsArgs{After: fixture.Args.number("after"), Wait: fixture.Args.duration(t, "wait")}) {
						if !collect(float64(event.ID), err) {
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

// recordingAdapter is a ScopeAdapter that records steps and fails on demand.
type recordingAdapter struct {
	steps     *scopeSteps
	failOpen  error
	failClose error
	openPath  string
}

func (a *recordingAdapter) Open(_ context.Context, path string) (string, error) {
	a.steps.add("open")
	a.openPath = path
	return "connection", a.failOpen
}

func (a *recordingAdapter) Close(context.Context, string) error {
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
		Scope struct {
			Name           string `json:"name"`
			To             string `json:"to"`
			Durability     string `json:"durability"`
			Offload        bool   `json:"offload"`
			CleanupTimeout string `json:"cleanup_timeout"`
			Create         *bool  `json:"create"`
		} `json:"scope"`
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
			CreatedFile string          `json:"created_file"`
			MissingFile string          `json:"missing_file"`
		} `json:"expect"`
	}
	for name := range contractFixtures[lifecycleFixture](t, "lifecycle", "/") {
		t.Run(name, func(t *testing.T) {
			cwd := contractDirectory(t)
			t.Chdir(cwd)
			fixture := contractFixtures[lifecycleFixture](t, "lifecycle", cwd)[name]
			skippedForGo(t, fixture.Skip)
			failing := map[string]bool{}
			for _, step := range fixture.Fail {
				failing[step] = true
			}
			steps := &scopeSteps{}
			version := "1.0.0"
			daemon := newContractDaemon(t, &version, func(received receivedRequest) (int, string, []byte) {
				step := strings.TrimPrefix(received.Path, "/v1/databases/")
				steps.add(step)
				if failing[step] {
					return http.StatusServiceUnavailable, "application/json", []byte(`{"code":"unavailable","message":"` + step + ` failed"}`)
				}
				if step == "acquire" {
					return http.StatusOK, "application/json", fixture.AcquireResponse
				}
				return http.StatusOK, "application/json", []byte(`{"database":{"name":"agents/42.sqlite","path":"/x","role":"origin","state":"synced"},"checkpointed":false,"vacuumed":false,"offloaded":false}`)
			})
			client := daemon.connect(t)
			openErr := errors.New("open failed")
			closeErr := errors.New("close failed")
			adapter := &recordingAdapter{steps: steps}
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
			if fixture.Scope.CleanupTimeout != "" {
				cleanupTimeout, _ = time.ParseDuration(fixture.Scope.CleanupTimeout)
			}
			err := Managed(ctx, client, fixture.Scope.Name, &ManagedArgs[string]{
				To: fixture.Scope.To, Durability: Durability(fixture.Scope.Durability), Offload: fixture.Scope.Offload,
				CleanupTimeout: cleanupTimeout, Adapter: adapter, RequireExisting: fixture.Scope.Create != nil && !*fixture.Scope.Create,
			}, func(ctx context.Context, connection string) error {
				steps.add("work")
				switch {
				case fixture.Cancel == "work":
					cancel()
					workErr = ctx.Err()
				case failing["work"]:
					workErr = errors.New("work failed")
				}
				return workErr
			})
			expectedSteps := fixture.Expect.Steps
			if expectedSteps == nil {
				expectedSteps = []string{}
			}
			if !reflect.DeepEqual(steps.all(), expectedSteps) {
				t.Fatalf("steps %v, want %v", steps.all(), expectedSteps)
			}
			if fixture.Expect.CreatedFile != "" {
				if _, statErr := os.Stat(fixture.Expect.CreatedFile); statErr != nil {
					t.Fatalf("the scope did not create %s: %v", fixture.Expect.CreatedFile, statErr)
				}
			}
			if fixture.Expect.MissingFile != "" {
				if _, statErr := os.Stat(fixture.Expect.MissingFile); statErr == nil {
					t.Fatalf("the scope created %s", fixture.Expect.MissingFile)
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
				}
				if !sameJSON(t, received.Body, expected) {
					t.Fatalf("%s sent %s, want %s", received.Path, received.Body, expected)
				}
			}
			raised := []error{}
			if err != nil {
				raised = append(raised, err)
				var joined interface{ Unwrap() []error }
				if errors.As(err, &joined) {
					raised = joined.Unwrap()
				}
			}
			if len(raised) != len(fixture.Expect.Raised) {
				t.Fatalf("raised %v, want %d errors", err, len(fixture.Expect.Raised))
			}
			for index, expected := range fixture.Expect.Raised {
				actual := raised[index]
				switch {
				case expected.Source == "work":
					if actual != workErr {
						t.Fatalf("error %d is %v, want the work's error %v", index, actual, workErr)
					}
				case expected.Source == "open":
					if actual != openErr {
						t.Fatalf("error %d is %v, want the open error", index, actual)
					}
				case expected.Code != "":
					var answered *Error
					if !errors.As(actual, &answered) || answered.Code != expected.Code {
						t.Fatalf("error %d is %v, want %s", index, actual, expected.Code)
					}
					if expected.Details != nil && !reflect.DeepEqual(answered.Details, expected.Details) {
						t.Fatalf("details %v, want %v", answered.Details, expected.Details)
					}
					if expected.Cause == "close" && !errors.Is(actual, closeErr) || expected.Cause == "release" && !IsCode(answered.Err, "unavailable") {
						t.Fatalf("error %d's cause is %v, want the %s error", index, answered.Err, expected.Cause)
					}
				}
			}
		})
	}
}
