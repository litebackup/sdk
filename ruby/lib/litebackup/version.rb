# frozen_string_literal: true

module Litebackup
  # Sent with every call. Daemons refuse SDKs older than their minimum with sdk_unsupported.
  VERSION = "1.4.0"
  # Connecting to an older daemon fails with daemon_unsupported.
  MINIMUM_DAEMON_VERSION = "1.1.0"
end
