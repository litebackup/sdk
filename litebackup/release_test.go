package litebackup

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

// The fixtures are shared with the TypeScript tests: the website's package
// metadata as Litebackup's release key signed it, and a copy whose 0.3.30
// document has one SHA-256 changed under the original signature.
func readFixture(t *testing.T, name string) []byte {
	t.Helper()
	content, err := os.ReadFile(filepath.Join("..", "testdata", "contract", "binary", name))
	if err != nil {
		t.Fatal(err)
	}
	return content
}

func TestFindReleaseSigned(t *testing.T) {
	metadata := readFixture(t, "package-metadata.json")
	release, err := FindRelease(metadata, nil)
	if err != nil {
		t.Fatal(err)
	}
	if release.Version != "0.3.30" || len(release.Channels) != 1 || release.Channels[0] != StableChannel {
		t.Fatalf("release %s on %v", release.Version, release.Channels)
	}
	file, err := release.File("litebackup-linux-amd64")
	if err != nil {
		t.Fatal(err)
	}
	if file.SHA256 != "3338a925ace256760fe48c1fb660c6ffdcc9be570fef5af7f40f8d34ae9391d7" || file.Size != 47322128 {
		t.Fatalf("file %+v", file)
	}
	if _, err := FindRelease(metadata, &FindReleaseArgs{Version: "0.3.1"}); err == nil {
		t.Fatal("found a version the metadata does not list")
	}
	if _, err := FindRelease(metadata, &FindReleaseArgs{Keys: map[string]string{"01a1ead6-4b94-79ca-81f4-3a67c112bc0b": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}}); !errors.Is(err, ErrSignature) {
		t.Fatalf("another key verified the release: %v", err)
	}
}

func TestFindReleaseTampered(t *testing.T) {
	metadata := readFixture(t, "package-metadata-tampered.json")
	if _, err := FindRelease(metadata, nil); !errors.Is(err, ErrSignature) {
		t.Fatalf("the tampered release verified: %v", err)
	}
}
