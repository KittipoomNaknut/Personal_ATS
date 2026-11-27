"""Loading the Anthropic API key from a file, with strict checks.

The key file is written by ``atsctl start`` to a private tmpfs directory and is
normally deleted right after the gateway has read it, so the key then exists
only in this process's memory. Nothing here ever prints or logs the key, and
error messages never include file contents.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Final, NoReturn

__all__ = ["ApiKey", "KeyFileError", "load_api_key"]

_KEY_FORMAT: Final = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,200}")
_MAX_FILE_BYTES: Final = 512


class KeyFileError(Exception):
    """The key file is missing, unsafe or does not contain a valid key."""


class ApiKey:
    """An API key that does not reveal itself in repr/str, logs or pickles."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not _KEY_FORMAT.fullmatch(value):
            raise KeyFileError("the key file does not contain a valid Anthropic API key")
        self._value = value

    def reveal(self) -> str:
        """The raw key. Only for constructing the API client."""
        return self._value

    def __repr__(self) -> str:
        return "ApiKey(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("ApiKey cannot be pickled")


def load_api_key(path: Path, *, unlink: bool = False) -> ApiKey:
    """Read and validate the key file; optionally delete it after reading.

    The file must be a regular file (not a symlink), owned by the current user,
    and not readable or writable by group or others.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise KeyFileError(f"key file not found: {path}") from None
    except OSError as exc:  # ELOOP for a symlink, EACCES, ...
        raise KeyFileError(f"cannot open key file {path}: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise KeyFileError(f"key file is not a regular file: {path}")
        if info.st_uid != os.getuid():
            raise KeyFileError(f"key file is not owned by the current user: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise KeyFileError(f"key file is accessible by other users; run: chmod 600 {path}")
        if info.st_size > _MAX_FILE_BYTES:
            raise KeyFileError(f"key file is too large to be an API key: {path}")
        data = os.read(fd, _MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if unlink:
        path.unlink(missing_ok=True)
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise KeyFileError("the key file does not contain a valid Anthropic API key") from None
    return ApiKey(text)
