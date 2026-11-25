# frozen_string_literal: true

module Litebackup
  # Talks to the Litebackup daemon of one session over its unix socket,
  # starting it like the CLI does. It connects on first use. Pass a block to
  # Client.open, or call close, which never shuts the daemon down:
  #
  #   Litebackup::Client.open do |litebackup|
  #     litebackup.backups.start("/var/lib/app/main.sqlite")
  #   end
  #
  # A client is safe to share between threads. One made before a fork is
  # refused in the child with invalid_argument; make a new one there, as
  # Puma's workers do after boot.
  class Client
    # What a client was created with, resolved once.
    class Settings
      attr_reader :environment, :api_key, :session, :socket_path, :binary_path, :launch

      def initialize(api_key:, session:, socket_path:, binary_path:, launch:, environment:)
        @environment = Launch.process_environment(environment)
        @api_key = api_key.nil? ? @environment[API_KEY_ENVIRONMENT] : api_key
        Credential.credential_type(@api_key)
        raise Litebackup.invalid_argument("socket_path needs a path") if socket_path == ""

        @explicit_socket = !socket_path.nil?
        @binary_path = binary_path
        @launch = launch
        if @explicit_socket
          @session = session
          @socket_path = Wire.path_text(socket_path)
        else
          @session, @socket_path = Paths.session_socket(session, @environment)
        end
      end

      def explicit_socket?
        @explicit_socket
      end

      def foreground_command(source, name:, prefix:, durability:, timeout:)
        if explicit_socket?
          raise Litebackup.invalid_argument("foreground backup needs a named session and cannot target socket_path")
        end

        binary = Binary.litebackup_binary(environment, binary_path)
        Foreground.backup_command(binary, source, session: session, name: name, prefix: prefix,
                                                  durability: durability, timeout: timeout)
      end

      def foreground_environment
        api_key.nil? ? environment : environment.merge(API_KEY_ENVIRONMENT => api_key)
      end
    end

    attr_reader :backups, :rules, :databases, :replicas, :events, :session, :failovers

    # api_key: the credential the daemon uses when it starts: an API key, or a
    #   join token it exchanges once for a key of this machine, then @ and the
    #   server's address; LITEBACKUP_API_KEY by default, and "" for none. The
    #   daemon keeps the key it saved unless this holds a different credential,
    #   which the launch refuses with drain_required while the client has write
    #   locks; a daemon already running keeps its key until it restarts.
    #   Foreground backups use it too.
    # session: the session name; LITEBACKUP_SESSION, then "default".
    # socket_path: connect to this socket and never start a daemon.
    # binary_path: the litebackup binary that starts the daemon; see Litebackup.binary.
    # launch: start the session daemon when it is not running.
    # environment: the environment for resolving paths and starting the daemon; ENV by default.
    def initialize(api_key: nil, session: nil, socket_path: nil, binary_path: nil, launch: true, environment: nil)
      @settings = Settings.new(api_key: api_key, session: session, socket_path: socket_path,
                               binary_path: binary_path, launch: launch, environment: environment)
      @transport = Transport.new(@settings.socket_path)
      @owner_pid = Process.pid
      @connect_lock = Mutex.new
      @connected = false
      @closed = false
      @lock = Mutex.new
      @failovers_followed = []
      @jobs = []
      @client_version = ""
      @backups = Backups.new(self)
      @rules = Rules.new(self)
      @databases = Databases.new(self)
      @replicas = Replicas.new(self)
      @events = Events.new(self)
      @session = Session.new(self)
      @failovers = Failovers.new(self)
    end

    # A client for the block, closed when the block ends; without a block, the client.
    def self.open(**, &block)
      client = new(**)
      return client if block.nil?

      begin
        block.call(client)
      ensure
        client.close
      end
    end

    # The daemon's version, from its last answer.
    attr_reader :client_version

    def socket_path
      @settings.socket_path
    end

    # The session name, or nil for a client of an explicit socket path.
    def session_name
      @settings.session
    end

    def closed?
      @closed
    end

    # Connects, starting the daemon when needed; fails with daemon_unsupported
    # for a daemon older than 1.1.0.
    def connect
      ensure_open
      @connect_lock.synchronize do
        return if @connected

        settings = @settings
        if !settings.explicit_socket? && settings.launch && settings.session
          Launch.launch_daemon(settings.session, settings.socket_path, settings.environment,
                               settings.api_key, settings.binary_path)
        end
        send_call(Wire.connect_probe)
        Wire.check_daemon_version(@client_version, settings.session)
        @connected = true
      end
    end

    # Stops following failovers, ends running foreground backups, and closes
    # connections. The daemon keeps running, and so do its failovers and
    # acquired databases.
    def close
      failovers, jobs = @lock.synchronize do
        @closed = true
        [@failovers_followed.dup, @jobs.dup]
      end
      # A forked child has neither the followers nor the jobs, and its copies
      # of the parent's connections are closed without ending the parent's calls.
      return @transport.close unless Process.pid == @owner_pid

      # Closing the transport first ends the calls that wait, the followers'
      # long-polls and any call a failover callback waits on included, so
      # stopping a follower never waits on a callback that waits on this client.
      @transport.close
      failovers.each(&:stop_following)
      jobs.each(&:stop)
      failovers.each(&:join_follower)
      nil
    end

    # Raises closed after close, and invalid_argument in a forked child.
    def ensure_open
      raise Error.new(ErrorCode::CLOSED, "the client is closed") if @closed
      return if Process.pid == @owner_pid

      raise Litebackup.invalid_argument(
        "this client was made in process #{@owner_pid} and cannot be used after a fork; " \
        "make a new client in this process"
      )
    end

    # Sends a call without connecting first.
    def send_call(call)
      answer = @transport.request(call.http_method, call.target, call.encoded_body, call.deadline_seconds)
      @client_version = answer.client_version
      Wire.decode_answer(call, answer.status, answer.body)
    end

    # Connects, then sends a call.
    def call(call)
      connect
      send_call(call)
    end

    # The settings it was made with; for the groups.
    attr_reader :settings

    def add_failover(failover)
      @lock.synchronize { @failovers_followed << failover }
    end

    def remove_failover(failover)
      @lock.synchronize { @failovers_followed.delete(failover) }
    end

    # Registers a running job so close stops it; false once the client closed.
    def add_job(job)
      @lock.synchronize do
        @jobs << job
        !@closed
      end
    end

    def remove_job(job)
      @lock.synchronize { @jobs.delete(job) }
    end
  end

  # The base of the client's groups.
  class Group
    def initialize(client)
      @client = client
    end
  end

  # Continuous backups and one-shot backup jobs.
  class Backups < Group
    # Saves a continuous backup rule for a file, directory, or glob; returns
    # once the daemon saved it. Starting the same path again with the same
    # settings returns the saved rule, so call it on every start. Different
    # settings for a saved path raise rule_exists with the saved rule in
    # details["rule"].
    def start(source, name: nil, prefix: nil, maximum_local_databases: nil,
              offload_durability: Durability::SERVER, offload_rollback_journal: false)
      @client.call(Wire.start_backup(source, name: name, prefix: prefix,
                                             maximum_local_databases: maximum_local_databases,
                                             offload_durability: offload_durability,
                                             offload_rollback_journal: offload_rollback_journal))
    end

    # Runs one foreground backup job and returns once it exits, after the
    # chosen destination acknowledged it. With a timeout, a job still running
    # 30 seconds after it ran out is killed and raises timeout. close ends a
    # running job, which then raises closed.
    def once(source, name: nil, prefix: nil, durability: Durability::SERVER, timeout: nil)
      @client.ensure_open
      settings = @client.settings
      command = settings.foreground_command(source, name: name, prefix: prefix, durability: durability,
                                                    timeout: timeout)
      deadline = Foreground.job_deadline(timeout)
      job = Foreground.start_job(command, settings.foreground_environment)
      begin
        job.stop unless @client.add_job(job)
        begin
          stderr, timed_out = job.await(deadline)
        rescue Exception # rubocop:disable Lint/RescueException
          # An interrupt, such as Ctrl-C or Timeout, never leaves the job running.
          job.signal(:KILL)
          job.await(nil)
          raise
        end
      ensure
        @client.remove_job(job)
      end
      raise Foreground.timed_out_backup if timed_out

      status = job.exit_status
      return nil if status.success?
      raise Error.new(ErrorCode::CLOSED, "the client is closed") if @client.closed?

      raise Foreground.backup_failure(stderr, status.exitstatus || status.termsig)
    end

    # Stops the single-file backup with this exact remote name on this machine.
    def stop(name)
      @client.call(Wire.stop_backup(name))
    end
  end

  # Saved backup and failover rules.
  class Rules < Group
    def list
      @client.call(Wire.list_rules)
    end

    # Stops a whole rule, including future matches.
    def stop(rule_id)
      @client.call(Wire.stop_rule(rule_id))
    end
  end

  # Databases on this machine and on the server.
  class Databases < Group
    def get(name)
      @client.call(Wire.database(name))
    end

    # One page; pass its next as after to continue.
    def page(after: "", limit: 100)
      @client.call(Wire.databases_page(after: after, limit: limit))
    end

    # Every database on this machine, a page at a time: an Enumerator
    # without a block.
    def all(limit: 100, &block)
      return to_enum(:all, limit: limit) if block.nil?

      after = ""
      loop do
        listing = page(after: after, limit: limit)
        listing.databases.each(&block)
        return nil if listing.next.nil? || listing.next.empty?

        after = listing.next
      end
    end

    def list
      all.to_a
    end

    def versions_page(name, after: 0, limit: nil)
      @client.call(Wire.versions_page(name, after: after, limit: limit))
    end

    # The versions the server restores, oldest first; at most limit when set.
    # An Enumerator without a block.
    def all_versions(name, after: 0, limit: nil, &block)
      return to_enum(:all_versions, name, after: after, limit: limit) if block.nil?
      return nil if limit == 0

      listed = 0
      loop do
        listing = versions_page(name, after: after, limit: limit.nil? ? nil : limit - listed)
        listing.versions.each do |version|
          block.call(version)
          listed += 1
          return nil if !limit.nil? && listed >= limit
        end
        return nil if listing.next.nil? || listing.next.zero? || listing.next <= after || listing.versions.empty?

        after = listing.next
      end
    end

    def versions(name, after: 0, limit: nil)
      all_versions(name, after: after, limit: limit).to_a
    end

    # Writes a version to a new file and checks it. An existing file is never
    # overwritten (file_exists). The file gets 0666 less umask, this
    # process's umask when nil.
    def restore(name, path:, version: nil, time: nil, umask: nil)
      @client.call(Wire.restore(name, path: path, version: version, time: time, umask: umask))
    end

    # Rolls a database back under its own name: a past version's content
    # becomes a new head, one past the current head. Pass version or time, one
    # of them; every later version stays restorable. The database must be one
    # this machine acquired or backs up, and its file closed (still_open
    # otherwise); a replica or a failover standby is refused
    # (invalid_argument), and a file holding writes the server lacks is
    # refused with conflict and left untouched.
    def revert(name, version: nil, time: nil)
      @client.call(Wire.revert(name, version: version, time: time))
    end

    def sync(name)
      @client.call(Wire.sync(name))
    end

    # Removes the local file of a released database once the server has its
    # last version, or the bucket with "cloud".
    def offload(name, durability: nil)
      @client.call(Wire.offload(name, durability: durability))
    end

    def writer(name)
      @client.call(Wire.writer(name))
    end

    # Locks the database for this machine and makes the local file the
    # server's head. A pool's lease also opens, keeps, and releases it. A
    # local file that diverged from the head is refused with conflict and
    # left untouched; discard_local replaces it with the head instead. While
    # another machine has the write lock, the daemon asks again for up to
    # lock_timeout; without it the acquire fails at once with lock_unavailable.
    def acquire(name, path: nil, maintenance: false, offload_on_release: false, offload_durability: Durability::SERVER,
                machine_label: nil, timeout: nil, lock_timeout: nil, umask: nil, discard_local: false)
      @client.call(Wire.acquire(name, path: path, maintenance: maintenance, offload_on_release: offload_on_release,
                                      offload_durability: offload_durability, machine_label: machine_label,
                                      timeout: timeout, lock_timeout: lock_timeout, umask: umask,
                                      discard_local: discard_local))
    end

    # Syncs a final time and releases the write lock. Close every connection
    # first, or it fails with still_open.
    def release(name, durability: nil, offload: false, timeout: nil)
      @client.call(Wire.release(name, durability: durability, offload: offload, timeout: timeout))
    end

    # A pool that keeps databases open for calls, opened and closed with
    # adapter. With a block, the pool is closed when the block ends:
    #
    #   litebackup.databases.pool(Litebackup::Integrations::SQLite3.adapter) do |pool|
    #     pool.lease("agents/42.sqlite", path: path) do |db|
    #       db.execute("INSERT INTO steps (body) VALUES (?)", [step])
    #     end
    #   end
    #
    # See Pool for idle_close, idle_release, cleanup_timeout, machine_label,
    # and on_release_error.
    def pool(adapter, idle_close: nil, idle_release: nil, cleanup_timeout: nil, machine_label: nil,
             on_release_error: nil, &block)
      settings = Pool::Settings.build(adapter, idle_close: idle_close, idle_release: idle_release,
                                               cleanup_timeout: cleanup_timeout, machine_label: machine_label,
                                               on_release_error: on_release_error)
      pool = Pool.new(@client, adapter, settings)
      return pool if block.nil?

      begin
        block.call(pool)
      ensure
        pool.close
      end
    end
  end

  # Live readonly replicas on this machine.
  class Replicas < Group
    # Keeps a live readonly replica and returns once its first version is local.
    def start(name, path: nil, write_protection: nil, timeout: nil)
      @client.call(Wire.start_replica(name, path: path, write_protection: write_protection, timeout: timeout))
    end

    # Replicates current and future databases under a prefix into a
    # directory; downloads go on in the background.
    def start_prefix(prefix, directory:, write_protection: nil)
      @client.call(Wire.start_prefix_replica(prefix, directory: directory, write_protection: write_protection))
    end

    def list
      @client.call(Wire.list_replicas)
    end

    def stop(name)
      @client.call(Wire.stop_replica(name))
    end

    def stop_prefix(prefix, directory:)
      @client.call(Wire.stop_prefix_replica(prefix, directory: directory))
    end
  end

  # The daemon's event log.
  class Events < Group
    # One page after an event ID; -1 returns none and, as next, where the log ends.
    def page(after: 0, wait: nil, limit: nil)
      @client.call(Wire.events_page(after: after, wait: wait, limit: limit))
    end

    # Yields events, oldest first, by long-polling until the caller stops:
    # an Enumerator without a block. A failed long-poll is retried with
    # backoff from the same cursor, and on_error hears each failure;
    # sdk_unsupported, closed, cancelled, invalid_argument, and cursor_expired
    # (the cursor is older than the oldest event kept; choose a new one) end
    # the watch by raising.
    def watch(after: 0, wait: nil, on_error: nil, &block)
      return to_enum(:watch, after: after, wait: wait, on_error: on_error) if block.nil?

      wait_seconds = Wire.seconds(wait, "the event wait") || Wire::EVENT_WAIT_SECONDS
      delay = Wire::FIRST_RETRY_SECONDS
      loop do
        begin
          listing = page(after: after, wait: wait_seconds)
        rescue Error => e
          raise if Wire.terminal_watch_error?(e)

          on_error&.call(e)
          sleep(delay)
          delay = [delay * 2, Wire::MAXIMUM_RETRY_SECONDS].min
          next
        end
        delay = Wire::FIRST_RETRY_SECONDS
        listing.events.each do |event|
          after = [after, event.id].max
          block.call(event)
        end
        after = [after, listing.next].max
      end
    end
  end

  # The session and its daemon.
  class Session < Group
    def status(after: "", limit: 100)
      @client.call(Wire.status(after: after, limit: limit))
    end

    # Final-syncs every database, releases every write lock, and stops the
    # daemon. A backup another machine writes has no lock and stays waiting.
    # Each database it could not sync or release is in failed, with its code,
    # and keeps any write lock it has; the session and the daemon then stay.
    # An empty failed is the only complete shutdown. close never does this.
    def shutdown(durability: nil, timeout: nil)
      @client.call(Wire.shutdown(durability: durability, timeout: timeout))
    end
  end

  # Databases run on several identical machines.
  class Failovers < Group
    # Runs the database at path, the same on every machine; returns once this
    # machine is the origin or has a replica. See Failover for the callbacks:
    # on_role_change hears the role the start answered, then every change;
    # on_refused hears the daemon's message when it refused this machine's
    # file; on_error hears each failed read. A file at the path that differs
    # from the server head, and is not a clean copy of the version this
    # machine last synced, is refused with conflict and left untouched;
    # discard_local replaces it with the head.
    def start(path, name: nil, timeout: nil, discard_local: false, on_role_change: nil, on_refused: nil, on_error: nil)
      call = Wire.start_failover(path, name: name, timeout: timeout, discard_local: discard_local)
      log_end = @client.call(Wire.events_page(after: -1))
      started = @client.call(call)
      @client.ensure_open
      failover = Failover.new(@client, started, log_end.next, on_role_change: on_role_change,
                                                              on_refused: on_refused, on_error: on_error)
      @client.add_failover(failover)
      failover.start_following
      failover
    end
  end
end
