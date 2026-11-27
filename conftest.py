"""Test-wide guard: no test may reach the network.

Tests talk to fakes (mock HTTP transports, unix sockets). This guard makes any
accidental real connection fail loudly instead of silently reaching a real
service - in this process, and in gateway subprocesses (which inherit a proxy
setting that points at a closed local port).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Iterator
from typing import Any

import pytest

_BLACKHOLE_PROXY = "http://127.0.0.1:9"  # port 9 (discard) is closed: connections are refused


def _is_loopback(address: Any) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True, scope="session")
def _no_network() -> Iterator[None]:
    original_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
            raise ConnectionRefusedError(f"tests must not use the network (tried {address!r})")
        return original_connect(self, address)

    saved = {name: os.environ.get(name) for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")}
    os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = _BLACKHOLE_PROXY
    os.environ["NO_PROXY"] = ""
    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
