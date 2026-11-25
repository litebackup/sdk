# frozen_string_literal: true

# Code generated from client-openapi.yaml by scripts/generate. DO NOT EDIT.

module Litebackup
  # The socket API's types; Litebackup exports the ones its methods return.
  module Models
    # What happened to a database: role_changed (role is origin or replica, and message is text),
    # draining (a failover origin stops being the origin before a drain releases its lock: its role
    # is unknown until the next role_changed), refused (path names the refused file, such as a
    # failover's file that holds writes the server lacks), path_unreadable (path names the path a
    # backup rule could not read), and the others by their names.
    module EventType
      ACQUIRED = "acquired"
      DOWNLOAD_FINISHED = "download_finished"
      DOWNLOAD_STARTED = "download_started"
      DRAINING = "draining"
      IMAGE_WRITTEN = "image_written"
      LOCAL_WRITE = "local_write"
      LOCK_LOST = "lock_lost"
      OFFLOADED = "offloaded"
      PATH_UNREADABLE = "path_unreadable"
      REBASED = "rebased"
      REFUSED = "refused"
      RELEASED = "released"
      REPLICATING = "replicating"
      RESTORED = "restored"
      REVERTED = "reverted"
      ROLE_CHANGED = "role_changed"
      ROLLBACK_SCAN = "rollback_scan"
      SERVER_GENERATION = "server_generation"
      STOPPED = "stopped"
      TRACKED = "tracked"
      ALL = [
        ACQUIRED,
        DOWNLOAD_FINISHED,
        DOWNLOAD_STARTED,
        DRAINING,
        IMAGE_WRITTEN,
        LOCAL_WRITE,
        LOCK_LOST,
        OFFLOADED,
        PATH_UNREADABLE,
        REBASED,
        REFUSED,
        RELEASED,
        REPLICATING,
        RESTORED,
        REVERTED,
        ROLE_CHANGED,
        ROLLBACK_SCAN,
        SERVER_GENERATION,
        STOPPED,
        TRACKED
      ].freeze
    end

    class Event < Data.define(:id, :time, :name, :type, :message, :role, :path)
      extend WireModel
      # role: for role_changed, the new role
      # path: for refused, the refused file; for path_unreadable, the path the rule could not read
      WIRE_FIELDS = {
        id: [:integer, true],
        time: [:time, true],
        name: [:string, false],
        type: [:string, true],
        message: [:string, true],
        role: [:string, false],
        path: [:string, false]
      }.freeze
    end

    # A version the server acknowledged. Clients decide what durability is enough.
    class SyncedState < Data.define(:version, :time)
      extend WireModel
      WIRE_FIELDS = {
        version: [:integer, true],
        time: [:time, false]
      }.freeze
    end

    # The newest version the server reported the primary bucket holds.
    class ArchivedVersion < Data.define(:version, :archived)
      extend WireModel
      # archived: when the server recorded the version as archived
      WIRE_FIELDS = {
        version: [:integer, true],
        archived: [:time, false]
      }.freeze
    end

    # how far a version must reach: server once the server holds it, cloud once the bucket holds it
    # too
    module Durability
      SERVER = "server"
      CLOUD = "cloud"
      ALL = [
        SERVER,
        CLOUD
      ].freeze
    end

    class DatabaseStatus < Data.define(:name, :path, :role, :state, :acquired, :failover, :backup_rule_id, :local_changed, :synced_server, :synced_cloud, :journal_mode, :page_count, :sync_count, :hint_misses, :last_full_scan_reason, :writer_machine_label, :last_error, :last_error_time, :first_unsynced_change_time, :next_retry_time, :draining, :machine_label, :maintenance, :offload_on_release, :offload_durability)
      extend WireModel
      ROLE_ORIGIN = "origin"
      ROLE_REPLICA = "replica"
      STATE_PENDING = "pending"
      STATE_SYNCING = "syncing"
      STATE_RETRYING = "retrying"
      STATE_SYNCED = "synced"
      STATE_WAITING = "waiting"
      STATE_OFFLOADED = "offloaded"
      STATE_STOPPED = "stopped"
      STATE_CONFLICTED = "conflicted"
      STATE_ERROR = "error"

      # role: origin when this machine writes the database and backs it up; replica for a live
      # readonly copy
      # state: pending while a backup or failover origin holds no write lock, as after a start or a
      # drain, until a sync takes it (a start takes back the lock of each backup a drain left
      # pending while the head is still the version it last synced); syncing while changes stream;
      # retrying while an origin's syncs fail, with last_error saying why and next_retry_time when
      # the next attempt runs; synced when the server holds every change: a backup or failover
      # origin whose last sync landed with no change seen after it, until its next change, or a
      # released database whose last version is on the server; waiting while another machine is the
      # origin; offloaded when the local file was removed because the server, or the bucket when the
      # offload asked for it, holds its last version; stopped for a stopped replica; conflicted when
      # this copy was refused and left as it is, because its file differs from the server head or
      # its write lock was lost or broken; error with last_error
      # acquired: the database is acquired and released by a worker rather than backed up
      # continuously
      # failover: the database belongs to a failover and changes role when the origin does
      # backup_rule_id: the backup or failover rule that tracks the database on this machine; absent
      # for an acquired database and a replica
      # journal_mode: wal, rollback, or unknown before the first scan. A full scan of a
      # rollback-journal database holds a read lock that blocks the app's writers until it ends, so
      # it runs at full speed rather than at --scan-bytes-per-second, and the first scan past 256 MB
      # records a rollback_scan event with its duration. Switch such a database to WAL mode (PRAGMA
      # journal_mode = WAL), where scans never block writers; Litebackup never changes the journal
      # mode itself.
      # writer_machine_label: the machine label of the last writer this machine saw; empty once this
      # machine released the database, drained it, or offloaded a copy whose write lock it held, as
      # no writer is known then
      # first_unsynced_change_time: when the first change the server lacks was seen; absent when the
      # server has every change seen. It survives restarts and may be earlier than the real first
      # change, never later.
      # next_retry_time: when the next attempt of a failed sync runs; absent when none is scheduled
      # draining: set on a failover origin while this machine drains, and on one a drain released,
      # which holds no write lock until it takes one again; a failover then reports its role as
      # unknown, as after a draining event
      # machine_label: the name this machine shows on the server and as the writer of its databases
      # offload_on_release: the client deletes the released copy once offload_durability holds it
      # offload_durability: what an offload of this acquired copy waits for: server removes the
      # released copy once the server holds its last version, cloud once the bucket holds it too. An
      # acquire, a release with offload, and an explicit offload each save the durability they asked
      # for. Absent when the database is not acquired
      WIRE_FIELDS = {
        name: [:string, true],
        path: [:string, true],
        role: [:string, true],
        state: [:string, true],
        acquired: [:boolean, true],
        failover: [:boolean, true],
        backup_rule_id: [:string, false],
        local_changed: [:boolean, true],
        synced_server: [[:model, :SyncedState], true],
        synced_cloud: [[:model, :ArchivedVersion], true],
        journal_mode: [:string, true],
        page_count: [:integer, true],
        sync_count: [:integer, true],
        hint_misses: [:integer, true],
        last_full_scan_reason: [:string, false],
        writer_machine_label: [:string, false],
        last_error: [:string, false],
        last_error_time: [:time, false],
        first_unsynced_change_time: [:time, false],
        next_retry_time: [:time, false],
        draining: [:boolean, false],
        machine_label: [:string, false],
        maintenance: [:boolean, false],
        offload_on_release: [:boolean, false],
        offload_durability: [:string, false]
      }.freeze
    end

    class Status < Data.define(:next, :session, :server, :client_version, :session_id, :lease_expires, :watch, :open_file_limit, :unreadable_paths, :databases)
      extend WireModel
      # next: Pass this name as after to read the next page. Omitted on the last page.
      # open_file_limit: The soft limit on open files the daemon runs with. Each active backup holds
      # about four, and the daemon lowers its default number of active backups to fit.
      # unreadable_paths: the paths a backup rule cannot read, most recent first; the events API has
      # the history
      WIRE_FIELDS = {
        next: [:string, false],
        session: [:string, true],
        server: [:string, true],
        client_version: [:string, true],
        session_id: [:string, true],
        lease_expires: [:time, false],
        watch: [:string, true],
        open_file_limit: [:integer, false],
        unreadable_paths: [[:list, [:model, :Event]], false],
        databases: [[:list, [:model, :DatabaseStatus]], true]
      }.freeze
    end

    # What went wrong, in one word. The socket answers with file_exists (the file a restore or an
    # acquire would write, or a -journal, -wal, or -shm beside it, already exists), rule_exists (a
    # backup rule with other settings already covers the path, and the error carries that rule),
    # lock_unavailable (another machine holds the write lock a command asks for, as an acquire, a
    # revert, or a failover does), not_found, still_open, invalid_argument, timeout, cursor_expired
    # (an events cursor older than the oldest event kept), changed (an offload or acquire refused
    # because the local file changed under it: written after its last sync, never synced, or written
    # while an acquire replaced it), rule_database (a release of a database a backup rule on this
    # machine keeps, whose write lock the rule never gives up; stop its backup to hand it off),
    # not_durable (an offload refused until the server, or the bucket when it waits for cloud, holds
    # the synced version, or a cloud wait whose name now holds another server database), and the
    # others by their names; busy (a foreground backup refused because another job, a continuous
    # backup, or another machine writes the database), cancelled (a command interrupted before it
    # finished), drain_required (a credential other than the one the client took its write locks
    # with, refused until a drain released them), and launch_failed (the command could not start the
    # client on this machine, or the client exited before it listened) come from the litebackup
    # command alone, which also answers unauthenticated when this machine has not joined a server. A
    # refusal from the server becomes client_unsupported (426: the server needs a newer litebackup
    # on this machine) or, with 503, not_configured (the server waits for setup), wrong_server (the
    # server this machine reached is not the one its session joined, or does not own the database),
    # unauthenticated (the server no longer accepts this machine's key), forbidden (the key may not
    # do this), plan_limit (the server's plan has no room for another database), or integrity (the
    # version's bucket objects failed an integrity check). lock_unavailable, lock_lost, not_found,
    # invalid_argument, and timeout keep their meaning, and any other refusal (a busy server, one
    # short of disk, one unreachable) is unavailable. A client treats a code it does not know as a
    # failure.
    module ErrorCode
      BUSY = "busy"
      CANCELLED = "cancelled"
      CHANGED = "changed"
      CLIENT_UNSUPPORTED = "client_unsupported"
      CONFLICT = "conflict"
      CURSOR_EXPIRED = "cursor_expired"
      DRAIN_REQUIRED = "drain_required"
      FILE_EXISTS = "file_exists"
      FORBIDDEN = "forbidden"
      INTEGRITY = "integrity"
      INTERNAL = "internal"
      INVALID_ARGUMENT = "invalid_argument"
      LAUNCH_FAILED = "launch_failed"
      LIMIT_EXCEEDED = "limit_exceeded"
      LOCK_LOST = "lock_lost"
      LOCK_UNAVAILABLE = "lock_unavailable"
      METHOD_NOT_ALLOWED = "method_not_allowed"
      NOT_CONFIGURED = "not_configured"
      NOT_DURABLE = "not_durable"
      NOT_FOUND = "not_found"
      PLAN_LIMIT = "plan_limit"
      RULE_DATABASE = "rule_database"
      RULE_EXISTS = "rule_exists"
      SDK_UNSUPPORTED = "sdk_unsupported"
      STILL_OPEN = "still_open"
      TIMEOUT = "timeout"
      UNAUTHENTICATED = "unauthenticated"
      UNAVAILABLE = "unavailable"
      WRONG_SERVER = "wrong_server"
      ALL = [
        BUSY,
        CANCELLED,
        CHANGED,
        CLIENT_UNSUPPORTED,
        CONFLICT,
        CURSOR_EXPIRED,
        DRAIN_REQUIRED,
        FILE_EXISTS,
        FORBIDDEN,
        INTEGRITY,
        INTERNAL,
        INVALID_ARGUMENT,
        LAUNCH_FAILED,
        LIMIT_EXCEEDED,
        LOCK_LOST,
        LOCK_UNAVAILABLE,
        METHOD_NOT_ALLOWED,
        NOT_CONFIGURED,
        NOT_DURABLE,
        NOT_FOUND,
        PLAN_LIMIT,
        RULE_DATABASE,
        RULE_EXISTS,
        SDK_UNSUPPORTED,
        STILL_OPEN,
        TIMEOUT,
        UNAUTHENTICATED,
        UNAVAILABLE,
        WRONG_SERVER
      ].freeze
    end

    class BackupRule < Data.define(:id, :path, :name, :prefix, :watch, :maximum_local_databases, :offload_durability, :offload_rollback_journal, :failover)
      extend WireModel
      # path: the path or glob
      # name: the resolved remote name for a single file, or empty for a directory or glob
      # offload_durability: what maximum_local_databases waits for before it removes a copy
      WIRE_FIELDS = {
        id: [:string, true],
        path: [:string, true],
        name: [:string, true],
        prefix: [:string, true],
        watch: [:string, true],
        maximum_local_databases: [:integer, true],
        offload_durability: [:string, true],
        offload_rollback_journal: [:boolean, true],
        failover: [:boolean, true]
      }.freeze
    end

    class Error < Data.define(:code, :message, :rule)
      extend WireModel
      # rule: for rule_exists from backup-rules/start, the rule already saved for the path; stop it
      # by its ID before saving different settings
      WIRE_FIELDS = {
        code: [:string, true],
        message: [:string, true],
        rule: [[:model, :BackupRule], false]
      }.freeze
    end

    class DatabaseStatusList < Data.define(:next, :databases)
      extend WireModel
      # next: Pass this name as after to read the next page. Omitted on the last page.
      WIRE_FIELDS = {
        next: [:string, false],
        databases: [[:list, [:model, :DatabaseStatus]], true]
      }.freeze
    end

    class RestorableVersion < Data.define(:version, :state_digest, :time, :archived)
      extend WireModel
      # time: when the version was synced
      # archived: when the server recorded the primary bucket's object of this version as archived;
      # absent when the primary bucket does not hold the version, as for one synced between archive
      # passes, which restores only until history pruning removes its segment
      WIRE_FIELDS = {
        version: [:integer, true],
        state_digest: [:string, true],
        time: [:time, true],
        archived: [:time, false]
      }.freeze
    end

    class VersionList < Data.define(:versions, :next)
      extend WireModel
      # next: pass as after to read the next page
      WIRE_FIELDS = {
        versions: [[:list, [:model, :RestorableVersion]], true],
        next: [:integer, false]
      }.freeze
    end

    class RestoreRequest < Data.define(:name, :path, :version, :time, :umask)
      extend WireModel
      # path: absolute path of a new file in an existing directory
      # version: the version to restore; the head when neither version nor time is set
      # time: restore the newest version at or before this time
      # umask: the caller's file mode creation mask, such as 18 for 022; the file gets 0666 less it,
      # as one the caller created would. Absent uses the client's own umask. The litebackup command
      # sends its own.
      WIRE_FIELDS = {
        name: [:string, true],
        path: [:string, true],
        version: [:integer, false],
        time: [:time, false],
        umask: [:integer, false]
      }.freeze
    end

    class RestoreResult < Data.define(:name, :path, :version, :page_count, :page_size, :state_digest)
      extend WireModel
      WIRE_FIELDS = {
        name: [:string, true],
        path: [:string, true],
        version: [:integer, true],
        page_count: [:integer, true],
        page_size: [:integer, true],
        state_digest: [:string, true]
      }.freeze
    end

    class RevertRequest < Data.define(:name, :version, :time)
      extend WireModel
      # version: the version whose content the new head holds; set version or time
      # time: revert to the newest version at or before this time; set version or time
      WIRE_FIELDS = {
        name: [:string, true],
        version: [:integer, false],
        time: [:time, false]
      }.freeze
    end

    class RevertResult < Data.define(:database, :source_version, :version, :state_digest)
      extend WireModel
      # source_version: the version whose content the new head holds; it and every version after it
      # stay restorable
      # version: the new head, one past the head before the revert
      # state_digest: the new head's state digest, which is the source version's
      WIRE_FIELDS = {
        database: [[:model, :DatabaseStatus], true],
        source_version: [:integer, true],
        version: [:integer, true],
        state_digest: [:string, true]
      }.freeze
    end

    class NameRequest < Data.define(:name)
      extend WireModel
      WIRE_FIELDS = {
        name: [:string, true]
      }.freeze
    end

    class AcquireRequest < Data.define(:name, :path, :maintenance, :discard_local, :offload_on_release, :offload_durability, :machine_label, :timeout_millis, :lock_timeout_millis, :umask)
      extend WireModel
      # path: absolute path of the local file; defaults to the path this machine uses
      # maintenance: checkpoint and, past the freelist threshold, VACUUM on release
      # discard_local: replace a local file that diverged from the server head with the head,
      # discarding its writes the head lacks; without it such a file is refused with 409 conflict
      # and left untouched
      # offload_on_release: lets Litebackup delete the released copy once offload_durability holds
      # it
      # offload_durability: what an offload of the released copy waits for; server, the default,
      # once the server holds its last version, cloud once the bucket holds it too. A release with
      # offload sets it to the release's durability.
      # machine_label: the name this machine shows on the server and as the writer of this database
      # only
      # timeout_millis: bound on downloading the head
      # lock_timeout_millis: how long to wait for the write lock while another machine holds it,
      # trying again until it is released; 0 refuses at once with lock_unavailable. It adds to the
      # timeout.
      # umask: the caller's file mode creation mask, such as 18 for 022; a file the acquire creates,
      # the head downloaded where none was or the empty file an offload removed, gets 0666 less it,
      # as one the caller created would. A copy the head replaces keeps its bits. Absent uses the
      # client's own umask. The litebackup command sends its own.
      WIRE_FIELDS = {
        name: [:string, true],
        path: [:string, false],
        maintenance: [:boolean, false],
        discard_local: [:boolean, false],
        offload_on_release: [:boolean, false],
        offload_durability: [:string, false],
        machine_label: [:string, false],
        timeout_millis: [:integer, false],
        lock_timeout_millis: [:integer, false],
        umask: [:integer, false]
      }.freeze
    end

    class AcquireResult < Data.define(:database, :decision)
      extend WireModel
      DECISION_CREATED = "created"
      DECISION_MATCHED = "matched"
      DECISION_RESUMED = "resumed"
      DECISION_DOWNLOADED = "downloaded"
      DECISION_FAST_FORWARDED = "fast_forwarded"
      DECISION_REPLACED = "replaced"

      # decision: what the acquire did to the local file
      WIRE_FIELDS = {
        database: [[:model, :DatabaseStatus], true],
        decision: [:string, true]
      }.freeze
    end

    class ReleaseRequest < Data.define(:name, :durability, :offload, :timeout_millis)
      extend WireModel
      # durability: server returns once the final sync is acknowledged; cloud waits for the bucket
      # too
      # offload: remove the local file once durability holds the released version, the server with
      # server and the bucket with cloud; an offload refused as not_durable is left to a later
      # offload pass
      # timeout_millis: how long to wait for the database to close and for the chosen durability
      WIRE_FIELDS = {
        name: [:string, true],
        durability: [:string, false],
        offload: [:boolean, false],
        timeout_millis: [:integer, false]
      }.freeze
    end

    class ReleaseResult < Data.define(:database, :checkpointed, :vacuumed, :offloaded)
      extend WireModel
      WIRE_FIELDS = {
        database: [[:model, :DatabaseStatus], true],
        checkpointed: [:boolean, true],
        vacuumed: [:boolean, true],
        offloaded: [:boolean, true]
      }.freeze
    end

    class OffloadRequest < Data.define(:name, :durability)
      extend WireModel
      # durability: what the offload waits for before it removes the copy: server, the default, once
      # the server holds its synced version; cloud once the bucket holds it too. Until then it is
      # refused with not_durable.
      WIRE_FIELDS = {
        name: [:string, true],
        durability: [:string, false]
      }.freeze
    end

    class Writer < Data.define(:name, :active, :this_machine, :writer_machine_label, :acquired_time, :lease_expires, :lease_remaining_millis, :lapsed)
      extend WireModel
      # active: whether some machine holds the write lock and its lease still runs
      # this_machine: whether this machine's session holds the write lock
      # writer_machine_label: the machine label of the writer, or empty when nobody holds the lock
      # acquired_time: when the writer took the lock
      # lease_expires: on the server's clock
      # lease_remaining_millis: how long the lease still runs, negative once it ran out
      # lapsed: the writer's lease ran out, so another machine may take over
      WIRE_FIELDS = {
        name: [:string, true],
        active: [:boolean, true],
        this_machine: [:boolean, true],
        writer_machine_label: [:string, true],
        acquired_time: [:time, false],
        lease_expires: [:time, false],
        lease_remaining_millis: [:integer, false],
        lapsed: [:boolean, true]
      }.freeze
    end

    class BackupRequest < Data.define(:path, :name, :prefix, :watch, :maximum_local_databases, :offload_durability, :offload_rollback_journal)
      extend WireModel
      WATCH_INOTIFY = "inotify"
      WATCH_POLL = "poll"

      # path: absolute file, directory, or glob with optional ** segments
      # name: the database name for a single path; must already be a valid name
      # prefix: name prefix such as app/ (a trailing / is added when missing); names are the prefix
      # plus the relative file path
      # maximum_local_databases: when positive, offload the least recently changed idle databases
      # past this count once offload_durability holds them
      # offload_durability: what maximum_local_databases waits for before it removes a copy; server,
      # the default, once the server holds its last version, cloud once the bucket holds it too
      # offload_rollback_journal: lets maximum_local_databases offload rollback-journal databases
      # too, on the operator's word that nothing keeps them open while idle; WAL databases are
      # offloaded either way
      WIRE_FIELDS = {
        path: [:string, true],
        name: [:string, false],
        prefix: [:string, false],
        watch: [:string, false],
        maximum_local_databases: [:integer, false],
        offload_durability: [:string, false],
        offload_rollback_journal: [:boolean, false]
      }.freeze
    end

    class BackupList < Data.define(:rules)
      extend WireModel
      WIRE_FIELDS = {
        rules: [[:list, [:model, :BackupRule]], true]
      }.freeze
    end

    class StopBackupRequest < Data.define(:name, :rule_id)
      extend WireModel
      # name: Exact remote name of a database with its own backup rule on this machine.
      # rule_id: ID returned by start or listed by rules. Stops the whole rule, including future
      # matches.
      WIRE_FIELDS = {
        name: [:string, false],
        rule_id: [:string, false]
      }.freeze
    end

    class StopBackupResult < Data.define(:lease_pending)
      extend WireModel
      WIRE_FIELDS = {
        lease_pending: [:boolean, true]
      }.freeze
    end

    class FailoverRequest < Data.define(:path, :name, :timeout_millis, :discard_local)
      extend WireModel
      # path: absolute path of the database, the same on every machine
      # name: the database name; defaults to the relative file path
      # timeout_millis: bound on the first download of a replica
      # discard_local: replace a file at the path that differs from the server head with the head,
      # discarding its writes the head lacks; without it such a file is refused with 409 conflict
      # and left untouched
      WIRE_FIELDS = {
        path: [:string, true],
        name: [:string, false],
        timeout_millis: [:integer, false],
        discard_local: [:boolean, false]
      }.freeze
    end

    class Failover < Data.define(:name, :path, :role, :database, :writer)
      extend WireModel
      ROLE_ORIGIN = "origin"
      ROLE_REPLICA = "replica"

      WIRE_FIELDS = {
        name: [:string, true],
        path: [:string, true],
        role: [:string, true],
        database: [[:model, :DatabaseStatus], true],
        writer: [[:model, :Writer], true]
      }.freeze
    end

    # Either name and path, for one database, or prefix and directory, for every database under it.
    class ReplicaRequest < Data.define(:name, :path, :prefix, :directory, :write_protection, :timeout_millis)
      extend WireModel
      WRITE_PROTECTION_LOCK = "lock"
      WRITE_PROTECTION_DETECT = "detect"
      WRITE_PROTECTION_NONE = "none"

      # path: with name, the replica file, defaulting to the path this machine uses
      # prefix: name prefix such as app; replicates every database under it
      # directory: with prefix, the directory that receives <directory>/<name relative to the
      # prefix>
      # write_protection: lock (default) holds the SQLite write lock so app writes get SQLITE_BUSY
      # while readers carry on; detect rebases after a local write; none trusts apps to open with
      # mode=ro
      # timeout_millis: bound on downloading the first version of a single replica
      WIRE_FIELDS = {
        name: [:string, false],
        path: [:string, false],
        prefix: [:string, false],
        directory: [:string, false],
        write_protection: [:string, false],
        timeout_millis: [:integer, false]
      }.freeze
    end

    class PrefixReplica < Data.define(:id, :prefix, :directory, :write_protection, :database_count)
      extend WireModel
      # directory: the directory that receives <directory>/<name relative to the prefix>
      # database_count: how many databases under the prefix this machine replicates
      WIRE_FIELDS = {
        id: [:string, true],
        prefix: [:string, true],
        directory: [:string, true],
        write_protection: [:string, true],
        database_count: [:integer, true]
      }.freeze
    end

    # The replica for a name, or the prefix replica for a prefix.
    class ReplicaResult < Data.define(:database, :prefix_replica)
      extend WireModel
      WIRE_FIELDS = {
        database: [[:model, :DatabaseStatus], false],
        prefix_replica: [[:model, :PrefixReplica], false]
      }.freeze
    end

    class ReplicaList < Data.define(:prefixes, :replicas)
      extend WireModel
      WIRE_FIELDS = {
        prefixes: [[:list, [:model, :PrefixReplica]], true],
        replicas: [[:list, [:model, :DatabaseStatus]], true]
      }.freeze
    end

    # Either name for one replica, or prefix and directory for a prefix.
    class StopReplicaRequest < Data.define(:name, :prefix, :directory)
      extend WireModel
      # directory: with prefix, the directory the prefix replica was started with
      WIRE_FIELDS = {
        name: [:string, false],
        prefix: [:string, false],
        directory: [:string, false]
      }.freeze
    end

    class Empty < Data.define
      extend WireModel
      WIRE_FIELDS = {}.freeze
    end

    class EventList < Data.define(:events, :next)
      extend WireModel
      # next: pass as after to continue
      WIRE_FIELDS = {
        events: [[:list, [:model, :Event]], true],
        next: [:integer, true]
      }.freeze
    end

    class ShutdownRequest < Data.define(:durability, :timeout_millis)
      extend WireModel
      DURABILITY_NONE = "none"
      DURABILITY_SERVER = "server"
      DURABILITY_CLOUD = "cloud"

      WIRE_FIELDS = {
        durability: [:string, false],
        timeout_millis: [:integer, false]
      }.freeze
    end

    class DrainFailure < Data.define(:name, :code, :message)
      extend WireModel
      WIRE_FIELDS = {
        name: [:string, true],
        code: [:string, true],
        message: [:string, true]
      }.freeze
    end

    class ShutdownResult < Data.define(:released, :failed)
      extend WireModel
      # released: how many databases the drain synced as asked and released
      # failed: every database the drain could not sync, wait for, or release, each keeping any
      # write lock it holds, and every conflicted copy whose writes are not on the server, with
      # conflict; empty only when the drain released every database it covers, which is the one case
      # the daemon stops
      WIRE_FIELDS = {
        released: [:integer, true],
        failed: [[:list, [:model, :DrainFailure]], true]
      }.freeze
    end
  end
end
