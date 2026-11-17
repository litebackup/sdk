package litebackup

import (
	"context"
	"sync"
)

// defaultClient is the client the package functions share, connected on
// first use with LITEBACKUP_TOKEN and LITEBACKUP_SESSION.
var defaultClient struct {
	mutex  sync.Mutex
	client *Client
}

// Default returns the shared client the package functions use, connecting
// on first use. A failed connection is tried again on the next call.
func Default(ctx context.Context) (*Client, error) {
	defaultClient.mutex.Lock()
	defer defaultClient.mutex.Unlock()
	if defaultClient.client != nil {
		return defaultClient.client, nil
	}
	client, err := Connect(ctx, Options{})
	if err != nil {
		return nil, err
	}
	defaultClient.client = client
	return client, nil
}

// Backup backs up a database with the shared client; see Client.Backup.
func Backup(ctx context.Context, path string, options BackupOptions) (*BackupRun, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Backup(ctx, path, options)
}

// Failover starts a failover with the shared client; see Client.Failover.
func Failover(ctx context.Context, path string, options FailoverOptions) (*FailoverRun, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Failover(ctx, path, options)
}

// Replicate starts a replica with the shared client; see Client.Replicate.
func Replicate(ctx context.Context, name string, options ReplicateOptions) (*DatabaseStatus, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Replicate(ctx, name, options)
}

// ReplicatePrefix registers replicas with the shared client; see Client.ReplicatePrefix.
func ReplicatePrefix(ctx context.Context, prefix string, options ReplicatePrefixOptions) (*PrefixReplica, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.ReplicatePrefix(ctx, prefix, options)
}

// Restore writes a version to a new file with the shared client; see Client.Restore.
func Restore(ctx context.Context, name string, options RestoreOptions) (*RestoreResult, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Restore(ctx, name, options)
}

// Versions lists restorable versions with the shared client; see Client.Versions.
func Versions(ctx context.Context, name string, options VersionsOptions) ([]Version, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Versions(ctx, name, options)
}
