# frozen_string_literal: true

require "minitest/autorun"
require "json"
require "tmpdir"
require "open3"
require "pathname"
require "litebackup"
require_relative "contract_daemon"

# The contract fixtures every SDK runs.
CONTRACT_ROOT = File.expand_path("../../testdata/contract", __dir__)

# A temporary working directory for one test, its symlinks resolved, entered
# for the test and left after it.
module WorkingDirectory
  def setup
    super
    @previous_directory = Dir.pwd
    @working_directory = File.realpath(Dir.mktmpdir("litebackup-test-"))
    Dir.chdir(@working_directory)
  end

  def teardown
    Dir.chdir(@previous_directory)
    FileUtils.chmod_R("u+rwx", @working_directory)
    FileUtils.rm_rf(@working_directory)
    super
  end
end

# A gate threads wait at until it opens, like Python's threading.Event.
class Gate
  def initialize
    @lock = Mutex.new
    @opened = ConditionVariable.new
    @open = false
  end

  def open!
    @lock.synchronize do
      @open = true
      @opened.broadcast
    end
  end

  def open?
    @open
  end

  # Waits up to seconds for the gate to open; returns whether it is open.
  def wait(seconds)
    deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + seconds
    @lock.synchronize do
      until @open
        left = deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC)
        break if left <= 0

        @opened.wait(@lock, left)
      end
      @open
    end
  end
end

# Polls every 10 ms until the block is true, failing after seconds.
def await_reported(seconds = 5)
  deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + seconds
  until yield
    raise Minitest::Assertion, "timed out" if Process.clock_gettime(Process::CLOCK_MONOTONIC) > deadline

    sleep(0.01)
  end
end

def json_answer(body, status = 200)
  [status, "application/json", JSON.generate(body)]
end
