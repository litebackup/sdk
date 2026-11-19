from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def cwd(tmp_path: Path) -> Iterator[str]:
    """A working directory for one test, its symlinks resolved."""
    directory = os.path.realpath(tmp_path)
    previous = os.getcwd()
    os.chdir(directory)
    try:
        yield directory
    finally:
        os.chdir(previous)
