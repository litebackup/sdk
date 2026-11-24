package litebackup

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	// APIKeyEnvironment holds the credential the daemon uses when it starts,
	// when ConnectArgs.APIKey is empty: an API key, or a join token the
	// daemon exchanges once for a key of the machine, then @ and the
	// server's address.
	APIKeyEnvironment = "LITEBACKUP_API_KEY"
	// SessionEnvironment names the session when ConnectArgs.Session is empty.
	SessionEnvironment = "LITEBACKUP_SESSION"
	// BinaryPathEnvironment overrides the litebackup binary the SDK starts.
	BinaryPathEnvironment = "LITEBACKUP_PATH"
	// DefaultSession is the session used when nothing names one.
	DefaultSession = "default"
	// socketName is the daemon's socket inside a session's runtime directory.
	socketName = "client.sock"
	// launchTimeout bounds litebackup launch.
	launchTimeout = 30 * time.Second
	// launchOutputDelay is how long launch waits for its output once its
	// context ended and the command was killed.
	launchOutputDelay = time.Second
)

// SystemConfigurationPath records a `litebackup install service` install.
// While it exists, root and the service's user reach the service's socket.
var SystemConfigurationPath = "/etc/litebackup/client.json"

// systemConfiguration is the part of the service install the SDK reads.
type systemConfiguration struct {
	UID              int    `json:"uid"`
	RuntimeDirectory string `json:"runtime_directory"`
}

// loadSystemConfiguration reads the service install, or returns nil when
// there is none or this SDK cannot read it.
func loadSystemConfiguration(path string) *systemConfiguration {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var configuration systemConfiguration
	if err := json.Unmarshal(content, &configuration); err != nil {
		return nil
	}
	if configuration.RuntimeDirectory == "" {
		return nil
	}
	return &configuration
}

// userConfiguration is the part of a user service install the SDK reads:
// ~/.config/litebackup/service.json, found from HOME alone.
type userConfiguration struct {
	RuntimeDirectory string `json:"runtime_directory"`
}

// loadUserConfiguration reads this user's service install, or returns nil
// when there is none or this SDK cannot read it.
func loadUserConfiguration() *userConfiguration {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil
	}
	content, err := os.ReadFile(filepath.Join(home, ".config", "litebackup", "service.json"))
	if err != nil {
		return nil
	}
	var configuration userConfiguration
	if err := json.Unmarshal(content, &configuration); err != nil {
		return nil
	}
	if configuration.RuntimeDirectory == "" {
		return nil
	}
	return &configuration
}

// SessionSocket resolves a session's name and socket exactly as the
// litebackup command does. Where `litebackup install service` ran as root,
// root and the service's user use the service's socket; where a user
// installed a service of their own, that user uses the directory it
// recorded, whatever this process's environment. Everyone else uses
// $XDG_RUNTIME_DIR/litebackup/<session>/client.sock, or a per-user directory
// under the temporary directory, which must be this user's alone.
func SessionSocket(session string) (string, string, error) {
	name := session
	if name == "" {
		name = os.Getenv(SessionEnvironment)
	}
	if name == "" {
		name = DefaultSession
	}
	for index, character := range name {
		letterOrDigit := character >= 'a' && character <= 'z' ||
			character >= '0' && character <= '9'
		if index == 0 && !letterOrDigit ||
			!letterOrDigit && character != '-' && character != '_' && character != '.' {
			return "", "", fmt.Errorf(
				"session names use [a-z0-9._-] and start with a letter or digit, got %q",
				name,
			)
		}
	}
	uid := os.Getuid()
	system := loadSystemConfiguration(SystemConfigurationPath)
	if system != nil && (uid == 0 || uid == system.UID) {
		return name, filepath.Join(system.RuntimeDirectory, name, socketName), nil
	}
	user := loadUserConfiguration()
	if user != nil {
		return name, filepath.Join(user.RuntimeDirectory, name, socketName), nil
	}
	runtimeRoot := os.Getenv("XDG_RUNTIME_DIR")
	if runtimeRoot == "" {
		runtimeRoot = filepath.Join(os.TempDir(), "litebackup-"+strconv.Itoa(uid))
		if err := privateRuntimeDirectory(runtimeRoot); err != nil {
			return "", "", err
		}
	}
	return name, filepath.Join(runtimeRoot, "litebackup", name, socketName), nil
}

// InstallCommand is the one line that installs the litebackup command on
// Linux and macOS for whoever runs it: into ~/.local/bin, or /usr/local/bin
// as root.
func InstallCommand() string {
	return "curl -fsSL https://litebackup.com/cli.sh | bash"
}

// Binary finds the litebackup binary: LITEBACKUP_PATH, then litebackup on
// PATH, then ~/.local/bin/litebackup, where InstallCommand puts it for a
// user other than root, which an app's PATH may not include. It fails with
// binary_not_found and the install command when there is none.
func Binary() (string, error) {
	return resolveBinary("")
}

// resolveBinary is Binary with an explicit override taking precedence. An
// override, the option or LITEBACKUP_PATH, that names no file is
// binary_not_found; it never falls back to another binary.
func resolveBinary(override string) (string, error) {
	if override = strings.TrimSpace(override); override != "" {
		return existingOverride(override)
	}
	if override = strings.TrimSpace(os.Getenv(BinaryPathEnvironment)); override != "" {
		return existingOverride(override)
	}
	found, err := exec.LookPath("litebackup")
	if err == nil {
		return found, nil
	}
	home, err := os.UserHomeDir()
	if err == nil {
		local := filepath.Join(home, ".local", "bin", "litebackup")
		stat, statErr := os.Stat(local)
		if statErr == nil && !stat.IsDir() && stat.Mode()&0o111 != 0 {
			return local, nil
		}
	}
	return "", &Error{
		Code: CodeBinaryNotFound,
		Message: fmt.Sprintf(
			"the litebackup command is not installed; set %s or install it with: %s",
			BinaryPathEnvironment,
			InstallCommand(),
		),
	}
}

// existingOverride returns an explicit binary path, or binary_not_found when
// no file is there. A file that cannot run fails later, as launch_failed.
func existingOverride(override string) (string, error) {
	_, err := os.Stat(override)
	if missingPath(err) {
		return "", &Error{
			Code: CodeBinaryNotFound,
			Message: fmt.Sprintf(
				"the litebackup binary %s does not exist; "+
					"set BinaryPath or %s to it, or install it with: %s",
				override,
				BinaryPathEnvironment,
				InstallCommand(),
			),
		}
	}
	return override, nil
}

// socketListening reports whether something accepts connections on the socket.
func socketListening(path string) bool {
	connection, err := net.DialTimeout("unix", path, time.Second)
	if err != nil {
		return false
	}
	return connection.Close() == nil
}

// launchRefusal is the code and message of the last error the launch
// command reported as JSON, or launch_failed and its whole output when it
// reported none. The code is drain_required when the launch refused another
// credential for a client that holds write locks, and launch_failed
// otherwise.
func launchRefusal(output []byte) (code string, message string) {
	code = CodeLaunchFailed
	var refusal string
	for line := range strings.SplitSeq(string(output), "\n") {
		var launchDiagnostic struct {
			Event   string `json:"event"`
			Code    string `json:"code"`
			Message string `json:"message"`
		}
		if json.Unmarshal([]byte(line), &launchDiagnostic) != nil {
			continue
		}
		// A warning carries an event; an error does not.
		if launchDiagnostic.Event == "" && launchDiagnostic.Code != "" &&
			launchDiagnostic.Message != "" {
			refusal = launchDiagnostic.Message
			code = CodeLaunchFailed
			if launchDiagnostic.Code == CodeDrainRequired {
				code = CodeDrainRequired
			}
		}
	}
	if refusal == "" {
		return CodeLaunchFailed, strings.TrimSpace(string(output))
	}
	return code, refusal
}

// launchDaemon starts the session daemon when nothing listens. It runs
// `litebackup launch --session`, which takes the session's launch lock and
// starts `litebackup daemon`, so concurrent processes start one daemon. The
// daemon uses apiKey, when set, as LITEBACKUP_API_KEY. Where `litebackup install
// service` ran, systemd or launchd owns the daemon and the launch command says how to
// reach or start the service instead.
func launchDaemon(
	ctx context.Context,
	session string,
	socket string,
	apiKey string,
	binaryPath string,
) error {
	if socketListening(socket) {
		return nil
	}
	binary, err := resolveBinary(binaryPath)
	if err != nil {
		return err
	}
	launchContext, cancel := context.WithTimeout(ctx, launchTimeout)
	defer cancel()
	command := exec.CommandContext(
		launchContext,
		binary,
		"launch",
		"--session",
		session,
		"--stdout-format",
		"json",
	)
	// Once the context ends and the command is killed, stop waiting for its
	// output too: a process it started may still hold the pipes open.
	command.WaitDelay = launchOutputDelay
	command.Env = append(os.Environ(), BinaryPathEnvironment+"="+binary)
	if apiKey != "" {
		command.Env = append(command.Env, APIKeyEnvironment+"="+apiKey)
	}
	output, err := command.CombinedOutput()
	if err != nil && ctx.Err() != nil {
		return &Error{
			Code:    CodeCancelled,
			Message: "starting Litebackup was cancelled: " + ctx.Err().Error(),
			Err:     ctx.Err(),
		}
	}
	if err != nil {
		var exitError *exec.ExitError
		if errors.As(err, &exitError) && len(strings.TrimSpace(string(output))) > 0 {
			code, refusal := launchRefusal(output)
			return &Error{
				Code:    code,
				Message: "starting Litebackup failed: " + refusal,
				Err:     err,
			}
		}
		return &Error{
			Code:    CodeLaunchFailed,
			Message: startFailureMessage(binary, err),
			Err:     err,
		}
	}
	return nil
}

// missingPath reports whether a stat failed because nothing is at the path:
// it, or a directory on its way, does not exist. A permission error is not
// a missing path.
func missingPath(err error) bool {
	return errors.Is(err, fs.ErrNotExist) || errors.Is(err, syscall.ENOTDIR)
}
