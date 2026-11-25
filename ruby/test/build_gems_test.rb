# frozen_string_literal: true

require_relative "test_helper"
require "digest"
require "openssl"
require "open3"
require "rubygems/package"
load File.expand_path("../scripts/build-gems", __dir__)

# scripts/build-gems: release verification, binary checks, and the gems it
# builds. The fixture's 0.3.30 release is real, so its binaries are not
# here; the gems are built from a release of the gem's own version that this
# test signs, with
# stand-in binaries.
class BuildGemsTest < Minitest::Test
  include WorkingDirectory

  METADATA = File.join(CONTRACT_ROOT, "binary")
  SCRIPT = File.expand_path("../scripts/build-gems", __dir__)
  VERSION = Litebackup::VERSION
  EXPECTED_GEMS = ["litebackup-#{VERSION}.gem"] + %w[
    x86_64-linux-gnu x86_64-linux-musl aarch64-linux-gnu aarch64-linux-musl x86_64-darwin arm64-darwin
  ].map { |gem_platform| "litebackup-#{VERSION}-#{gem_platform}.gem" }

  def base64url(bytes)
    [bytes].pack("m0").tr("+/", "-_").delete("=")
  end

  # A static ELF binary's first bytes: its header and one PT_LOAD segment,
  # or PT_INTERP for a dynamic one.
  def elf(machine, segment_type: 1)
    header = "\x7fELF".b + [2, 1, 1].pack("C3") + ("\0" * 9)
    header += [2, machine, 1, 0, 64, 0, 0, 64, 56, 1, 64, 0, 0].pack("vvVQ<Q<Q<Vvvvvvv")
    header + [segment_type].pack("V") + ("\0" * 52)
  end

  def macho(cpu)
    [0xFEEDFACF, cpu, 0, 2, 0, 0, 0, 0].pack("Vl<l<VVVVV")
  end

  def stand_in_binaries
    {
      "litebackup-linux-amd64" => elf(62), "litebackup-linux-arm64" => elf(183),
      "litebackup-darwin-amd64" => macho(0x01000007), "litebackup-darwin-arm64" => macho(0x0100000C)
    }
  end

  # Writes binaries into the working directory and metadata whose release
  # of version a new key signs; returns the metadata path and the keys.
  def signed_release(binaries, version: VERSION)
    key = OpenSSL::PKey.generate_key("ED25519")
    public_key = base64url(key.public_to_der.byteslice(-32, 32))
    artifacts = binaries.map do |name, content|
      File.binwrite(name, content)
      { "name" => name, "filename" => name, "url" => "https://example.invalid/#{name}",
        "sha256" => Digest::SHA256.hexdigest(content), "size" => content.bytesize }
    end
    document = JSON.generate("version" => version, "artifacts" => artifacts)
    signature = key.sign(nil, "clor-apps-v1\0test-key\0#{document}")
    release = { "version" => version, "channels" => ["stable"],
                "signature" => { "key_id" => "test-key", "document" => base64url(document),
                                 "signature" => base64url(signature) } }
    File.write("metadata.json", JSON.generate("packages" => [{ "slug" => "cli", "releases" => [release] }]))
    ["metadata.json", { "test-key" => public_key }]
  end

  def build(keys, metadata, version: VERSION, package_version: VERSION)
    GemBuild.build_gems(release_directory: @working_directory, metadata: metadata, version: version,
                        package_version: package_version, output: "out", keys: keys)
  end

  def test_a_verified_release_builds_the_plain_gem_and_every_platform_gem
    binaries = stand_in_binaries
    metadata, keys = signed_release(binaries)
    built = build(keys, metadata)
    assert_equal(EXPECTED_GEMS.sort, built.map { |path| File.basename(path) }.sort)
    built.each do |path|
      package = Gem::Package.new(path)
      assert_empty(package.spec.executables, "gem install would write a litebackup wrapper")
      bundled = package.contents.include?("lib/litebackup/bin/litebackup")
      assert_equal(package.spec.platform != Gem::Platform::RUBY, bundled, path)
      assert_includes(package.contents, "lib/litebackup.rb")
      assert_equal(VERSION, package.spec.version.to_s)
    end
    musl = Gem::Package.new(File.join("out", "litebackup-#{VERSION}-aarch64-linux-musl.gem"))
    entry = nil
    musl.gem.with_read_io do |io|
      Gem::Package::TarReader.new(io).each do |tar_entry|
        next unless tar_entry.full_name == "data.tar.gz"

        Gem::Package::TarReader.new(Zlib::GzipReader.new(tar_entry)).each do |data_entry|
          if data_entry.full_name == "lib/litebackup/bin/litebackup"
            entry = [data_entry.header.mode & 0o777,
                     data_entry.read]
          end
        end
      end
    end
    assert_equal([0o755, binaries["litebackup-linux-arm64"]], entry)
    assert_includes(packaged_file(musl, "lib/litebackup/version.rb"), %(VERSION = "#{VERSION}"))
  end

  # The text of one file a gem packages.
  def packaged_file(package, name)
    text = nil
    package.gem.with_read_io do |io|
      Gem::Package::TarReader.new(io).each do |tar_entry|
        next unless tar_entry.full_name == "data.tar.gz"

        Gem::Package::TarReader.new(Zlib::GzipReader.new(tar_entry)).each do |data_entry|
          text = data_entry.read if data_entry.full_name == name
        end
      end
    end
    text
  end

  def test_tampered_metadata_builds_nothing
    error = assert_raises(GemBuild::BuildError) do
      build(Litebackup::Release::RELEASE_KEYS, File.join(METADATA, "package-metadata-tampered.json"),
            version: "0.3.30", package_version: "0.3.30")
    end
    assert_includes(error.message, "does not hold a verified release")
    refute_path_exists("out")
  end

  def test_a_release_version_unlike_the_gems_is_refused
    metadata, keys = signed_release(stand_in_binaries, version: "0.3.30")
    error = assert_raises(GemBuild::BuildError) { build(keys, metadata, version: "0.3.30") }
    assert_includes(error.message, "every release is lockstep")
    # A caller that says the gem is 0.3.30 is still refused: its code reports VERSION.
    error = assert_raises(GemBuild::BuildError) do
      build(keys, metadata, version: "0.3.30", package_version: "0.3.30")
    end
    assert_includes(error.message, "is not the gem's version #{VERSION}")
    refute_path_exists("out")
  end

  def test_a_binary_that_does_not_match_builds_nothing
    metadata, keys = signed_release(stand_in_binaries)
    File.binwrite("litebackup-linux-amd64", "not the signed binary")
    error = assert_raises(GemBuild::BuildError) { build(keys, metadata) }
    assert_includes(error.message, "does not match release #{VERSION}")
    refute_path_exists("out")
  end

  def test_a_dynamic_or_foreign_binary_builds_nothing
    {
      "litebackup-linux-amd64" => [elf(62, segment_type: 3), "the binary is dynamic"],
      "litebackup-linux-arm64" => [elf(62), "not aarch64"],
      "litebackup-darwin-arm64" => [macho(0x01000007), "not arm64"]
    }.each do |name, (content, message)|
      metadata, keys = signed_release(stand_in_binaries.merge(name => content))
      error = assert_raises(GemBuild::BuildError) { build(keys, metadata) }
      assert_includes(error.message, message)
    end
    refute_path_exists("out")
  end

  def test_the_command_refuses_the_real_release_without_its_binaries
    output, status = Open3.capture2e(RbConfig.ruby, SCRIPT, "--version", "0.3.30", "--output", "out",
                                     @working_directory, File.join(METADATA, "package-metadata.json"))
    assert_equal(1, status.exitstatus, output)
    assert_includes(output, "is missing; every platform is built together")
    refute_path_exists("out")
  end
end
