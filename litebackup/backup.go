package litebackup

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
	"slices"
	"strings"
	"syscall"
	"time"
)

// StartBackupArgs maps matching files to remote names.
type StartBackupArgs struct {
	Name   string
	Prefix string
	// MaximumLocalDatabases, when positive, offloads the least recently
	// changed idle databases past this count once OffloadDurability holds them.
	MaximumLocalDatabases int64
	// OffloadDurability is what MaximumLocalDatabases waits for: DurabilityServer,
	// the default when empty, once the server holds a copy's last version,
	// or DurabilityCloud once the bucket holds it too.
	OffloadDurability Durability
	// OffloadRollbackJournal lets MaximumLocalDatabases offload rollback-journal
	// databases too. Set it only when nothing keeps them open while idle: no
	// lock shows an idle rollback-journal connection. WAL databases are
	// offloaded either way.
	OffloadRollbackJournal bool
}

// BackupArgs configures a foreground backup job.
type BackupArgs struct {
	// Name is the exact remote name of a single file; the file name when empty.
	Name string
	// Prefix puts each backed-up file under this namespace.
	Prefix string
	// Durability is DurabilityServer, the default and enough in most cases,
	// or DurabilityCloud to wait for the bucket too.
	Durability Durability
	// Timeout bounds the job, which then fails with timeout. A job that does
	// not answer within Timeout plus 30 seconds is stopped.
	Timeout time.Duration
}

// StartBackup saves a continuous backup rule for a file, directory, or glob
// and returns once the daemon saved it; listing and uploads run in the
// background. Starting the same path again with the same settings returns
// the saved rule, so call it on every start. Other settings for a saved path
// fail with rule_exists, and the error's Details["rule"] is the saved rule.
func (c *Client) StartBackup(
	ctx context.Context,
	path string,
	args *StartBackupArgs,
) (*BackupRule, error) {
	if args == nil {
		args = &StartBackupArgs{}
	}
	if err := validateStartBackup(path, args); err != nil {
		return nil, err
	}
	absolute, err := absolutePath(path)
	if err != nil {
		return nil, err
	}
	body := BackupRequest{
		Path:                   absolute,
		Name:                   args.Name,
		Prefix:                 args.Prefix,
		MaximumLocalDatabases:  args.MaximumLocalDatabases,
		OffloadRollbackJournal: args.OffloadRollbackJournal,
	}
	if args.OffloadDurability == DurabilityCloud {
		body.OffloadDurability = DurabilityCloud
	}
	var rule BackupRule
	if err := c.call(ctx, http.MethodPost, "/v1/backup-rules/start", body, &rule, 0); err != nil {
		return nil, err
	}
	return &rule, nil
}

// validateStartBackup checks what StartBackup needs before anything connects.
func validateStartBackup(path string, args *StartBackupArgs) error {
	if path == "" {
		return invalidArgument("backup needs a path")
	}
	if args != nil && args.Name != "" && args.Prefix != "" {
		return invalidArgument("choose a name or a prefix")
	}
	if args != nil {
		return validateOffloadDurability(args.OffloadDurability)
	}
	return nil
}

// Backup runs the foreground job for this session. Close ends a job in
// flight, as cancelling ctx does.
func (c *Client) Backup(ctx context.Context, path string, args *BackupArgs) error {
	if c.isClosed() {
		return closedError()
	}
	if c.customSocket {
		return invalidArgument(
			"foreground backup needs a named session and cannot target SocketPath",
		)
	}
	backupContext, cancel := context.WithCancel(ctx)
	defer cancel()
	stop := context.AfterFunc(c.lifetime, cancel)
	defer stop()
	err := backupWithBinary(backupContext, &backupWithBinaryArgs{
		session:    c.session,
		binaryPath: c.binaryPath,
		apiKey:     c.apiKey,
		path:       path,
		backup:     args,
	})
	if ctx.Err() == nil && IsCode(err, CodeCancelled) && c.isClosed() {
		return closedError()
	}
	return err
}

// foregroundGrace is how long past its timeout a foreground job may run
// before it is stopped, so it can report its own timeout first.
var foregroundGrace = deadlineGrace

// stopGrace is how long a stopped foreground job has to exit after SIGTERM
// before it gets SIGKILL.
var stopGrace = 15 * time.Second

// backupFailed is the code of a foreground job that failed without a
// structured final report; its message is the end of the job's error output.
const backupFailed = "backup_failed"

// stderrTail bounds how much of a failed job's error output a message keeps.
const stderrTail = 4096

// maximumFileDiagnostics is how many of the job's file diagnostics a failed
// backup's message lists.
const maximumFileDiagnostics = 20

// failureLines reads a job's error output line by line and keeps its
// structured error lines wherever they fall, however much log output lies
// between them: the first file diagnostics and the last line, the job's
// final report. It keeps the output's end for a job that wrote none.
type failureLines struct {
	pending     []byte
	diagnostics []Error
	last        *Error
	tail        []byte
}

// Write takes the next part of the job's error output.
func (f *failureLines) Write(output []byte) (int, error) {
	f.tail = append(f.tail, output...)
	if len(f.tail) > stderrTail {
		f.tail = f.tail[len(f.tail)-stderrTail:]
	}
	f.pending = append(f.pending, output...)
	for {
		end := bytes.IndexByte(f.pending, '\n')
		if end < 0 {
			break
		}
		f.parse(f.pending[:end])
		f.pending = f.pending[end+1:]
	}
	// A line this long is log output; only its end is kept, and it does not parse.
	if len(f.pending) > stderrPart {
		f.pending = f.pending[len(f.pending)-stderrPart:]
	}
	return len(output), nil
}

// stderrPart bounds an unfinished line failureLines keeps.
const stderrPart = 32 << 10

// parse keeps line when it is a structured error.
func (f *failureLines) parse(line []byte) {
	var failure Error
	if json.Unmarshal(line, &failure) != nil || failure.Code == "" || failure.Message == "" {
		return
	}
	f.last = &failure
	// A file reported twice is listed once, so the limit counts distinct files.
	if len(f.diagnostics) < maximumFileDiagnostics &&
		!slices.ContainsFunc(f.diagnostics, func(diagnostic Error) bool {
			return diagnostic.Message == failure.Message
		}) {
		f.diagnostics = append(f.diagnostics, failure)
	}
}

// failure is the final report, followed by each file diagnostic the job
// printed before it, or nil when the job printed no structured error.
func (f *failureLines) failure() *Error {
	f.parse(f.pending)
	f.pending = nil
	if f.last == nil {
		return nil
	}
	final := *f.last
	listed := map[string]bool{final.Message: true}
	for _, diagnostic := range f.diagnostics {
		if listed[diagnostic.Message] {
			continue
		}
		listed[diagnostic.Message] = true
		final.Message += "\n" + diagnostic.Message
	}
	return &final
}

// backupWithBinaryArgs configures backupWithBinary: which job to run, and
// where and with what credential.
type backupWithBinaryArgs struct {
	// session names the session; the job's default when empty.
	session string
	// binaryPath is the litebackup binary; see Binary when empty.
	binaryPath string
	// apiKey is set as LITEBACKUP_API_KEY when not empty; the job inherits
	// the environment's otherwise.
	apiKey string
	// path is the source to back up; required.
	path string
	// backup holds the caller's options; nil uses the defaults.
	backup *BackupArgs
}

// backupWithBinary runs one foreground backup with the litebackup binary.
// nil args fail with invalid_argument, as a missing path does.
func backupWithBinary(ctx context.Context, args *backupWithBinaryArgs) error {
	if args == nil {
		args = &backupWithBinaryArgs{}
	}
	path := args.path
	backup := args.backup
	if backup == nil {
		backup = &BackupArgs{}
	}
	if path == "" || (backup.Name != "" && backup.Prefix != "") ||
		strings.HasSuffix(backup.Name, "/") {
		return invalidArgument("expected a source and either a name or a prefix")
	}
	if backup.Timeout < 0 {
		return invalidArgument("the backup timeout must not be negative")
	}
	if args.apiKey == "" {
		if _, err := checkCredential(os.Getenv(APIKeyEnvironment)); err != nil {
			return err
		}
	}
	binary, err := resolveBinary(args.binaryPath)
	if err != nil {
		return err
	}
	commandArgs := []string{"backup", "--stdout-format", "json"}
	if args.session != "" {
		commandArgs = append(commandArgs, "--session", args.session)
	}
	if backup.Durability != "" {
		commandArgs = append(commandArgs, "--durability", string(backup.Durability))
	}
	if backup.Timeout > 0 {
		commandArgs = append(commandArgs, "--timeout", backup.Timeout.String())
	}
	commandArgs = append(commandArgs, "--", path)
	if backup.Name != "" {
		commandArgs = append(commandArgs, backup.Name)
	}
	if backup.Prefix != "" {
		commandArgs = append(commandArgs, strings.TrimSuffix(backup.Prefix, "/")+"/")
	}
	// The job answers its own timeout; the local deadline only ends a job
	// that never does.
	jobContext, cancel := ctx, context.CancelFunc(func() {})
	if backup.Timeout > 0 {
		jobContext, cancel = context.WithTimeout(ctx, backup.Timeout+foregroundGrace)
	}
	defer cancel()
	cmd := exec.CommandContext(jobContext, binary, commandArgs...)
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = stopGrace
	if args.apiKey != "" {
		cmd.Env = append(os.Environ(), APIKeyEnvironment+"="+args.apiKey)
	}
	lines := &failureLines{}
	cmd.Stderr = lines
	err = cmd.Run()
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return &Error{Code: CodeCancelled, Message: "the backup was cancelled", Err: ctx.Err()}
	}
	if jobContext.Err() != nil {
		return &Error{
			Code:    CodeTimeout,
			Message: "the foreground backup did not finish within its timeout",
			Err:     context.DeadlineExceeded,
		}
	}
	var exited *exec.ExitError
	if !errors.As(err, &exited) {
		code := CodeLaunchFailed
		if errors.Is(err, exec.ErrNotFound) || binaryMissing(binary, err) {
			code = CodeBinaryNotFound
		}
		return &Error{
			Code:    code,
			Message: startFailureMessage(binary, err),
			Err:     err,
		}
	}
	// The job reports each file's error, the writer of a busy one included,
	// before its final report.
	finalFailure := lines.failure()
	if finalFailure != nil {
		return finalFailure
	}
	message := string(bytes.TrimSpace(lines.tail))
	if message == "" {
		message = "foreground backup failed with " + exited.ProcessState.String()
	}
	return &Error{Code: backupFailed, Message: message, Err: err}
}

// binaryMissing reports whether starting binary failed because no file is
// there. A start can fail with ENOENT while the file exists, when its script
// interpreter is missing; that file cannot run, which is launch_failed.
func binaryMissing(binary string, startErr error) bool {
	if !missingPath(startErr) {
		return false
	}
	_, err := os.Stat(binary)
	return missingPath(err)
}

// startFailureMessage says why starting binary failed. A file that is there
// but fails to start as missing cannot run: the interpreter its #! line
// names, or its dynamic loader, is not on this machine. The message names
// that line rather than repeating the file's path as if it were missing.
func startFailureMessage(binary string, startErr error) string {
	if !missingPath(startErr) || binaryMissing(binary, startErr) {
		return "starting " + binary + " failed: " + startErr.Error()
	}
	interpreterLine := scriptInterpreterLine(binary)
	if interpreterLine == "" {
		return "starting " + binary + " failed: it cannot run because " +
			"its interpreter or dynamic loader is not on this machine"
	}
	return "starting " + binary + " failed: it cannot run because " +
		"the interpreter its first line names is not on this machine: " + interpreterLine
}

// scriptInterpreterLine is the #! line that starts the file at path, or ""
// when it starts with none or cannot be read.
func scriptInterpreterLine(path string) string {
	file, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer file.Close()
	start := make([]byte, 256)
	count, _ := io.ReadFull(file, start)
	firstLine, _, _ := strings.Cut(string(start[:count]), "\n")
	if !strings.HasPrefix(firstLine, "#!") {
		return ""
	}
	return strings.TrimSpace(firstLine)
}
