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
	client, err := Connect(ctx, nil)
	if err != nil {
		return nil, err
	}
	defaultClient.client = client
	return client, nil
}

// Backup runs once without connecting to or launching a daemon.
func Backup(ctx context.Context, path string, args *BackupArgs) error {
	return runBackup(ctx, "", path, args)
}

// StartBackup saves a continuous rule in the local daemon.
func StartBackup(ctx context.Context, path string, args *StartBackupArgs) (*BackupRule, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.StartBackup(ctx, path, args)
}

// StopBackup stops the single-file backup with this remote database name.
func StopBackup(ctx context.Context, name string) (*StopBackupResult, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.StopBackup(ctx, name)
}

// StopBackupRule stops a whole backup rule using the ID returned by StartBackup.
func StopBackupRule(ctx context.Context, id string) (*StopBackupResult, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.StopBackupRule(ctx, id)
}

// Failover starts a failover with the shared client; see Client.Failover.
func Failover(ctx context.Context, path string, args *FailoverArgs) (*FailoverRun, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Failover(ctx, path, args)
}

// Replicate starts a replica with the shared client; see Client.Replicate.
func Replicate(ctx context.Context, name string, args *ReplicateArgs) (*DatabaseStatus, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Replicate(ctx, name, args)
}

// ReplicatePrefix registers replicas with the shared client; see Client.ReplicatePrefix.
func ReplicatePrefix(ctx context.Context, prefix string, args *ReplicatePrefixArgs) (*PrefixReplica, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.ReplicatePrefix(ctx, prefix, args)
}

// Restore writes a version to a new file with the shared client; see Client.Restore.
func Restore(ctx context.Context, name string, args *RestoreArgs) (*RestoreResult, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Restore(ctx, name, args)
}

// Versions lists restorable versions with the shared client; see Client.Versions.
func Versions(ctx context.Context, name string, args *VersionsArgs) ([]Version, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Versions(ctx, name, args)
}
