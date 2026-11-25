package litebackup

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// TestConnectStopsLaunchingWhenCancelled checks that Connect returns
// cancelled soon after its context ends while litebackup launch stalls, even
// when a process the launch started keeps its output open.
func TestConnectStopsLaunchingWhenCancelled(t *testing.T) {
	directory := t.TempDir()
	binary := filepath.Join(directory, "litebackup")
	// The shell's child sleep keeps the output pipe open after the shell is killed.
	if err := os.WriteFile(binary, []byte("#!/bin/sh\nsleep 30\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("XDG_RUNTIME_DIR", t.TempDir())
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	started := time.Now()
	_, err := Connect(ctx, &ConnectArgs{Session: "stalled", BinaryPath: binary})
	if !IsCode(err, CodeCancelled) {
		t.Fatalf("Connect returned %v, want cancelled", err)
	}
	if elapsed := time.Since(started); elapsed > 5*time.Second {
		t.Fatalf("Connect waited %s after its context ended", elapsed)
	}
}

// credentialStandIn writes a litebackup stand-in that appends its command
// and the LITEBACKUP_API_KEY it was given ("unset" when it had none) to a
// file, and returns both paths. Its launch exits without a daemon.
func credentialStandIn(t *testing.T) (string, string) {
	t.Helper()
	directory := t.TempDir()
	binary := filepath.Join(directory, "litebackup")
	calls := filepath.Join(directory, "calls")
	script := "#!/bin/sh\nprintf '%s %s\\n' \"$1\" \"${LITEBACKUP_API_KEY-unset}\" >> '" +
		calls + "'\n"
	if err := os.WriteFile(binary, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("XDG_RUNTIME_DIR", t.TempDir())
	return binary, calls
}

// standInCalls reads what the stand-in was run with, one line per run.
func standInCalls(t *testing.T, calls string) []string {
	t.Helper()
	content, err := os.ReadFile(calls)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		t.Fatal(err)
	}
	return strings.Split(strings.TrimSuffix(string(content), "\n"), "\n")
}

// TestConnectPassesTheCredential checks what litebackup launch receives:
// ConnectArgs.APIKey, a join token or an API key, over LITEBACKUP_API_KEY;
// the variable when APIKey is empty; and no credential when neither is set,
// so the daemon uses the key it saved. The daemon replaces a saved key only
// when the credential it receives differs, which the client's own tests
// check.
func TestConnectPassesTheCredential(t *testing.T) {
	const (
		joinToken = "litebackup_join_exampletokenvalue@backups.example.com"
		apiKey    = "litebackup_examplekeyvalue@backups.example.com"
	)
	connects := []struct {
		environment    string
		apiKeyArgument string
		launched       string
	}{
		{environment: "", apiKeyArgument: joinToken, launched: "launch " + joinToken},
		{environment: joinToken, apiKeyArgument: apiKey, launched: "launch " + apiKey},
		{environment: apiKey, apiKeyArgument: "", launched: "launch " + apiKey},
		{environment: joinToken, apiKeyArgument: "", launched: "launch " + joinToken},
	}
	for _, connect := range connects {
		binary, calls := credentialStandIn(t)
		t.Setenv(APIKeyEnvironment, connect.environment)
		_, err := Connect(
			t.Context(),
			&ConnectArgs{Session: "unit", BinaryPath: binary, APIKey: connect.apiKeyArgument},
		)
		if err == nil {
			t.Fatal("Connect succeeded without a daemon")
		}
		launched := standInCalls(t, calls)
		if len(launched) != 1 || launched[0] != connect.launched {
			t.Fatalf("launch ran with %q, want %q", launched, connect.launched)
		}
	}

	binary, calls := credentialStandIn(t)
	if err := os.Unsetenv(APIKeyEnvironment); err != nil {
		t.Fatal(err)
	}
	_, err := Connect(t.Context(), &ConnectArgs{Session: "unit", BinaryPath: binary})
	if err == nil {
		t.Fatal("Connect succeeded without a daemon")
	}
	if launched := standInCalls(t, calls); len(launched) != 1 || launched[0] != "launch unset" {
		t.Fatalf("launch ran with %q, want no credential", launched)
	}
}

// TestConnectRefusesInviteCredentials checks that an invite token, in
// ConnectArgs.APIKey or LITEBACKUP_API_KEY, fails with invalid_argument
// before litebackup runs, as a malformed variable does for a foreground
// backup.
func TestConnectRefusesInviteCredentials(t *testing.T) {
	const invite = "litebackup_invite_exampleinvitevalue@backups.example.com"
	binary, calls := credentialStandIn(t)
	t.Setenv(APIKeyEnvironment, "")
	_, err := Connect(
		t.Context(),
		&ConnectArgs{Session: "unit", BinaryPath: binary, APIKey: invite},
	)
	if !IsCode(err, CodeInvalidArgument) {
		t.Fatalf("an invite in APIKey: %v, want invalid_argument", err)
	}
	t.Setenv(APIKeyEnvironment, invite)
	_, err = Connect(t.Context(), &ConnectArgs{Session: "unit", BinaryPath: binary})
	if !IsCode(err, CodeInvalidArgument) {
		t.Fatalf("an invite in %s: %v, want invalid_argument", APIKeyEnvironment, err)
	}
	t.Setenv(APIKeyEnvironment, "litebackup_examplekeyvalue")
	t.Setenv(BinaryPathEnvironment, binary)
	err = Backup(t.Context(), "/var/lib/app/main.sqlite", nil)
	if !IsCode(err, CodeInvalidArgument) {
		t.Fatalf("a key without a host for a foreground backup: %v, want invalid_argument", err)
	}
	if launched := standInCalls(t, calls); launched != nil {
		t.Fatalf("litebackup ran with %q", launched)
	}
}

// TestBackupPassesTheCredential checks that a foreground backup gets
// LITEBACKUP_API_KEY, from the environment or from the client's APIKey.
func TestBackupPassesTheCredential(t *testing.T) {
	const apiKey = "litebackup_examplekeyvalue@backups.example.com"
	binary, calls := credentialStandIn(t)
	t.Setenv(APIKeyEnvironment, apiKey)
	t.Setenv(BinaryPathEnvironment, binary)
	if err := Backup(t.Context(), "/var/lib/app/main.sqlite", nil); err != nil {
		t.Fatal(err)
	}
	t.Setenv(APIKeyEnvironment, "")
	const joinToken = "litebackup_join_exampletokenvalue@backups.example.com"
	err := backupWithBinary(t.Context(), &backupWithBinaryArgs{
		session:    "unit",
		binaryPath: binary,
		apiKey:     joinToken,
		path:       "/var/lib/app/main.sqlite",
	})
	if err != nil {
		t.Fatal(err)
	}
	launched := standInCalls(t, calls)
	expected := []string{"backup " + apiKey, "backup " + joinToken}
	if strings.Join(launched, "\n") != strings.Join(expected, "\n") {
		t.Fatalf("backup ran with %q, want %q", launched, expected)
	}
}

// TestLaunchFailedCarriesTheCommandMessage reports launch_failed with the
// launch command's own message, not its whole error line, and passes
// --stdout-format json so that line is JSON.
func TestLaunchFailedCarriesTheCommandMessage(t *testing.T) {
	directory := t.TempDir()
	arguments := filepath.Join(directory, "arguments")
	binary := filepath.Join(directory, "litebackup")
	script := "#!/bin/sh\n" +
		`echo "$@" > "` + arguments + `"` + "\n" +
		`echo '{"event":"warning","code":"` + specCodeNotConfigured + `",` +
		`"message":"the server waits for setup"}' >&2` + "\n" +
		`echo '{"code":"` + specCodeUnauthenticated + `",` +
		`"message":"this machine has not joined a server"}' >&2` + "\n" +
		"exit 1\n"
	if err := os.WriteFile(binary, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	err := launchDaemon(t.Context(), "unit", filepath.Join(directory, "client.sock"), "", binary)
	if !IsCode(err, CodeLaunchFailed) {
		t.Fatalf("launch: %v", err)
	}
	var launchError *Error
	if !errors.As(err, &launchError) {
		t.Fatalf("launch: %v", err)
	}
	if launchError.Message != "starting Litebackup failed: this machine has not joined a server" {
		t.Fatalf("message %q", launchError.Message)
	}
	launchArguments, err := os.ReadFile(arguments)
	if err != nil {
		t.Fatal(err)
	}
	if string(launchArguments) != "launch --session unit --stdout-format json\n" {
		t.Fatalf("arguments %q", launchArguments)
	}
	code, refusal := launchRefusal([]byte("plain text\n"))
	if code != CodeLaunchFailed || refusal != "plain text" {
		t.Fatalf("plain output %s %q", code, refusal)
	}
}

// TestLaunchDrainRequiredKeepsItsCode launches with a credential the launch
// command refuses because the session's client holds write locks: the error
// keeps drain_required and the command's message.
func TestLaunchDrainRequiredKeepsItsCode(t *testing.T) {
	directory := t.TempDir()
	binary := filepath.Join(directory, "litebackup")
	script := "#!/bin/sh\n" +
		`echo '{"code":"` + CodeDrainRequired + `",` +
		`"message":"drain it first with litebackup drain, then change the key"}' >&2` + "\n" +
		"exit 1\n"
	if err := os.WriteFile(binary, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	err := launchDaemon(t.Context(), "unit", filepath.Join(directory, "client.sock"), "", binary)
	if !IsCode(err, CodeDrainRequired) {
		t.Fatalf("launch: %v", err)
	}
	var launchError *Error
	if !errors.As(err, &launchError) || !strings.HasSuffix(
		launchError.Message,
		"drain it first with litebackup drain, then change the key",
	) {
		t.Fatalf("launch: %v", err)
	}
}

// TestUnusableBinaryNamesItsInterpreter launches with a binary that is there
// but cannot run: launch_failed names its first line, and a file without
// one names its interpreter or dynamic loader.
func TestUnusableBinaryNamesItsInterpreter(t *testing.T) {
	directory := t.TempDir()
	binary := filepath.Join(directory, "litebackup")
	if err := os.WriteFile(binary, []byte("#!/nonexistent/litebackup-interpreter\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	unusableBinary := "starting " + binary + " failed: it cannot run because the interpreter " +
		"its first line names is not on this machine: #!/nonexistent/litebackup-interpreter"
	err := launchDaemon(t.Context(), "unit", filepath.Join(directory, "client.sock"), "", binary)
	if !IsCode(err, CodeLaunchFailed) || !strings.Contains(err.Error(), unusableBinary) {
		t.Fatalf("a launch with a script whose interpreter is missing: %v", err)
	}
	if message := startFailureMessage(binary, syscall.ENOTDIR); message != unusableBinary {
		t.Fatalf("ENOTDIR on a script: %q", message)
	}
	native := filepath.Join(directory, "native")
	if err := os.WriteFile(native, []byte("\x7fELF"), 0o755); err != nil {
		t.Fatal(err)
	}
	want := "starting " + native + " failed: it cannot run because its interpreter or dynamic loader " +
		"is not on this machine"
	if message := startFailureMessage(native, fs.ErrNotExist); message != want {
		t.Fatalf("a file without a #! line: %q", message)
	}
}
