package litebackup

import "time"

// Role is what this machine does with a database.
type Role string

const (
	// RoleOrigin means this machine writes the database and backs it up.
	RoleOrigin Role = "origin"
	// RoleReplica means this machine keeps a live readonly copy.
	RoleReplica Role = "replica"
)

// State is where a database stands on this machine.
type State string

const (
	// StateSyncing means changes stream to the server.
	StateSyncing State = "syncing"
	// StateRetrying means an origin's syncs fail; LastError says why, and
	// NextRetryTime when the next attempt runs.
	StateRetrying State = "retrying"
	// StateSynced means a released database's last version is on the server.
	StateSynced State = "synced"
	// StateWaiting means another machine is the origin.
	StateWaiting State = "waiting"
	// StateOffloaded means the local file was removed because the bucket holds it.
	StateOffloaded State = "offloaded"
	// StateStopped means a replica no longer streams.
	StateStopped State = "stopped"
	// StateConflicted means another machine wrote while this copy had changes.
	StateConflicted State = "conflicted"
	// StateError means the database is not synced and will not be retried,
	// such as a replica past the server's lock cap; LastError says why.
	// Failing syncs that are retried are StateRetrying.
	StateError State = "error"
)

// Durability is how far a version must reach before a wait returns.
type Durability string

const (
	// DurabilityNone returns at once.
	DurabilityNone Durability = "none"
	// DurabilityServer waits for the server's acknowledgement.
	DurabilityServer Durability = "server"
	// DurabilityCloud waits for the bucket to hold the version too.
	DurabilityCloud Durability = "cloud"
)

// WriteProtection is how a replica keeps app writes out.
type WriteProtection string

const (
	// WriteProtectionLock holds the SQLite write lock, so app writes get SQLITE_BUSY while readers carry on.
	WriteProtectionLock WriteProtection = "lock"
	// WriteProtectionDetect rebases the replica after a local write.
	WriteProtectionDetect WriteProtection = "detect"
	// WriteProtectionNone trusts apps to open the replica with mode=ro.
	WriteProtectionNone WriteProtection = "none"
)

// SyncedState is a version the server acknowledged.
type SyncedState struct {
	Version int64      `json:"version"`
	Time    *time.Time `json:"time,omitempty"`
}

// DatabaseStatus is one database on this machine.
type DatabaseStatus struct {
	Name               string      `json:"name"`
	Path               string      `json:"path"`
	Role               Role        `json:"role"`
	State              State       `json:"state"`
	Acquired           bool        `json:"acquired"`
	Failover           bool        `json:"failover"`
	LocalChanged       bool        `json:"local_changed"`
	SyncedServer       SyncedState `json:"synced_server"`
	SyncedCloud        SyncedState `json:"synced_cloud"`
	JournalMode        string      `json:"journal_mode"`
	PageCount          int64       `json:"page_count"`
	SyncCount          int64       `json:"sync_count"`
	HintMisses         int64       `json:"hint_misses"`
	LastFullScanReason string      `json:"last_full_scan_reason,omitempty"`
	Writer             string      `json:"writer,omitempty"`
	LastError          string      `json:"last_error,omitempty"`
	LastErrorTime      *time.Time  `json:"last_error_time,omitempty"`
	// FirstUnsyncedChangeTime is when the first change the server lacks was
	// seen; nil when the server has every change seen. It may be earlier than
	// the real first change, never later.
	FirstUnsyncedChangeTime *time.Time `json:"first_unsynced_change_time,omitempty"`
	// NextRetryTime is when a failed sync runs again; nil when none is scheduled.
	NextRetryTime *time.Time `json:"next_retry_time,omitempty"`
	MachineLabel  string     `json:"machine_label,omitempty"`
	Maintenance   bool       `json:"maintenance,omitempty"`
	Offload       string     `json:"offload,omitempty"`
}

// DatabasePage contains the matching databases and a cursor when more follow.
type DatabasePage struct {
	Databases []DatabaseStatus `json:"databases"`
	Next      string           `json:"next,omitempty"`
}

// Status is the session, its server, and a page of databases on this machine.
type Status struct {
	Next                  string           `json:"next,omitempty"`
	RecentDiscoveryErrors []Event          `json:"recent_discovery_errors,omitempty"`
	Session               string           `json:"session"`
	Server                string           `json:"server"`
	ClientVersion         string           `json:"client_version"`
	SessionID             string           `json:"session_id"`
	LeaseExpires          *time.Time       `json:"lease_expires,omitempty"`
	Watch                 string           `json:"watch"`
	Databases             []DatabaseStatus `json:"databases"`
}

// Version is a version of a database the server can restore.
type Version struct {
	Version     int64     `json:"version"`
	StateDigest string    `json:"state_digest"`
	Time        time.Time `json:"time"`
}

// RestoreResult is the file a restore wrote.
type RestoreResult struct {
	Name        string `json:"name"`
	Path        string `json:"path"`
	Version     int64  `json:"version"`
	PageCount   int64  `json:"page_count"`
	PageSize    int64  `json:"page_size"`
	StateDigest string `json:"state_digest"`
}

// Conflict is a local copy saved because it diverged from the server.
type Conflict struct {
	ID            string    `json:"id"`
	Name          string    `json:"name"`
	Path          string    `json:"path"`
	ConflictPath  string    `json:"conflict_path"`
	SyncedVersion int64     `json:"synced_version"`
	ServerVersion int64     `json:"server_version"`
	Reason        string    `json:"reason"`
	Created       time.Time `json:"created"`
}

// AcquireResult is an acquired database and how its local file was made the head.
type AcquireResult struct {
	Database DatabaseStatus `json:"database"`
	// Outcome is created, adopted, resumed, downloaded, fast_forwarded, or healed.
	Outcome  string    `json:"outcome"`
	Conflict *Conflict `json:"conflict,omitempty"`
}

// ReleaseResult is a released database and the maintenance its release ran.
type ReleaseResult struct {
	Database     DatabaseStatus `json:"database"`
	Checkpointed bool           `json:"checkpointed"`
	Vacuumed     bool           `json:"vacuumed"`
	Offloaded    bool           `json:"offloaded"`
}

// Writer is which machine writes a database and how long its lease runs.
type Writer struct {
	Name string `json:"name"`
	// Active is whether some machine holds the write lock and its lease still runs.
	Active bool `json:"active"`
	// ThisMachine is whether this machine's session holds the write lock.
	ThisMachine bool `json:"this_machine"`
	// Machine is the writer's machine label, or empty when nobody holds the lock.
	Machine      string     `json:"machine"`
	Acquired     *time.Time `json:"acquired,omitempty"`
	LeaseExpires *time.Time `json:"lease_expires,omitempty"`
	// LeaseRemainingMillis is negative once the lease ran out.
	LeaseRemainingMillis int64 `json:"lease_remaining_millis,omitempty"`
	// Lapsed is whether the writer's lease ran out, so another machine may take over.
	Lapsed bool `json:"lapsed"`
}

// BackupRule is a backup or failover running on this machine.
type BackupRule struct {
	ID string `json:"id"`
	// Path is the path or glob the backup was started with.
	Path                  string `json:"path"`
	Name                  string `json:"name"`
	Prefix                string `json:"prefix"`
	Watch                 string `json:"watch"`
	MaximumLocalDatabases int64  `json:"maximum_local_databases"`
	Failover              bool   `json:"failover"`
}

// FailoverInfo is a failover as the daemon reports it.
type FailoverInfo struct {
	Name     string         `json:"name"`
	Path     string         `json:"path"`
	Role     Role           `json:"role"`
	Database DatabaseStatus `json:"database"`
	Writer   Writer         `json:"writer"`
}

// PrefixReplica replicates every database under a prefix into a directory.
type PrefixReplica struct {
	ID              string `json:"id"`
	Prefix          string `json:"prefix"`
	To              string `json:"to"`
	WriteProtection string `json:"write_protection"`
	Databases       int64  `json:"databases"`
}

// ReplicaList is every prefix replica and every replica on this machine.
type ReplicaList struct {
	Prefixes []PrefixReplica  `json:"prefixes"`
	Replicas []DatabaseStatus `json:"replicas"`
}

// Event is one entry of the daemon's event log.
type Event struct {
	ID   int64     `json:"id"`
	Time time.Time `json:"time"`
	Name string    `json:"name,omitempty"`
	// Type is such as role_changed, conflict_saved, synced, or downloaded.
	Type    string `json:"type"`
	Message string `json:"message"`
	// Role is the new role of a role_changed event.
	Role Role `json:"role,omitempty"`
	// Path is the conflict copy of a conflict_saved event.
	Path string `json:"path,omitempty"`
}

// EventList is one page of events.
type EventList struct {
	Events []Event `json:"events"`
	// Next is the After of the following page.
	Next int64 `json:"next"`
}

// ShutdownResult is what a shutdown released.
type ShutdownResult struct {
	Released int64    `json:"released"`
	Failed   []string `json:"failed"`
}
