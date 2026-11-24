"""scripts/build-wheels: binary inspection and release verification, and the package metadata."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-wheels"
METADATA = Path(__file__).resolve().parents[2] / "testdata" / "contract" / "binary"


def load_script() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("build_wheels", str(SCRIPT))
    spec = importlib.util.spec_from_loader("build_wheels", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_a_dynamic_binary_gets_one_manylinux_tag_within_the_host_glibc() -> None:
    if not sys.platform.startswith("linux") or os.uname().machine != "x86_64":
        pytest.skip("needs a glibc x86_64 Python to inspect")
    build_wheels = load_script()
    tags = build_wheels.elf_platforms(Path(os.path.realpath(sys.executable)).read_bytes(), "x86_64", ["static"])
    assert len(tags) == 1 and re.fullmatch(r"manylinux_2_\d+_x86_64", tags[0]), tags
    glibc = os.confstr("CS_GNU_LIBC_VERSION") or ""
    assert int(tags[0].split("_")[2]) <= int(glibc.split(".")[1])


def test_a_static_binary_gets_both_linux_tags(tmp_path: Path) -> None:
    if shutil.which("go") is None or os.uname().machine != "x86_64":
        pytest.skip("needs go to build a static binary")
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
    (tmp_path / "go.mod").write_text("module static\n\ngo 1.21\n")
    subprocess.run(["go", "build", "-o", "static"], cwd=tmp_path, env={**os.environ, "CGO_ENABLED": "0"}, check=True)
    build_wheels = load_script()
    content = (tmp_path / "static").read_bytes()
    assert build_wheels.elf_platforms(content, "x86_64", ["manylinux_2_17_x86_64", "musllinux_1_2_x86_64"]) == [
        "manylinux_2_17_x86_64",
        "musllinux_1_2_x86_64",
    ]
    with pytest.raises(SystemExit):
        build_wheels.elf_platforms(content, "aarch64", [])


def macho(minimum: tuple[int, int], cpu: int = 0x0100000C) -> bytes:
    """A Mach-O header with one LC_BUILD_VERSION for macOS."""
    encoded = minimum[0] << 16 | minimum[1] << 8
    command = struct.pack("<IIIIII", 0x32, 24, 1, encoded, encoded, 0)
    return struct.pack("<IiiIIIII", 0xFEEDFACF, cpu, 0, 2, 1, len(command), 0, 0) + command


def test_macos_tags_follow_the_minimum_os() -> None:
    build_wheels = load_script()
    assert build_wheels.macho_platform(macho((11, 0)), "arm64") == "macosx_11_0_arm64"
    assert build_wheels.macho_platform(macho((12, 3)), "arm64") == "macosx_13_0_arm64"
    assert build_wheels.macho_platform(macho((10, 15), cpu=0x01000007), "x86_64") == "macosx_10_15_x86_64"
    with pytest.raises(SystemExit):
        build_wheels.macho_platform(macho((11, 0)), "x86_64")


def build(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Calls scripts/build-wheels with arguments."""
    return subprocess.run([str(SCRIPT), *arguments], capture_output=True, text=True, timeout=300, check=False)


def test_tampered_metadata_builds_nothing(tmp_path: Path) -> None:
    completed = build(
        "--version", "0.3.30", "--output", str(tmp_path / "out"), str(tmp_path), str(METADATA / "package-metadata-tampered.json")
    )
    assert completed.returncode == 1 and "does not hold a verified release" in completed.stderr
    assert not (tmp_path / "out").exists()


def test_a_binary_that_does_not_match_builds_nothing(tmp_path: Path) -> None:
    (tmp_path / "litebackup-linux-amd64").write_bytes(b"not the signed binary")
    completed = build("--version", "0.3.30", "--output", str(tmp_path / "out"), str(tmp_path), str(METADATA / "package-metadata.json"))
    assert completed.returncode == 1 and "does not match release 0.3.30" in completed.stderr
    assert not (tmp_path / "out").exists()


def test_package_metadata_names_the_license_and_project_links() -> None:
    project = tomllib.loads((SCRIPT.parents[1] / "pyproject.toml").read_text())["project"]
    assert project["license"] == "MIT"
    assert project["urls"] == {
        "Homepage": "https://litebackup.com/sdk",
        "Source": "https://github.com/litebackup/sdk",
        "Issues": "https://github.com/litebackup/sdk/issues",
    }


def test_a_missing_platform_builds_nothing(tmp_path: Path) -> None:
    completed = build("--version", "0.3.30", "--output", str(tmp_path / "out"), str(tmp_path), str(METADATA / "package-metadata.json"))
    assert completed.returncode == 1 and "is missing; every platform is built together" in completed.stderr
    assert not (tmp_path / "out").exists()


def test_another_version_than_the_package_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    script = load_script()
    script.require_package_version("1.1.0", "1.1.0")
    with pytest.raises(SystemExit):
        script.require_package_version("0.3.30", "1.1.0")
    assert "every release is lockstep" in capsys.readouterr().err
