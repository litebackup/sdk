# frozen_string_literal: true

require "json"

module Litebackup
  # backups.once: one foreground litebackup backup job, as the CLI runs it.
  module Foreground
    # How many of the job's file diagnostics a failed backup's message lists.
    MAXIMUM_FILE_DIAGNOSTICS = 20
    # How long a job has to exit after SIGTERM before it gets SIGKILL.
    STOP_GRACE_SECONDS = 15.0
    # How long a job's error output may stay open after the job exited, in a
    # process it started, before reading stops.
    DRAIN_SECONDS = 1.0
    # How often the wait for a job's exit checks it.
    POLL_SECONDS = 0.05

    class << self
      # How long a stopped job has before SIGKILL, and how long past its
      # timeout a job runs before it is killed; tests shorten them.
      attr_accessor :stop_grace_seconds, :deadline_grace_seconds
    end
    self.stop_grace_seconds = STOP_GRACE_SECONDS
    self.deadline_grace_seconds = Wire::DEADLINE_GRACE_SECONDS

    module_function

    # The litebackup command line of one foreground job.
    def backup_command(binary, source, session:, name:, prefix:, durability:, timeout:)
      raise Litebackup.invalid_argument("backup needs a path") unless Wire.supplied?(source)
      raise Litebackup.invalid_argument("choose a name or a prefix") if !name.nil? && !prefix.nil?
      raise Litebackup.invalid_argument("expected an exact name") if !name.nil? && (name.empty? || name.end_with?("/"))

      Wire.check_durability(durability)
      command = [binary, "backup", "--stdout-format", "json", "--durability", durability]
      command += ["--session", session] if Wire.supplied?(session)
      timeout_seconds = Wire.seconds(timeout, "the backup timeout")
      command += ["--timeout", "#{(timeout_seconds * 1000).round}ms"] unless timeout_seconds.nil?
      command += ["--", Wire.absolute_path(source)]
      command << name unless name.nil?
      command << (prefix.end_with?("/") ? prefix : "#{prefix}/") if Wire.supplied?(prefix)
      command
    end

    # The job's structured error on this line of its error output, or nil for a log line.
    def reported_failure(line)
      parsed = JSON.parse(line)
      parsed.is_a?(Hash) && parsed["code"].is_a?(String) && parsed["message"].is_a?(String) ? parsed : nil
    rescue JSON::ParserError
      nil
    end

    # The job's final structured report, followed by each file diagnostic it
    # printed before it, or its text. Each file diagnostic names the file, and
    # the machine that writes it for a busy one.
    def backup_failure(stderr, exit_status)
      final_report = nil
      diagnostics = []
      stderr.strip.each_line(chomp: true) do |line|
        failure = reported_failure(line)
        next if failure.nil?

        final_report = failure
        # A file reported twice is listed once, so the limit counts distinct files.
        if diagnostics.length < MAXIMUM_FILE_DIAGNOSTICS && !diagnostics.include?(failure["message"])
          diagnostics << failure["message"]
        end
      end
      if final_report.nil?
        text = stderr.strip
        return Error.new("backup_failed",
                         text.empty? ? "foreground backup failed with exit status #{exit_status}" : text)
      end

      message = final_report["message"]
      listed = [message]
      diagnostics.each do |diagnostic|
        next if listed.include?(diagnostic)

        listed << diagnostic
        message += "\n#{diagnostic}"
      end
      Error.new(final_report["code"], message)
    end

    # binary_not_found when the binary is missing, launch_failed when it
    # cannot run. ENOENT while the file is there means its script interpreter
    # is missing: the file cannot run.
    def start_failure(binary, error)
      not_found = error.is_a?(Errno::ENOENT) || error.is_a?(Errno::ENOTDIR)
      code = not_found && Binary.missing_path?(binary) ? ErrorCode::BINARY_NOT_FOUND : ErrorCode::LAUNCH_FAILED
      Error.new(code, Binary.start_failure_message(binary, error))
    end

    # A running job: its operating-system process and the thread reading its error output.
    class Job
      attr_reader :pid

      def initialize(pid, error_output)
        @pid = pid
        @error_output = error_output
        @text = +""
        @reader = Thread.new { read_error_output }
        @waiter = Process.detach(pid)
      end

      def exited?
        !@waiter.alive?
      end

      def exit_status
        @waiter.value
      end

      def signal(signal_name)
        Process.kill(signal_name, pid)
      rescue Errno::ESRCH
        nil
      end

      # Stops a running job: SIGTERM, then SIGKILL when it has not exited within STOP_GRACE_SECONDS.
      def stop
        signal(:TERM)
        return unless @waiter.join(Foreground.stop_grace_seconds).nil?

        signal(:KILL)
        @waiter.join
      end

      # Waits for the job to exit, killing it once deadline_seconds pass.
      # Returns its error output and whether the deadline ended it. Reading
      # stops DRAIN_SECONDS after the job exited even if a process it started
      # keeps the output open.
      def await(deadline_seconds)
        timed_out = false
        if @waiter.join(deadline_seconds).nil?
          timed_out = true
          signal(:KILL)
          @waiter.join
        end
        if @reader.join(DRAIN_SECONDS).nil?
          @error_output.close
          @reader.join
        end
        [@text.dup.force_encoding(Encoding::UTF_8).scrub, timed_out]
      end

      private

      def read_error_output
        loop { @text << @error_output.readpartial(65_536) }
      rescue IOError
        nil
      ensure
        @error_output.close unless @error_output.closed?
      end
    end

    # Starts a job, with its standard output discarded and its error output read.
    def start_job(command, environment)
      error_reader, error_writer = IO.pipe
      begin
        pid = Process.spawn(Launch.exact_environment(environment), *command, in: File::NULL, out: File::NULL,
                                                                             err: error_writer)
      rescue SystemCallError => e
        error_reader.close
        raise start_failure(command.first, e)
      ensure
        error_writer.close
      end
      Job.new(pid, error_reader)
    end

    # How long to wait for a job: its timeout plus the grace that lets the job
    # report its own timeout first.
    def job_deadline(timeout)
      timeout_seconds = Wire.seconds(timeout, "the backup timeout")
      timeout_seconds.nil? ? nil : timeout_seconds + Foreground.deadline_grace_seconds
    end

    def timed_out_backup
      Error.new(ErrorCode::TIMEOUT, "the foreground backup did not finish within its timeout")
    end
  end
end
