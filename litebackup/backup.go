package litebackup

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"
)

// durabilityPoll is how often Wait reads a database's status while it waits
// for the bucket.
var durabilityPoll = 500 * time.Millisecond

// BackupOptions configures a backup.
type BackupOptions struct {
	// Name sets the database name outright; it must be a valid name.
	Name string
	// Prefix goes before the normalized file name, such as "app/".
	Prefix string
	// Wait makes Backup return only once the server, or the bucket, holds
	// the first version of every matching database.
	Wait Durability
	// Timeout bounds Wait.
	Timeout time.Duration
	// MaximumLocalDatabases, when positive, offloads the least recently
	// changed idle databases past this count once the bucket holds them.
	MaximumLocalDatabases int64
}

// BackupRun is a backup of a database, or of every database matching a
// glob in the last path segment.
type BackupRun struct {
	client *Client
	// ID names the backup on this machine.
	ID string
	// Name is the database name for a single path, or empty for a glob.
	Name string
	// Path is the absolute path or glob.
	Path string
	// Databases are the databases the backup synced when it started.
	Databases []DatabaseStatus
}

// Backup backs up a database and keeps syncing it. Relative paths resolve
// against the working directory. The name is the file name, lowercased with
// other characters turned into -, after Prefix, unless Name sets it. When
// another machine already writes that name, the error is an *Error with
// code name_in_use and nothing is overwritten.
func (c *Client) Backup(ctx context.Context, path string, options BackupOptions) (*BackupRun, error) {
	if path == "" {
		return nil, errors.New("backup needs a path")
	}
	absolute, err := absolutePath(path)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"path": absolute}
	if options.Name != "" {
		body["name"] = options.Name
	}
	if options.Prefix != "" {
		body["prefix"] = options.Prefix
	}
	if options.Wait != "" {
		body["wait"] = options.Wait
	}
	if options.Timeout > 0 {
		body["timeout_millis"] = millis(options.Timeout)
	}
	if options.MaximumLocalDatabases > 0 {
		body["maximum_local_databases"] = options.MaximumLocalDatabases
	}
	var info BackupInfo
	if err := c.call(ctx, http.MethodPost, "/v1/backups/start", body, &info); err != nil {
		return nil, err
	}
	backup := &BackupRun{client: c, ID: info.ID, Name: info.Name, Path: info.Path, Databases: info.Databases}
	if backup.Name == "" && len(info.Databases) == 1 && !hasGlob(absolute) {
		backup.Name = info.Databases[0].Name
	}
	return backup, nil
}

// hasGlob reports whether a path's last segment is a glob.
func hasGlob(path string) bool {
	for _, character := range path {
		switch character {
		case '*', '?', '[':
			return true
		}
	}
	return false
}

// names returns the databases the backup syncs now.
func (b *BackupRun) names(ctx context.Context) ([]string, error) {
	backups, err := b.client.Backups(ctx)
	if err != nil {
		return nil, err
	}
	databases := b.Databases
	for index := range backups {
		if backups[index].ID == b.ID {
			databases = backups[index].Databases
			break
		}
	}
	names := make([]string, 0, len(databases))
	for index := range databases {
		names = append(names, databases[index].Name)
	}
	if len(names) == 0 && b.Name != "" {
		names = append(names, b.Name)
	}
	return names, nil
}

// Wait returns once every change written before the call is on the server
// (DurabilityServer) or in the bucket too (DurabilityCloud).
func (b *BackupRun) Wait(ctx context.Context, durability Durability) error {
	if durability == DurabilityNone {
		return nil
	}
	if durability != DurabilityServer && durability != DurabilityCloud {
		return fmt.Errorf("wait for server or cloud, not %q", durability)
	}
	names, err := b.names(ctx)
	if err != nil {
		return err
	}
	for _, name := range names {
		synced, err := b.client.Sync(ctx, name)
		if err != nil {
			return err
		}
		if durability == DurabilityCloud {
			if err := b.client.waitCloud(ctx, name, synced.SyncedServer.Version); err != nil {
				return err
			}
		}
	}
	return nil
}

// waitCloud reads a database's status until the bucket holds version.
func (c *Client) waitCloud(ctx context.Context, name string, version int64) error {
	ticker := time.NewTicker(durabilityPoll)
	defer ticker.Stop()
	for {
		status, err := c.Database(ctx, name)
		if err != nil {
			return err
		}
		if status.SyncedCloud.Version >= version {
			return nil
		}
		if status.State == StateError && status.LastError != "" {
			return &Error{Code: "sync_failed", Message: status.LastError, Status: http.StatusOK}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

// Stop stops the backup and releases its write locks. The files and their
// backups stay.
func (b *BackupRun) Stop(ctx context.Context) error {
	return b.client.StopBackup(ctx, StopBackupOptions{ID: b.ID})
}
