"""Checks the Python SDK against testdata/contract/names/sdk-names.json, the names all three SDKs share."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import litebackup
from litebackup import AsyncLitebackup, Litebackup

SDK_NAMES: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "testdata" / "contract" / "names" / "sdk-names.json").read_text()
)


@pytest.mark.parametrize("client_class", [Litebackup, AsyncLitebackup])
def test_shared_operation_methods(client_class: type[Litebackup] | type[AsyncLitebackup]) -> None:
    client = client_class(socket_path="/nonexistent/client.sock")
    for entry in SDK_NAMES["operations"]:
        group, method = entry["operation"].split(".")
        assert callable(getattr(getattr(client, group, None), method, None)), f"{client_class.__name__}: {entry['operation']}"


def test_shared_type_exports() -> None:
    for type_name in SDK_NAMES["types"]:
        assert type_name in litebackup.__all__, type_name
