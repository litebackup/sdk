"""SQLite filenames and file: URIs, resolved as sqlite3ParseUri does."""

from __future__ import annotations

import os
from collections.abc import Iterable
from urllib.parse import unquote

from ..errors import invalid_argument

NO_FILE = "an in-memory or temporary database has no file to back up; open it from a path"


def _decode(text: str) -> str:
    """SQLite URI percent-decoding; unlike a form, '+' stays '+'."""
    return unquote(text, errors="strict")


def file_from_uri(uri: str, parameters: Iterable[tuple[str, str]] = ()) -> str:
    """The absolute file a file: URI opens; extra parameters are the driver's own, such as SQLAlchemy's query."""
    rest = uri.removeprefix("file:")
    if rest.startswith("//"):
        end = min((index for index in (rest.find(character, 2) for character in "/?#") if index >= 0), default=len(rest))
        authority = rest[2:end]
        if authority not in ("", "localhost"):
            raise invalid_argument(f"SQLite opens only local files, and {uri!r} names host {authority!r}")
        rest = rest[end:]
    rest = rest.split("#", 1)[0]
    encoded_path, _, query = rest.partition("?")
    try:
        path = _decode(encoded_path)
        pairs = [(_decode(key), _decode(value)) for key, _, value in (part.partition("=") for part in query.split("&") if part)]
    except UnicodeDecodeError as error:
        raise invalid_argument(f"cannot decode {uri!r}: {error}") from error
    for key, value in [*pairs, *parameters]:
        if (key, value) in (("mode", "memory"), ("vfs", "memdb")):
            raise invalid_argument(NO_FILE)
    return plain_file(path)


def plain_file(filename: str) -> str:
    """The absolute file a plain SQLite filename opens; '' and ':memory:' open none."""
    if filename in ("", ":memory:"):
        raise invalid_argument(NO_FILE)
    return os.path.abspath(filename)


def file_from_database_list(rows: Iterable[tuple[object, ...]], schema: str) -> str:
    """The file of one schema in PRAGMA database_list rows of (seq, name, file)."""
    for row in rows:
        if row[1] == schema:
            file = row[2]
            if not isinstance(file, str) or not file:
                raise invalid_argument(f"the database {schema!r} is in memory or temporary and has no file to back up")
            return file
    raise invalid_argument(f"no attached database named {schema!r}")
