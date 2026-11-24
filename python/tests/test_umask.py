"""The umask acquire and restore send."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from litebackup import ErrorCode, LitebackupError
from litebackup._umask import current_umask, request_umask, status_umask


def test_current_umask_reads_the_mask() -> None:
    previous = os.umask(0o027)
    try:
        assert current_umask() == 0o027
        assert request_umask(None) == 0o027
    finally:
        os.umask(previous)


def test_status_umask_parses_a_status_file(tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\tapp\nUmask:\t0077\nState:\tR\n")
    assert status_umask(str(status)) == 0o077
    assert status_umask(str(tmp_path / "missing")) is None


def test_request_umask_checks_a_passed_umask() -> None:
    assert request_umask(0) == 0
    for umask in (-1, 0o1000, True, 1.5):
        with pytest.raises(LitebackupError) as raised:
            request_umask(umask)  # type: ignore[arg-type]
        assert raised.value.code == ErrorCode.INVALID_ARGUMENT


def test_status_umask_reads_past_a_name_that_is_not_utf8(tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.write_bytes(b"Name:\tapp\xff\nUmask:\t0022\n")
    assert status_umask(str(status)) == 0o022


def test_request_umask_reads_the_mask_each_call() -> None:
    previous = os.umask(0o077)
    try:
        assert request_umask(None) == 0o077
        os.umask(0o002)
        assert request_umask(None) == 0o002
    finally:
        os.umask(previous)
