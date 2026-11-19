"""Hatchling build hook for platform wheels.

scripts/build-wheels sets LITEBACKUP_WHEEL_BINARY to a verified litebackup binary
and LITEBACKUP_WHEEL_PLATFORM to its wheel platform tag. The hook then ships the
binary as litebackup/_bin/litebackup and tags the wheel py3-none-<platform>. With
neither set, the build is the pure wheel or the sdist, which use the installed
litebackup command.
"""

from __future__ import annotations

import os
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class PlatformBinaryHook(BuildHookInterface):  # type: ignore[type-arg]
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel":
            return
        binary = os.environ.get("LITEBACKUP_WHEEL_BINARY")
        platform = os.environ.get("LITEBACKUP_WHEEL_PLATFORM")
        if not binary and not platform:
            return
        if not binary or not platform or not os.path.isfile(binary):
            raise RuntimeError("platform wheels need LITEBACKUP_WHEEL_BINARY (an existing file) and LITEBACKUP_WHEEL_PLATFORM")
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{platform}"
        build_data["force_include"][binary] = "litebackup/_bin/litebackup"
