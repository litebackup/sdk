"""Telling an API key from a join token, as the daemon does."""

from __future__ import annotations

import re
from typing import Literal

from .errors import invalid_argument

API_KEY_ENVIRONMENT = "LITEBACKUP_API_KEY"
"""Holds the credential a daemon uses when it starts: an API key, or a join token the daemon
exchanges once for a key of the machine, then @ and the server's address."""

# Join and invite tokens start with longer prefixes of an API key's.
_API_KEY_PREFIX = "litebackup_"
_JOIN_TOKEN_PREFIX = "litebackup_join_"
_INVITE_PREFIX = "litebackup_invite_"

CredentialType = Literal["api_key", "join_token"]


# The server address a credential may name, as the daemon checks it: an optional http:// or https://
# in any case, a host name or a bracketed IPv6 address, an optional port, and an optional path
# without a query or fragment, of ASCII URL characters and percent escapes.
_SERVER_ADDRESS = re.compile(
    r"(?:[Hh][Tt][Tt][Pp][Ss]?://)?(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?(?:/(?:[A-Za-z0-9._~!$&'()*+,;=:/-]|%[0-9A-Fa-f]{2})*)?"
)


def credential_type(key_and_server: str | None) -> CredentialType | None:
    """Tells an API key from a join token by prefix; None for an empty credential, which leaves the daemon its saved key.

    Raises invalid_argument, without repeating the credential, for an invite token, a credential with
    neither prefix, or one without @ and the server's address: a host, or an http:// or https:// URL of one.
    """
    stripped = (key_and_server or "").strip()
    if not stripped:
        return None
    secret, separator, server = stripped.rpartition("@")
    if not separator or not secret or not _SERVER_ADDRESS.fullmatch(server):
        raise invalid_argument(
            "expected <API_KEY>@<HOST>: an API key or a join token from Machines → Add machine, "
            "then @ and the server's address: a host, or an http:// or https:// URL of one"
        )
    if secret.startswith(_INVITE_PREFIX):
        raise invalid_argument(
            "this is an invite token, which signs a person in to the console; use an API key or a join token from Machines → Add machine"
        )
    if secret.startswith(_JOIN_TOKEN_PREFIX):
        return "join_token"
    if secret.startswith(_API_KEY_PREFIX):
        return "api_key"
    raise invalid_argument(f"expected an API key or a join token, which start with {_API_KEY_PREFIX}; get one from Machines → Add machine")
