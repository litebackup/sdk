# frozen_string_literal: true

# Read as text, so building several gems in one process loads no constant twice.
version = File.read(File.join(__dir__, "lib", "litebackup", "version.rb"))[/^  VERSION = "(.+)"$/, 1]

Gem::Specification.new do |spec|
  spec.name = "litebackup"
  spec.version = version
  spec.summary = "Back up, replicate, and restore SQLite databases with Litebackup"
  spec.description = "A client of the litebackup daemon on this machine: continuous backups, " \
                     "restores, replicas, leases of databases that move between machines, " \
                     "failovers, and a Rails integration."
  spec.authors = ["Litebackup"]
  spec.homepage = "https://litebackup.com"
  spec.license = "MIT"
  spec.required_ruby_version = ">= 3.2"
  spec.metadata = {
    "source_code_uri" => "https://github.com/litebackup/sdk",
    "rubygems_mfa_required" => "true"
  }
  # The platform gems that scripts/build-gems makes add lib/litebackup/bin/litebackup.
  # It is not an executable: gem install writes no litebackup wrapper that would
  # shadow an installed command.
  spec.files = Dir["lib/**/*.rb", "lib/litebackup/bin/litebackup", "README.md", "LICENSE"]
  spec.require_paths = ["lib"]
end
