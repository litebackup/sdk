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
	CodeInvalidArgument = "invalid_argument"
	// CodeBinaryNotFound means no litebackup binary was found to start the daemon.
	CodeBinaryNotFound = "binary_not_found"
	// CodeLaunchFailed means litebackup launch ran and failed.
	CodeLaunchFailed = "launch_failed"
	// CodeConnectionFailed means the socket refused the connection or closed it mid-call.
	CodeConnectionFailed = "connection_failed"
	// CodeIncompatibleVersion means the daemon is older than MinimumDaemonVersion.
	CodeIncompatibleVersion = "incompatible_version"
	// CodeCancelled means the caller cancelled the wait; the daemon may still finish the operation.
	CodeCancelled = "cancelled"
	// CodeClosed means the client was closed.
	CodeClosed = "closed"
	// CodeInvalidResponse means the daemon answered 200 with a body the SDK cannot decode.
	CodeInvalidResponse = "invalid_response"
	// CodeScopeCleanupFailed means a managed scope could not close or release its database.
	CodeScopeCleanupFailed = "scope_cleanup_failed"
)

// Codes the daemon answers with. Codes not listed here are kept verbatim too.
const (
	// CodeBusy means another job or a continuous backup holds a foreground backup's writer lock.
	CodeBusy = "busy"
	// CodeExists means a restore target exists, or the path already has a backup rule with other settings.
	CodeExists = "exists"
	// CodeInUse means another machine writes the database.
	CodeInUse = "in_use"
	// CodeNotFound means the database, rule, or conflict copy does not exist.
	CodeNotFound = "not_found"
	// CodeStillOpen means a process still has the database open, so it cannot be released.
	CodeStillOpen = "still_open"
	// CodeTimeout means the operation's timeout ran out.
	CodeTimeout = "timeout"
	// CodeSDKUnsupported means this SDK is older than the daemon accepts.
	CodeSDKUnsupported = "sdk_unsupported"
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
	// wire names, such as "rule" for exists from StartBackup. For
	// scope_cleanup_failed it holds step, name, path, and acquired or durability.
	Details map[string]any
	// Err is the underlying error, such as the context's error for cancelled.
	Err error
}

// Error returns the code and message.
func (e *Error) Error() string {
	return e.Code + ": " + e.Message
}

// Unwrap returns the underlying error.
func (e *Error) Unwrap() error { return e.Err }

// ErrBusy matches a writer lock held by another job or a continuous backup.
var ErrBusy = errors.New("backup writer busy")

// Is matches ErrBusy for busy and context.DeadlineExceeded for timeout.
func (e *Error) Is(target error) bool {
	return target == ErrBusy && e.Code == CodeBusy || target == context.DeadlineExceeded && e.Code == CodeTimeout
}

// IsCode reports whether err is an *Error with the given code.
func IsCode(err error, code string) bool {
	var answered *Error
	return errors.As(err, &answered) && answered.Code == code
}

// invalidArgument returns the error for a failed local check.
func invalidArgument(message string) *Error {
	return &Error{Code: CodeInvalidArgument, Message: message}
}

// responseError decodes a non-200 answer: a JSON object's code, message, and
// remaining fields, or http_<status> with the body's text.
func responseError(status int, content []byte) *Error {
	answered := &Error{Code: "http_" + strconv.Itoa(status), Message: strings.TrimSpace(string(content)), Status: status, Details: map[string]any{}}
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
