# frozen_string_literal: true

require "json"
require "socket"

module Litebackup
  # Starting a session's daemon with litebackup launch, as the CLI does. The
  # launch command locks the session's launch lock, so concurrent processes
  # start one daemon. Launches are never retried.
  module Launch
    LAUNCH_TIMEOUT_SECONDS = 30.0
    # How long a finished command's output pipes may stay open, in a process
    # it started, before reading stops.
    DRAIN_SECONDS = 1.0

    module_function

    # Whether something accepts connections on the socket.
    def socket_listening?(socket_path)
      socket = Socket.new(Socket::AF_UNIX, Socket::SOCK_STREAM)
      address = Socket.pack_sockaddr_un(socket_path)
      begin
        socket.connect_nonblock(address)
      rescue IO::WaitWritable
        return false if socket.wait_writable(1.0).nil?

        begin
          socket.connect_nonblock(address)
        rescue Errno::EISCONN
          nil
        end
      end
      true
    rescue SystemCallError, ArgumentError
      false
    ensure
      socket&.close
    end

    # The launch command line and its environment.
    def launch_command(session, environment, api_key, binary_path)
      binary = Binary.litebackup_binary(environment, binary_path)
      launch_environment = environment.merge(BINARY_PATH_ENVIRONMENT => binary)
      launch_environment[API_KEY_ENVIRONMENT] = api_key unless api_key.nil?
      [[binary, "launch", "--session", session, "--stdout-format", "json"], launch_environment]
    end

    # The code and message of the last error the launch command reported as
    # JSON: drain_required when the launch refused a new credential for a
    # client that has write locks, and launch_failed otherwise, also with the
    # whole output when the command reported no error as JSON.
    def launch_denial(output)
      denial = ""
      denial_code = ErrorCode::LAUNCH_FAILED
      output.each_line do |line|
        diagnostic = begin
          JSON.parse(line)
        rescue JSON::ParserError
          next
        end
        # A warning carries an event; an error does not.
        next unless diagnostic.is_a?(Hash) && !diagnostic.key?("event") && diagnostic["code"].is_a?(String)

        message = diagnostic["message"]
        next unless message.is_a?(String) && !message.empty?

        denial = message
        denial_code = diagnostic["code"] == ErrorCode::DRAIN_REQUIRED ? ErrorCode::DRAIN_REQUIRED : ErrorCode::LAUNCH_FAILED
      end
      return [ErrorCode::LAUNCH_FAILED, output.strip] if denial.empty?

      [denial_code, denial]
    end

    def launch_failed(output, cause)
      code, denial = launch_denial(output)
      Error.new(code, "starting Litebackup failed: #{denial.empty? ? cause : denial}")
    end

    # Starts the session daemon when nothing listens on its socket.
    def launch_daemon(session, socket_path, environment, api_key, binary_path)
      return if socket_listening?(socket_path)

      command, launch_environment = launch_command(session, environment, api_key, binary_path)
      stdout, stderr, status = capture_output(launch_environment, command, LAUNCH_TIMEOUT_SECONDS)
      raise launch_failed("", "litebackup launch did not finish within #{LAUNCH_TIMEOUT_SECONDS.to_i} s") if status.nil?
      return if status.success?

      raise launch_failed(stderr.empty? ? stdout : stderr, "exit status #{status.exitstatus || status.termsig}")
    end

    # Starts a command with exactly environment and returns its output and
    # exit status; the status is nil when the command was killed after
    # timeout_seconds. Raises launch_failed when the command cannot start.
    def capture_output(environment, command, timeout_seconds)
      stdout_reader, stdout_writer = IO.pipe
      stderr_reader, stderr_writer = IO.pipe
      begin
        pid = Process.spawn(Launch.exact_environment(environment), *command, in: File::NULL, out: stdout_writer,
                                                                             err: stderr_writer)
      rescue SystemCallError => e
        [stdout_reader, stderr_reader].each(&:close)
        raise Error.new(ErrorCode::LAUNCH_FAILED, Binary.start_failure_message(command.first, e))
      ensure
        [stdout_writer, stderr_writer].each(&:close)
      end
      readers = [stdout_reader, stderr_reader].map { |pipe| Thread.new { read_pipe(pipe) } }
      waiter = Process.detach(pid)
      if waiter.join(timeout_seconds).nil?
        begin
          Process.kill(:KILL, pid)
        rescue Errno::ESRCH
          nil
        end
        waiter.join
        status = nil
      else
        status = waiter.value
      end
      outputs = readers.map { |reader| reader.join(DRAIN_SECONDS) ? reader.value.to_s : "" }
      [stdout_reader, stderr_reader].each(&:close)
      readers.each { |reader| reader.join if reader.alive? }
      [outputs[0].force_encoding(Encoding::UTF_8).scrub, outputs[1].force_encoding(Encoding::UTF_8).scrub, status]
    end

    # A pipe's whole text, or what arrived before another thread closed it.
    def read_pipe(pipe)
      text = +""
      loop { text << pipe.readpartial(65_536) }
    rescue IOError
      text
    end

    # The environment Process.spawn needs so a command gets environment and
    # nothing else: every variable of this process that environment lacks is
    # unset.
    def exact_environment(environment)
      ENV.keys.to_h { |name| [name, nil] }.merge(environment)
    end

    # The environment a client resolves paths and starts the daemon with: ENV by default.
    def process_environment(environment)
      (environment.nil? ? ENV.to_h : environment.to_h).transform_keys(&:to_s)
    end
  end
end
