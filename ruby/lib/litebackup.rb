# frozen_string_literal: true

# Back up, replicate, and restore SQLite databases with Litebackup.
#
# The client talks to the litebackup daemon of a session over its unix socket
# and starts it when it is not running, with the credential in
# LITEBACKUP_API_KEY:
#
#   require "litebackup"
#
#   Litebackup::Client.open do |litebackup|
#     litebackup.backups.start("/var/lib/app/main.sqlite")
#   end
#
# The sqlite3 gem's pool adapter is in litebackup/integrations/sqlite3, and
# the Rails integration in litebackup/integrations/rails.

require "fileutils"
require_relative "litebackup/version"
require_relative "litebackup/generated/error_codes"
require_relative "litebackup/wire_model"
require_relative "litebackup/generated/models"
require_relative "litebackup/errors"
require_relative "litebackup/umask"
require_relative "litebackup/wire"
require_relative "litebackup/credential"
require_relative "litebackup/paths"
require_relative "litebackup/binary"
require_relative "litebackup/launch"
require_relative "litebackup/transport"
require_relative "litebackup/foreground"
require_relative "litebackup/pool"
require_relative "litebackup/failover"
require_relative "litebackup/client"
require_relative "litebackup/release"

module Litebackup
  # The socket API's types that the client's methods return, under the names
  # every SDK exports.
  EXPORTED_MODELS = %i[
    AcquireResult ArchivedVersion BackupRule DatabaseStatus DatabaseStatusList DrainFailure Event EventList
    PrefixReplica ReleaseResult ReplicaList RestorableVersion RestoreResult RevertResult ShutdownResult Status
    StopBackupResult SyncedState VersionList Writer
  ].freeze
  EXPORTED_MODELS.each { |model_name| const_set(model_name, Models.const_get(model_name)) }

  # The litebackup binary a client starts the daemon with; see Binary.litebackup_binary.
  def self.binary(environment = ENV, binary_path = nil)
    Binary.litebackup_binary(environment, binary_path)
  end

  # The one line that installs the litebackup command.
  def self.install_command
    Binary.install_command
  end

  # The session name and socket path a client uses.
  def self.session_socket(session = nil, environment = ENV)
    Paths.session_socket(session, environment.to_h)
  end
end
