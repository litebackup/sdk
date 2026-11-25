# frozen_string_literal: true

module Litebackup
  BINARY_PATH_ENVIRONMENT = "LITEBACKUP_PATH"

  # Finding the litebackup binary. Nothing is downloaded at runtime.
  module Binary
    # Where a platform gem carries its binary. The gemspec lists it as a
    # file, not an executable, so gem install writes no wrapper for it.
    BUNDLED_PATH = File.join(__dir__, "bin", "litebackup")

    module_function

    # The one line that installs the litebackup command on Linux and macOS for whoever runs it.
    def install_command
      "curl -fsSL https://litebackup.com/cli.sh | bash"
    end

    def executable?(path)
      File.file?(path) && File.executable?(path)
    end

    # The binary this platform's gem carries, or nil for the plain gem.
    def bundled_binary
      executable?(BUNDLED_PATH) ? BUNDLED_PATH : nil
    end

    # Finds the litebackup binary: the explicit path, then LITEBACKUP_PATH,
    # then the binary the gem bundles, then litebackup on PATH, then
    # ~/.local/bin/litebackup. Raises binary_not_found with the install
    # command when there is none, or when the explicit path or LITEBACKUP_PATH
    # names no file.
    def litebackup_binary(environment = ENV, binary_path = nil)
      override = binary_path.to_s.strip
      override = environment[BINARY_PATH_ENVIRONMENT].to_s.strip if override.empty?
      unless override.empty?
        # An explicit path that names no file never falls back to another
        # binary; a file that cannot run fails later, as launch_failed.
        if missing_path?(override)
          raise Error.new(
            ErrorCode::BINARY_NOT_FOUND,
            "the litebackup binary #{override} does not exist; " \
            "set binary_path or #{BINARY_PATH_ENVIRONMENT} to it, or install it with: #{install_command}"
          )
        end
        return override
      end
      bundled = bundled_binary
      return bundled unless bundled.nil?

      found = path_binary(environment["PATH"].to_s)
      return found unless found.nil?

      home = Wire.supplied?(environment["HOME"]) ? environment["HOME"] : Dir.home
      local = File.join(home, ".local", "bin", "litebackup")
      return local if executable?(local)

      raise Error.new(
        ErrorCode::BINARY_NOT_FOUND,
        "the litebackup command is not installed; set #{BINARY_PATH_ENVIRONMENT} or install it with: #{install_command}"
      )
    end

    # The first executable litebackup in a PATH value.
    def path_binary(search_path)
      search_path.split(File::PATH_SEPARATOR).reject(&:empty?).each do |directory|
        candidate = File.join(directory, "litebackup")
        return candidate if executable?(candidate)
      end
      nil
    end

    # Whether nothing is at path: it, or a directory on its way, does not
    # exist. A path behind a directory this user may not enter is not missing.
    def missing_path?(path)
      File.stat(path)
      false
    rescue Errno::ENOENT, Errno::ENOTDIR
      true
    rescue SystemCallError
      false
    end

    # Why starting binary failed. A file that is there but fails to start as
    # missing cannot run: the interpreter its #! line names, or its dynamic
    # loader, is not on this machine. The message names that line rather than
    # repeating the file's path as if it were missing.
    def start_failure_message(binary, error)
      not_found = error.is_a?(Errno::ENOENT) || error.is_a?(Errno::ENOTDIR)
      return "starting #{binary} failed: #{error.message}" if !not_found || missing_path?(binary)

      interpreter_line = script_interpreter_line(binary)
      unusable_binary = "starting #{binary} failed: it cannot run because"
      return "#{unusable_binary} its interpreter or dynamic loader is not on this machine" if interpreter_line.empty?

      "#{unusable_binary} the interpreter its first line names is not on this machine: #{interpreter_line}"
    end

    # The #! line that starts the file at path, or "" when it starts with none or cannot be read.
    def script_interpreter_line(path)
      start = File.binread(path, 256) || ""
      first_line = start.split("\n", 2).first.to_s.force_encoding(Encoding::UTF_8).scrub
      first_line.start_with?("#!") ? first_line.strip : ""
    rescue SystemCallError, IOError
      ""
    end
  end
end
