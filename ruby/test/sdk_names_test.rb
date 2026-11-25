# frozen_string_literal: true

require_relative "test_helper"

# Checks the Ruby SDK against testdata/contract/names/sdk-names.json, the
# names every SDK shares. Ruby spells operations as Python does.
class SDKNamesTest < Minitest::Test
  SDK_NAMES = JSON.parse(File.read(File.join(CONTRACT_ROOT, "names", "sdk-names.json")))

  def test_shared_operation_methods
    client = Litebackup::Client.new(socket_path: "/nonexistent/client.sock")
    SDK_NAMES["operations"].each do |entry|
      group, method_name = entry["operation"].split(".")
      assert_respond_to(client.public_send(group), method_name, entry["operation"])
    end
  ensure
    client&.close
  end

  def test_shared_type_exports
    SDK_NAMES["types"].each do |type_name|
      assert(Litebackup.const_defined?(type_name, false), type_name)
    end
  end
end
