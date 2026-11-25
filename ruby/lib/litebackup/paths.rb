# frozen_string_literal: true

require "json"
require "tmpdir"

module Litebackup
  # Records a litebackup install service install; root and the service's
  # user use its socket.
  SYSTEM_CONFIGURATION_PATH = "/etc/litebackup/client.json"

  # Session names and sockets, resolved exactly as the litebackup command does.
  module Paths
    DEFAULT_SESSION = "default"
    SESSION_PATTERN = /\A[a-z0-9][a-z0-9._-]*\z/

    module_function

    # The runtime directory and uid a service configuration file names, or nil.
    def runtime_directory(path)
      configuration = JSON.parse(File.read(path))
      return nil unless configuration.is_a?(Hash)

      directory = configuration["runtime_directory"]
      return nil unless directory.is_a?(String) && !directory.empty?

      uid = configuration["uid"]
      [directory, uid.is_a?(Integer) ? uid : nil]
    rescue SystemCallError, IOError, JSON::ParserError
      nil
    end

    # The session name and its socket path.
    #
    # Where litebackup install service ran as root, root and the service's
    # user use the service's socket; where a user installed a service of their
    # own, that user uses the directory it saved. Everyone else uses
    # $XDG_RUNTIME_DIR/litebackup/<session>/client.sock, or a per-user
    # directory under the temporary directory, which must be this user's alone.
    def session_socket(session, environment, system_configuration = SYSTEM_CONFIGURATION_PATH)
      name = [session, environment["LITEBACKUP_SESSION"], DEFAULT_SESSION].find { |value| Wire.supplied?(value) }
      unless SESSION_PATTERN.match?(name)
        raise Litebackup.invalid_argument(
          "session names use [a-z0-9._-] and start with a letter or digit, got #{name.inspect}"
        )
      end
      uid = Process.uid
      system = runtime_directory(system_configuration)
      return [name, File.join(system[0], name, "client.sock")] if system && (uid.zero? || uid == system[1])

      home = Wire.supplied?(environment["HOME"]) ? environment["HOME"] : Dir.home
      user = runtime_directory(File.join(home, ".config", "litebackup", "service.json"))
      return [name, File.join(user[0], name, "client.sock")] if user

      runtime_root = environment["XDG_RUNTIME_DIR"]
      unless Wire.supplied?(runtime_root)
        temporary = Wire.supplied?(environment["TMPDIR"]) ? environment["TMPDIR"] : Dir.tmpdir
        runtime_root = File.join(temporary, "litebackup-#{uid}")
        private_runtime_directory(runtime_root, uid)
      end
      [name, File.join(runtime_root, "litebackup", name, "client.sock")]
    end

    # Makes sure the per-user runtime directory used without $XDG_RUNTIME_DIR
    # is this user's own. The temporary directory is shared, so another local
    # user could create it first and answer as the daemon. It is created with
    # mode 0700 when missing; one that is a symlink, is not a directory,
    # belongs to another user, or is open to anyone else is refused, as the
    # litebackup command refuses it.
    def private_runtime_directory(directory, uid)
      begin
        Dir.mkdir(directory, 0o700)
      rescue Errno::EEXIST
        nil
      rescue SystemCallError => e
        raise Litebackup.invalid_argument("creating #{directory} failed: #{e.message}")
      end
      status = File.lstat(directory)
      return if status.directory? && status.uid == uid && status.mode.nobits?(0o077)

      raise Litebackup.invalid_argument(
        "#{directory} is not a directory of this user's alone; " \
        "remove it or set XDG_RUNTIME_DIR to this user's own directory"
      )
    end
  end
end
