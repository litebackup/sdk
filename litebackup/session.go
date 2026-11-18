package litebackup

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	// TokenEnvironment holds the join token, TOKEN@HOST, a daemon joins with on its first start.
	TokenEnvironment = "LITEBACKUP_TOKEN"
	// SessionEnvironment names the session when Options.Session is empty.
	SessionEnvironment = "LITEBACKUP_SESSION"
	// BinaryPathEnvironment overrides the litebackup binary the SDK starts.
	BinaryPathEnvironment = "LITEBACKUP_PATH"
	// DefaultSession is the session used when nothing names one.
	DefaultSession = "default"
	// socketName is the daemon's socket inside a session's runtime directory.
	socketName = "client.sock"
	// launchTimeout bounds litebackup launch.
	launchTimeout = 30 * time.Second
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
// under the temporary directory.
func SessionSocket(session string) (string, string, error) {
	name := session
	if name == "" {
		name = os.Getenv(SessionEnvironment)
	}
	if name == "" {
		name = DefaultSession
	}
	for index, character := range name {
		letterOrDigit := character >= 'a' && character <= 'z' || character >= '0' && character <= '9'
		if index == 0 && !letterOrDigit || !letterOrDigit && character != '-' && character != '_' && character != '.' {
			return "", "", fmt.Errorf("session names use [a-z0-9._-] and start with a letter or digit, got %q", name)
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
	}
	return name, filepath.Join(runtimeRoot, "litebackup", name, socketName), nil
}

// InstallCommand is the one line that installs the litebackup command for
// every user of this machine, on Linux and macOS.
func InstallCommand() string {
	return "curl -fsSL https://litebackup.com/cli.sh | sudo bash"
}

// Binary finds the litebackup binary: LITEBACKUP_PATH, then litebackup on PATH.
func Binary() (string, error) {
	override := strings.TrimSpace(os.Getenv(BinaryPathEnvironment))
	if override != "" {
		return override, nil
	}
	found, err := exec.LookPath("litebackup")
	if err == nil {
		return found, nil
	}
	return "", fmt.Errorf("the litebackup command is not installed; set %s or install it with: %s", BinaryPathEnvironment, InstallCommand())
}

// socketListening reports whether something accepts connections on the socket.
func socketListening(path string) bool {
	connection, err := net.DialTimeout("unix", path, time.Second)
	if err != nil {
		return false
	}
	return connection.Close() == nil
}

// launchDaemon starts the session daemon when nothing listens. It runs
// `litebackup launch --session`, which takes the session's launch lock and
// starts `litebackup daemon`, so concurrent processes start one daemon. The
// daemon joins with token on its first start. Where `litebackup install
// service` ran, systemd owns the daemon and the launch command says how to
// reach or start the service instead.
func launchDaemon(ctx context.Context, session string, socket string, token string) error {
	if socketListening(socket) {
		return nil
	}
	binary, err := Binary()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, launchTimeout)
	defer cancel()
	command := exec.CommandContext(ctx, binary, "launch", "--session", session)
	command.Env = append(os.Environ(), BinaryPathEnvironment+"="+binary)
	if token != "" {
		command.Env = append(command.Env, TokenEnvironment+"="+token)
	}
	output, err := command.CombinedOutput()
	if err != nil {
		var exitError *exec.ExitError
		if errors.As(err, &exitError) && len(strings.TrimSpace(string(output))) > 0 {
			return fmt.Errorf("starting Litebackup failed: %s", strings.TrimSpace(string(output)))
		}
		return fmt.Errorf("starting Litebackup failed: %w", err)
	}
	return nil
}
