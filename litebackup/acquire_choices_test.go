package litebackup

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// TestUnknownAcquireChoicesAreDenied checks that an offload durability the
// daemon does not know is invalid_argument before anything is sent, and that
// a lease refuses an unknown durability before it creates the local file.
func TestUnknownAcquireChoicesAreDenied(t *testing.T) {
	daemon := newFakeDaemon(t)
	client := daemon.connect(t)
	for _, args := range []*AcquireArgs{
		{OffloadDurability: Durability("sometimes")},
	} {
		if _, err := client.Acquire(t.Context(), "app.db", args); !IsCode(err, CodeInvalidArgument) {
			t.Errorf("acquire with %+v: %v, want invalid_argument", args, err)
		}
	}
	if sent := daemon.received("/v1/databases/acquire"); len(sent) != 0 {
		t.Fatalf("refused acquires reached the daemon: %d", len(sent))
	}
	pool, err := NewPool(client, &PoolArgs[string]{Adapter: slowCloseAdapter{}})
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close(t.Context())
	path := filepath.Join(t.TempDir(), "agents", "42.sqlite")
	err = pool.Lease(t.Context(), "agents/42.sqlite", &LeaseArgs{
		Path:       path,
		Durability: Durability("sometimes"),
	}, func(context.Context, string) error { return nil })
	if !IsCode(err, CodeInvalidArgument) {
		t.Fatalf("a lease with an unknown durability: %v, want invalid_argument", err)
	}
	if _, statErr := os.Stat(filepath.Dir(path)); !os.IsNotExist(statErr) {
		t.Fatalf("a refused lease created %s: %v", filepath.Dir(path), statErr)
	}
}
