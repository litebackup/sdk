"""Verifying Litebackup's signed package metadata. Needs the ``litebackup[release]`` extra (cryptography).

The release is always taken from its signed document, never from the unsigned listing.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

RELEASE_KEYS: dict[str, str] = {
    "01a1ead6-4b94-79ca-81f4-3a67c112bc0b": "t3C_3l71PimyhkPZOzJ_Xi4dOuHJ1CmSdDZ8lTm0vNI",
}
"""Litebackup's Ed25519 release keys by key ID, base64url without padding, as the litebackup command checks them."""

COMMAND_PACKAGE = "cli"
STABLE_CHANNEL = "stable"
_SIGNATURE_CONTEXT = b"clor-apps-v1\x00"
_SIGNATURE_SEPARATOR = b"\x00"


class SignatureError(Exception):
    """A release whose signature does not match Litebackup's release keys."""

    def __init__(self) -> None:
        super().__init__("the release signature does not match Litebackup's release keys")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseFile:
    """One published file of a release."""

    name: str
    filename: str
    url: str
    sha256: str
    size: int
    os: str = ""
    architecture: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Release:
    """A package release taken from its verified signed document."""

    version: str
    channels: list[str]
    files: list[ReleaseFile]

    def file(self, name: str) -> ReleaseFile:
        for candidate in self.files:
            if candidate.name == name:
                return candidate
        raise KeyError(f"release {self.version} has no file {name}")


def _base64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _verify(signed: Any, keys: dict[str, str]) -> dict[str, Any]:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as error:
        raise ImportError("verifying releases needs the release extra: pip install 'litebackup[release]'") from error
    if not isinstance(signed, dict) or signed.get("key_id") not in keys:
        raise SignatureError()
    try:
        key = Ed25519PublicKey.from_public_bytes(_base64url(keys[signed["key_id"]]))
        document = _base64url(signed["document"])
        key.verify(_base64url(signed["signature"]), _SIGNATURE_CONTEXT + signed["key_id"].encode() + _SIGNATURE_SEPARATOR + document)
        decoded = json.loads(document)
    except (InvalidSignature, KeyError, TypeError, ValueError) as error:
        raise SignatureError() from error
    if not isinstance(decoded, dict):
        raise SignatureError()
    return decoded


def find_release(
    metadata: str | bytes | dict[str, Any],
    *,
    package: str = COMMAND_PACKAGE,
    version: str | None = None,
    channel: str = STABLE_CHANNEL,
    keys: dict[str, str] | None = None,
) -> Release:
    """Reads package metadata, as https://litebackup.com/pkg/metadata serves it, and returns the
    selected release from its signed document. Raises SignatureError when the signature does not verify."""
    listing = json.loads(metadata) if isinstance(metadata, str | bytes) else metadata
    listed = next((candidate for candidate in listing.get("packages", []) if candidate.get("slug") == package), None)
    if listed is None:
        raise LookupError(f"no package {package}")
    candidate = next(
        (
            release
            for release in listed.get("releases", [])
            if (release.get("version") == version if version else channel in release.get("channels", []))
        ),
        None,
    )
    if candidate is None:
        raise LookupError(f"package {package} has no release {version}" if version else f"package {package} has no release on {channel}")
    document = _verify(candidate.get("signature"), RELEASE_KEYS if keys is None else keys)
    if document.get("version") != candidate.get("version"):
        raise SignatureError()
    files = [
        ReleaseFile(
            name=artifact["name"],
            filename=artifact["filename"],
            url=artifact["url"],
            sha256=artifact["sha256"],
            size=artifact["size"],
            os=artifact.get("os", ""),
            architecture=artifact.get("architecture", ""),
        )
        for artifact in document.get("artifacts") or []
    ]
    return Release(version=document["version"], channels=list(candidate.get("channels", [])), files=files)
