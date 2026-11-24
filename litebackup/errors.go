package litebackup

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"strings"
)

// Codes the SDK raises itself. Their Status is 0.
const (
	// CodeInvalidArgument means an argument failed local validation; nothing was sent.
	CodeInvalidArgument = specCodeInvalidArgument
	// CodeBinaryNotFound means no litebackup binary was found to start the daemon or a foreground backup.
	CodeBinaryNotFound = "binary_not_found"
	// CodeLaunchFailed means litebackup launch ran and failed.
	CodeLaunchFailed = specCodeLaunchFailed
	// CodeDrainRequired means litebackup launch refused APIKey, a credential
	// other than the one the session's client took its write locks with:
	// Shutdown with the current credential releases them first.
	CodeDrainRequired = specCodeDrainRequired
	// CodeConnectionFailed means the socket refused the connection or closed it mid-call.
	CodeConnectionFailed = "connection_failed"
	// CodeDaemonUnsupported means the daemon is older than MinimumDaemonVersion.
	CodeDaemonUnsupported = "daemon_unsupported"
	// CodeCancelled means the caller cancelled the wait; the daemon may still finish the operation.
	CodeCancelled = specCodeCancelled
	// CodeClosed means the client was closed.
	CodeClosed = "closed"
	// CodeInvalidResponse means the daemon answered 200 with a body the SDK cannot decode.
	CodeInvalidResponse = "invalid_response"
	// CodeLeaseCleanupFailed means a pool could not close or release a leased
	// database; the error is a *LeaseCleanupError that says which step failed.
	CodeLeaseCleanupFailed = "lease_cleanup_failed"
)

// Codes the daemon answers with. Codes not listed here are kept verbatim too.
const (
	// CodeBusy means another job or a continuous backup holds a foreground backup's writer lock.
	CodeBusy = specCodeBusy
	// CodeFileExists means the file a restore or an acquire would write, or a
	// journal file beside it, already exists.
	CodeFileExists = specCodeFileExists
	// CodeLockLost means this machine lost the database's write lock: another
	// machine acquired it or an admin broke it. The copy is conflicted, its
	// file as it was.
	CodeLockLost = specCodeLockLost
	// CodeLockUnavailable means another machine holds the database's write lock.
	CodeLockUnavailable = specCodeLockUnavailable
	// CodeNotFound means the database or rule does not exist.
	CodeNotFound = specCodeNotFound
	// CodeRuleDatabase means a backup rule on this machine keeps the database
	// and its write lock, so it cannot be released; stop its backup instead.
	CodeRuleDatabase = specCodeRuleDatabase
	// CodeRuleExists means the path already has a backup rule with other
	// settings; Details["rule"] is the saved rule.
	CodeRuleExists = specCodeRuleExists
	// CodeStillOpen means a process still has the database open, so it cannot be released.
	CodeStillOpen = specCodeStillOpen
	// CodeTimeout means the operation's timeout ran out.
	CodeTimeout = specCodeTimeout
	// CodeSDKUnsupported means this SDK is older than the daemon accepts.
	CodeSDKUnsupported = specCodeSDKUnsupported
	// CodeCursorExpired means an events cursor is older than the oldest event
	// the daemon keeps; a watch ends with it, and the caller picks a new cursor.
	CodeCursorExpired = specCodeCursorExpired
)

// Error is every failure the SDK returns: the daemon's answer, or an error
// the SDK raised itself with Status 0.
type Error struct {
	// Code is the daemon's code verbatim, or one of the Code constants.
	Code    string
	Message string
	// Status is the HTTP status of the answer, or 0 for errors the SDK raised.
	Status int
	// Details holds the answer's fields besides code and message under their
	// wire names, such as "rule" for rule_exists from StartBackup. For
	// lease_cleanup_failed it holds step, name, path, and acquired or durability.
	Details map[string]any
	// Err is the underlying error, such as the context's error for cancelled.
	Err error
}

// Error returns the code and message.
func (e *Error) Error() string {
	return e.Code + ": " + e.Message
}

// Unwrap returns the underlying error.
func (e *Error) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.Err
}

// ErrBusy matches a writer lock held by another job or a continuous backup.
var ErrBusy = errors.New("backup writer busy")

// Is matches ErrBusy for busy and context.DeadlineExceeded for timeout.
func (e *Error) Is(target error) bool {
	if e == nil {
		return false
	}
	code, isCodeTarget := target.(codeTarget)
	if isCodeTarget {
		return e.Code == string(code)
	}
	return target == ErrBusy && e.Code == CodeBusy ||
		target == context.DeadlineExceeded && e.Code == CodeTimeout
}

// IsCode reports whether err, or any error it wraps or joins, is an *Error
// with the given code.
func IsCode(err error, code string) bool {
	if errors.Is(err, codeTarget(code)) {
		return true
	}
	// Errors that convert themselves with an As method but do not wrap.
	var answered *Error
	return errors.As(err, &answered) && answered != nil && answered.Code == code
}

// codeTarget matches an *Error with this code in errors.Is.
type codeTarget string

func (c codeTarget) Error() string { return string(c) }

// invalidArgument returns the error for a failed local check.
func invalidArgument(message string) *Error {
	return &Error{Code: CodeInvalidArgument, Message: message}
}

// responseError decodes a non-200 answer: a JSON object's code, message, and
// remaining fields, or http_<status> with the body's text.
func responseError(status int, content []byte) *Error {
	answered := &Error{
		Code:    "http_" + strconv.Itoa(status),
		Message: strings.TrimSpace(string(content)),
		Status:  status,
		Details: map[string]any{},
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(content, &fields) != nil || fields == nil {
		return answered
	}
	var code, message string
	codeErr := json.Unmarshal(fields["code"], &code)
	messageErr := json.Unmarshal(fields["message"], &message)
	if codeErr != nil || code == "" {
		if messageErr == nil {
			answered.Message = message
		}
		return answered
	}
	answered.Code = code
	answered.Message = message
	for key, raw := range fields {
		if key == "code" || key == "message" {
			continue
		}
		var detail any
		if json.Unmarshal(raw, &detail) == nil {
			answered.Details[key] = detail
		}
	}
	return answered
}
