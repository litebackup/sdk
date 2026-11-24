package litebackup

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestDefaultEndsWithItsContext stalls the shared client's first
// connection on a client binary that never comes up, and checks that a
// second caller returns as cancelled when its own context ends, instead of
// waiting for the first.
func TestDefaultEndsWithItsContext(t *testing.T) {
	directory := t.TempDir()
	binary := filepath.Join(directory, "litebackup")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\nsleep 3\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("XDG_RUNTIME_DIR", t.TempDir())
	t.Setenv("LITEBACKUP_SESSION", "stalled")
	t.Setenv("LITEBACKUP_PATH", binary)
	t.Cleanup(func() {
		defaultClient.mutex.Lock()
		defaultClient.client = nil
		defaultClient.mutex.Unlock()
	})
	first, cancelFirst := context.WithCancel(context.Background())
	defer cancelFirst()
	firstDone := make(chan struct{})
	go func() {
		defer close(firstDone)
		_, _ = Default(first)
	}()
	time.Sleep(200 * time.Millisecond)
	second, cancelSecond := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancelSecond()
	started := time.Now()
	_, err := Default(second)
	if !IsCode(err, CodeCancelled) {
		t.Fatalf("the second caller got %v, want cancelled", err)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("the second caller waited %s for the first", elapsed)
	}
	cancelFirst()
	<-firstDone
}
