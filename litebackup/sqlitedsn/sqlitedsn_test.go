package sqlitedsn

import (
	"errors"
	"path/filepath"
	"testing"
)

// TestResolve covers each form without a driver; internal/drivertest checks
// the same forms against the real drivers.
func TestResolve(t *testing.T) {
	directory := t.TempDir()
	t.Chdir(directory)
	cases := map[string]string{
		"app.sqlite":                        filepath.Join(directory, "app.sqlite"),
		"/var/lib/app/../app/main.sqlite":   "/var/lib/app/main.sqlite",
		"app.sqlite?_busy_timeout=5000":     filepath.Join(directory, "app.sqlite"),
		"?odd.sqlite":                       filepath.Join(directory, "?odd.sqlite"),
		"file:app.sqlite?cache=shared":      filepath.Join(directory, "app.sqlite"),
		"file:///var/lib/app/main.sqlite":   "/var/lib/app/main.sqlite",
		"file://localhost/var/lib/a%20b.db": "/var/lib/a b.db",
		"file:a+b.sqlite":                   filepath.Join(directory, "a+b.sqlite"),
		"file:x.sqlite?mode=rwc#memory":     filepath.Join(directory, "x.sqlite"),
	}
	for dsn, want := range cases {
		for _, resolve := range []func(string) (string, error){Modernc, Mattn} {
			got, err := resolve(dsn)
			if err != nil || got != want {
				t.Fatalf("%q: %q %v, want %q", dsn, got, err, want)
			}
		}
	}
	for _, dsn := range []string{"", ":memory:", "file::memory:", "file:x?mode=memory", "file:", "x.db?vfs=memdb", "file:x.db?vfs=memdb"} {
		if _, err := Modernc(dsn); !errors.Is(err, ErrNoFile) {
			t.Fatalf("%q: %v", dsn, err)
		}
	}
	if _, err := Mattn("file://host/x.db"); err == nil || errors.Is(err, ErrNoFile) {
		t.Fatalf("a remote authority: %v", err)
	}
	if _, err := Mattn("file:%zz.db"); err == nil {
		t.Fatal("a bad escape was accepted")
	}
}
