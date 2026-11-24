package litebackup

import (
	"context"
	"sync"
)

// defaultClient is the client the package functions share, connected on
// first use with LITEBACKUP_API_KEY and LITEBACKUP_SESSION.
var defaultClient struct {
	mutex  sync.Mutex
	client *Client
	// connecting is closed when the connection under way ends; nil when no
	// connection is under way.
	connecting chan struct{}
}

// Default returns the shared client the package functions use, connecting
// on first use. One caller connects at a time; the others wait for it, each
// only as long as its own context allows. A failed connection is tried
// again by the next caller.
func Default(ctx context.Context) (*Client, error) {
	for {
		defaultClient.mutex.Lock()
		if client := defaultClient.client; client != nil {
			defaultClient.mutex.Unlock()
			return client, nil
		}
		connecting := defaultClient.connecting
		if connecting == nil {
			break
		}
		defaultClient.mutex.Unlock()
		select {
		case <-connecting:
		case <-ctx.Done():
			return nil, &Error{
				Code:    CodeCancelled,
				Message: "the call was cancelled while another caller connected the shared client",
				Err:     ctx.Err(),
			}
		}
	}
	connecting := make(chan struct{})
	defaultClient.connecting = connecting
	defaultClient.mutex.Unlock()
	client, err := Connect(ctx, nil)
	defaultClient.mutex.Lock()
	defaultClient.connecting = nil
	if err == nil {
		defaultClient.client = client
	}
	defaultClient.mutex.Unlock()
	close(connecting)
	if err != nil {
		return nil, err
	}
	return client, nil
}

// Backup runs once without connecting to or launching a daemon.
func Backup(ctx context.Context, path string, args *BackupArgs) error {
	return backupWithBinary(ctx, &backupWithBinaryArgs{path: path, backup: args})
}

// StartBackup saves a continuous rule in the local daemon.
func StartBackup(ctx context.Context, path string, args *StartBackupArgs) (*BackupRule, error) {
	if err := validateStartBackup(path, args); err != nil {
		return nil, err
	}
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.StartBackup(ctx, path, args)
}

// StopBackup stops the single-file backup with this remote database name.
func StopBackup(ctx context.Context, name string) (*StopBackupResult, error) {
	if err := validateStopBackup(name); err != nil {
		return nil, err
	}
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.StopBackup(ctx, name)
}

// StopBackupRule stops a whole backup rule using the ID returned by StartBackup.
func StopBackupRule(ctx context.Context, id string) (*StopBackupResult, error) {
	if err := validateStopBackupRule(id); err != nil {
		return nil, err
	}
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.StopBackupRule(ctx, id)
}

// StartFailover starts a failover with the shared client; see Client.StartFailover.
func StartFailover(ctx context.Context, path string, args *FailoverArgs) (*Failover, error) {
	if err := validateFailover(path, args); err != nil {
		return nil, err
	}
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.StartFailover(ctx, path, args)
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
func ReplicatePrefix(
	ctx context.Context,
	prefix string,
	args *ReplicatePrefixArgs,
) (*PrefixReplica, error) {
	if err := validateReplicatePrefix(args); err != nil {
		return nil, err
	}
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.ReplicatePrefix(ctx, prefix, args)
}

// Restore writes a version to a new file with the shared client; see Client.Restore.
func Restore(ctx context.Context, name string, args *RestoreArgs) (*RestoreResult, error) {
	if err := validateRestore(args); err != nil {
		return nil, err
	}
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Restore(ctx, name, args)
}

// Revert rolls a database back with the shared client; see Client.Revert.
func Revert(ctx context.Context, name string, args *RevertArgs) (*RevertResult, error) {
	if err := validateRevert(args); err != nil {
		return nil, err
	}
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Revert(ctx, name, args)
}

// Versions lists restorable versions with the shared client; see Client.Versions.
func Versions(ctx context.Context, name string, args *VersionsArgs) ([]RestorableVersion, error) {
	client, err := Default(ctx)
	if err != nil {
		return nil, err
	}
	return client.Versions(ctx, name, args)
}
