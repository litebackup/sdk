package litebackup

import (
	"io/fs"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

// TestCurrentUmask reads the process umask, set for the test, from
// /proc/self/status or the fallback, parses a status file's Umask line, and
// sends a given umask in place of the process's, refusing one past 0777.
func TestCurrentUmask(t *testing.T) {
	previous := syscall.Umask(0o027)
	defer syscall.Umask(previous)
	if mask := currentUmask(); mask != 0o027 {
		t.Fatalf("umask %o, want 027", mask)
	}
	status := filepath.Join(t.TempDir(), "status")
	statusText := []byte("Name:\tapp\nUmask:\t0077\nState:\tR\n")
	if err := os.WriteFile(status, statusText, 0o600); err != nil {
		t.Fatal(err)
	}
	mask, ok := statusUmask(status)
	if !ok || mask != 0o077 {
		t.Fatalf("status umask %o %t", mask, ok)
	}
	if _, ok := statusUmask(filepath.Join(t.TempDir(), "missing")); ok {
		t.Fatal("a missing status file gave a umask")
	}
	sent, err := requestUmask(nil)
	if err != nil || *sent != 0o027 {
		t.Fatalf("default umask %v: %v", sent, err)
	}
	umask := fs.FileMode(0)
	sent, err = requestUmask(&umask)
	if err != nil || *sent != 0 {
		t.Fatalf("given umask %v: %v", sent, err)
	}
	umask = 0o1000
	if _, err := requestUmask(&umask); !IsCode(err, CodeInvalidArgument) {
		t.Fatalf("umask past 0777: %v", err)
	}
}
