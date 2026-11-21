"""Host, Origin and Sec-Fetch-Site guard for the browser surface (B4).

The Origin authority is bound to the validated Host authority, string-equal
after normalization, with the listener scheme (plain ``http`` in P0). Loopback
aliases are therefore not same-origin. ``Sec-Fetch-Site`` is defence in depth,
never the binding mechanism.

Classification is a record of request shape, not trust: an Origin-absent request
with no ``Sec-Fetch-*`` header is labelled ``cli`` and still faces the CSRF and
write-switch checks. Every failure is an RFC 9457 problem detail with a fixed
code and no echoed input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from fastapi import Request
from fastapi.responses import JSONResponse

from app.security import problem

MAX_HEADER_LENGTH: Final[int] = 255
LISTENER_SCHEME: Final[str] = "http"
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
_PORT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{1,5}$")


@dataclass(frozen=True)
class Classification:
    """How the request was shaped; carries no authority."""

    client_kind: str  # "browser" | "cli"


def _reject_host_form(host: str) -> bool:
    if not host:
        return True
    if host.endswith("."):
        return True
    if host.isdigit() and host != "127.0.0.1":
        return True
    if host.startswith("0x") or host.startswith("0X"):
        return True
    return False


def _parse_authority(raw: str) -> tuple[str, int] | None:
    """Parse ``host[:port]``; return (lowercase host, port) or None."""
    if not raw or len(raw) > MAX_HEADER_LENGTH:
        return None
    if not raw.isascii():
        return None
    if any(character in raw for character in " \t\r\n,/"):
        return None

    if raw.startswith("["):
        closing = raw.find("]")
        if closing == -1:
            return None
        host = raw[1:closing]
        remainder = raw[closing + 1 :]
        if remainder == "":
            port_text: str | None = None
        elif remainder.startswith(":"):
            port_text = remainder[1:]
        else:
            return None
        if ":" in host and host.lower() != "::1":
            return None
    else:
        if raw.count(":") > 1:
            return None
        host, separator, port_text = raw.partition(":")
        if not separator:
            port_text = None
        if not host:
            return None

    host_lower = host.lower()
    if _reject_host_form(host_lower):
        return None

    if port_text is None:
        port = 80
    else:
        if not _PORT_RE.match(port_text):
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
    return host_lower, port


def _parse_origin(raw: str) -> tuple[str, str] | None:
    """Parse ``scheme://authority``; return (scheme, "host:port") or None."""
    if not raw or len(raw) > MAX_HEADER_LENGTH or not raw.isascii():
        return None
    scheme, separator, remainder = raw.partition("://")
    if not separator:
        return None
    scheme_lower = scheme.lower()
    if scheme_lower not in {"http", "https"}:
        return None
    if any(character in remainder for character in "@/?#"):
        return None
    parsed = _parse_authority(remainder)
    if parsed is None:
        return None
    host, port = parsed
    return scheme_lower, f"{host}:{port}"


def _allowlist(settings: object) -> set[str]:
    port = getattr(settings, "listen_port")
    configured = getattr(settings, "listen_host")
    entries: set[str] = set()
    for host in LOOPBACK_HOSTS | {configured}:
        lowered = str(host).lower()
        if lowered == "::1":
            entries.add(f"[::1]:{port}")
        else:
            entries.add(f"{lowered}:{port}")
    return entries


def classify_request(request: Request) -> Classification | JSONResponse:
    """Validate Host, Origin and Sec-Fetch-Site; return a classification or an error."""
    host_values = request.headers.getlist("host")
    if not host_values:
        return problem(400, "missing_host")
    if len(host_values) != 1:
        return problem(400, "bad_host")
    parsed_host = _parse_authority(host_values[0])
    if parsed_host is None:
        return problem(400, "bad_host")
    host, host_port = parsed_host
    if host not in LOOPBACK_HOSTS:
        return problem(403, "host_not_allowed")
    authority = f"{host}:{host_port}"
    if authority not in _allowlist(request.app.state.settings):
        return problem(403, "host_not_allowed")

    origin_values = request.headers.getlist("origin")
    has_sec_fetch = any(
        name.lower().startswith("sec-fetch-")
        for name in request.headers.keys()
    )

    if not origin_values:
        if has_sec_fetch:
            return problem(403, "missing_origin")
        return Classification("cli")

    if len(origin_values) != 1:
        return problem(403, "bad_origin")
    parsed_origin = _parse_origin(origin_values[0])
    if parsed_origin is None:
        return problem(403, "bad_origin")
    scheme, origin_authority = parsed_origin
    if scheme != LISTENER_SCHEME:
        return problem(403, "bad_origin")
    if origin_authority != authority:
        return problem(403, "origin_not_allowed")

    site_values = request.headers.getlist("sec-fetch-site")
    if not site_values:
        return problem(403, "missing_sec_fetch_site")
    if len(site_values) != 1 or site_values[0] != "same-origin":
        return problem(403, "bad_sec_fetch_site")

    return Classification("browser")
