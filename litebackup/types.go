package litebackup

// The socket API's response shapes are generated into protocol.gen.go with
// their wire names: DatabaseStatus, DatabaseStatusList, Status,
// RestorableVersion, RestoreResult, Conflict, AcquireResult, ReleaseResult,
// Writer, BackupRule, PrefixReplica, ReplicaList, Event, EventList,
// StopBackupResult, and ShutdownResult. Optional fields hold their zero value
// when the daemon omits them; check a time with IsZero.

// Role is what this machine does with a database.
type Role = DatabaseStatusRole

const (
	// RoleOrigin means this machine writes the database and backs it up.
	RoleOrigin Role = DatabaseStatusRoleOrigin
	// RoleReplica means this machine keeps a live readonly copy.
	RoleReplica Role = DatabaseStatusRoleReplica
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
type WriteProtection = ReplicaRequestWriteProtection

const (
	// WriteProtectionLock holds the SQLite write lock, so app writes get SQLITE_BUSY while readers carry on.
	WriteProtectionLock WriteProtection = ReplicaRequestWriteProtectionLock
	// WriteProtectionDetect rebases the replica after a local write.
	WriteProtectionDetect WriteProtection = ReplicaRequestWriteProtectionDetect
	// WriteProtectionNone trusts apps to open the replica with mode=ro.
	WriteProtectionNone WriteProtection = ReplicaRequestWriteProtectionNone
)

// FailoverInfo is a failover as the daemon reports it, the socket API's
// Failover schema. The name Failover is the package function that starts one.
type FailoverInfo struct {
	Name     string         `json:"name"`
	Path     string         `json:"path"`
	Role     Role           `json:"role"`
	Database DatabaseStatus `json:"database"`
	Writer   Writer         `json:"writer"`
}
