package litebackup

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
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
		return nil, errors.New("backup needs a path")
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
	return runBackup(ctx, c.session, path, args)
}

func runBackup(ctx context.Context, session, path string, args *BackupArgs) error {
	if args == nil {
		args = &BackupArgs{}
	}
	binary, err := Binary()
	if err != nil {
		return err
	}
	if path == "" || (args.Name != "" && args.Prefix != "") || strings.HasSuffix(args.Name, "/") {
		return &Error{Code: "invalid_argument", Message: "expected a source and either a name or a prefix"}
	}
	commandArgs := []string{"backup", "--json-errors"}
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
	cmd.Stderr = os.Stderr
	out, err := cmd.Output()
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if err != nil {
		var failure Error
		if json.Unmarshal(out, &failure) == nil && failure.Code != "" {
			return &failure
		}
	}
	return err
}
