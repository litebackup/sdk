package litebackup

import "testing"

// callerCode is a caller's own string type for error codes.
type callerCode string

// TestErrorCodesAreUntyped checks that the Code* constants taken from the
// spec stay untyped, so callers may assign them to their own string types,
// and keep their wire values.
func TestErrorCodesAreUntyped(t *testing.T) {
	codes := map[callerCode]string{
		CodeBusy:            "busy",
		CodeCancelled:       "cancelled",
		CodeFileExists:      "file_exists",
		CodeLockUnavailable: "lock_unavailable",
		CodeInvalidArgument: "invalid_argument",
		CodeNotFound:        "not_found",
		CodeRuleExists:      "rule_exists",
		CodeSDKUnsupported:  "sdk_unsupported",
		CodeStillOpen:       "still_open",
		CodeTimeout:         "timeout",
	}
	for code, expectedWireCode := range codes {
		if string(code) != expectedWireCode {
			t.Errorf("%s is %q", expectedWireCode, code)
		}
	}
}
