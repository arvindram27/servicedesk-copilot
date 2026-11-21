"""Shared B0 test scaffolding: the transport guard only.

No later-gate module (db, audit, health, metrics, session, security, providers,
embeddings, itsm) is imported here, and no database is created.

The guard fails any socket connection whose destination is not loopback. The
loopback exception is deliberately narrow: it exists so that a later gate's
live-server tests (child-process crash tests on 127.0.0.1) can run, and it
proves nothing about non-loopback isolation by itself.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

import pytest

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _host_of(address: object) -> str | None:
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, bytes):
            return host.decode("ascii", "replace")
        if isinstance(host, str):
            return host
    if isinstance(address, str):
        return address
    return None


def is_loopback_address(address: object) -> bool:
    """True only for an explicit loopback destination."""
    host = _host_of(address)
    if host is None:
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def transport_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Block outbound socket connections that are not to loopback."""
    attempts: list[str] = []
    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection

    def guarded_connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        if not is_loopback_address(address):
            attempts.append(str(address))
            raise RuntimeError(f"transport guard blocked outbound connection to {address!r}")
        return real_connect(self, address, *args, **kwargs)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        if not is_loopback_address(address):
            attempts.append(str(address))
            raise RuntimeError(f"transport guard blocked outbound connection to {address!r}")
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    yield attempts
