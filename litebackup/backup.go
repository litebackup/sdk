package litebackup

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

// StartBackupArgs maps matching files to remote names.
type StartBackupArgs struct {
	Name                  string
	Prefix                string
	MaximumLocalDatabases int64
}

// BackupArgs configures a foreground backup job.
type BackupArgs struct {
	Name    string
	Prefix  string
	Wait    Durability
	Timeout time.Duration
}

func (c *Client) StartBackup(ctx context.Context, path string, args *StartBackupArgs) (*BackupRule, error) {
	if args == nil {
		args = &StartBackupArgs{}
	}
	if path == "" {
		return nil, &Error{Code: "invalid_argument", Message: "backup needs a path"}
	}
	if args.Name != "" && args.Prefix != "" {
		return nil, &Error{Code: "invalid_argument", Message: "choose a name or a prefix"}
	}
	absolute, err := absolutePath(path)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"path": absolute}
	if args.Name != "" {
		body["name"] = args.Name
	}
	if args.Prefix != "" {
		body["prefix"] = args.Prefix
	}
	if args.MaximumLocalDatabases != 0 {
		body["maximum_local_databases"] = args.MaximumLocalDatabases
	}
	var info BackupRule
	if err := c.call(ctx, http.MethodPost, "/v1/backup-rules/start", body, &info); err != nil {
		return nil, err
	}
	return &info, nil
}

// ErrBusy matches a writer lock held by another job or a continuous backup.
var ErrBusy = errors.New("backup writer busy")

// Is matches the corresponding busy and timeout errors.
func (e *Error) Is(target error) bool {
	return target == ErrBusy && e.Code == "busy" || target == context.DeadlineExceeded && e.Code == "timeout"
}

// Backup runs the foreground job for this session.
func (c *Client) Backup(ctx context.Context, path string, args *BackupArgs) error {
	if c.customSocket {
		return &Error{Code: "invalid_argument", Message: "foreground backup needs a named session and cannot target SocketPath"}
	}
	return runBackup(ctx, c.session, path, args)
}

func runBackup(ctx context.Context, session, path string, args *BackupArgs) error {
	if args == nil {
		args = &BackupArgs{}
	}
	if path == "" || (args.Name != "" && args.Prefix != "") || strings.HasSuffix(args.Name, "/") {
		return &Error{Code: "invalid_argument", Message: "expected a source and either a name or a prefix"}
	}
	binary, err := Binary()
	if err != nil {
		return err
	}
	commandArgs := []string{"backup", "--stdout-format", "json"}
	if session != "" {
		commandArgs = append(commandArgs, "--session", session)
	}
	if args.Wait != "" {
		commandArgs = append(commandArgs, "--wait", string(args.Wait))
	}
	if args.Timeout > 0 {
		commandArgs = append(commandArgs, "--timeout", args.Timeout.String())
	}
	commandArgs = append(commandArgs, "--", path)
	if args.Name != "" {
		commandArgs = append(commandArgs, args.Name)
	}
	if args.Prefix != "" {
		commandArgs = append(commandArgs, strings.TrimSuffix(args.Prefix, "/")+"/")
	}
	cmd := exec.CommandContext(ctx, binary, commandArgs...)
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = 15 * time.Second
	_, err = cmd.Output()
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	var exited *exec.ExitError
	if errors.As(err, &exited) {
		var firstMessage string
		var outcome *Error
		for line := range bytes.Lines(exited.Stderr) {
			var failure Error
			if json.Unmarshal(line, &failure) == nil && failure.Code != "" && failure.Message != "" {
				if outcome == nil {
					firstMessage = failure.Message
				}
				outcome = &failure
			}
		}
		if outcome != nil {
			// Keep one file diagnostic alongside the final job outcome.
			if firstMessage != outcome.Message {
				outcome.Message += "\n" + firstMessage
			}
			return outcome
		}
	}
	return err
}
