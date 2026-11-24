// Command handoff is the Go step of the cross-language handoff test: it
// leases a database from a pool, checks and adds rows with the sqlite3
// command, and closes the pool, which releases it. LITEBACKUP_SESSION names its session.
//
// Usage: handoff NAME PATH EXPECTED-ROWS ADDED-ROWS
package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/litebackup/sdk/litebackup"
)

// commandAdapter hands the work its path; the sqlite3 command opens and closes it per statement.
type commandAdapter struct{}

func (commandAdapter) Open(_ context.Context, path string) (string, error) { return path, nil }
func (commandAdapter) Close(context.Context, string) error                 { return nil }

func sqlite(path string, statement string) (string, error) {
	output, err := exec.Command("sqlite3", "-cmd", ".timeout 2000", path, statement).
		CombinedOutput()
	return strings.TrimSpace(string(output)), err
}

func main() {
	if len(os.Args) != 5 {
		fmt.Fprintln(os.Stderr, "usage: handoff NAME PATH EXPECTED-ROWS ADDED-ROWS")
		os.Exit(2)
	}
	name, path := os.Args[1], os.Args[2]
	expected, _ := strconv.Atoi(os.Args[3])
	added, _ := strconv.Atoi(os.Args[4])
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	client, err := litebackup.Connect(ctx, nil)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer client.Close()
	pool, err := litebackup.NewPool(client, &litebackup.PoolArgs[string]{Adapter: commandAdapter{}})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	err = pool.Lease(ctx, name, &litebackup.LeaseArgs{Path: path}, func(_ context.Context, path string) error {
		count, err := sqlite(path, "SELECT count(*) FROM steps")
		if err != nil {
			return fmt.Errorf("reading: %v %s", err, count)
		}
		if count != strconv.Itoa(expected) {
			return fmt.Errorf("found %s rows, want %d", count, expected)
		}
		for index := range added {
			output, err := sqlite(path, fmt.Sprintf("INSERT INTO steps (body) VALUES ('go %d')", index))
			if err != nil {
				return fmt.Errorf("writing: %v %s", err, output)
			}
		}
		return nil
	})
	err = errors.Join(err, pool.Close(ctx))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Println("go handoff ok")
}
