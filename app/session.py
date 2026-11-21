"""Session bootstrap and the bounded in-memory session store (B4).

A session id is server-generated, never identity, and never a cookie. It scopes
tokens, audit correlation and idempotency lookups in later gates; no endpoint
treats it as authority. Sessions live only in process memory: they die with the
process, and clients re-bootstrap (browser on page load, CLI per process start).

Tokens are emitted in exactly one place: the authorized 200 bootstrap response
body. They are never logged, never labelled into metrics and never echoed in an
error body.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.security import problem
from app.security.origin import classify_request

DEFAULT_TTL_S: Final[float] = 3600.0
DEFAULT_CAPACITY: Final[int] = 1000

router = APIRouter()


@dataclass(frozen=True)
class Session:
    """One live session. Never persisted, never logged."""

    session_id: str
    csrf_token: str
    client_kind: str
    expires_at_monotonic: float
    expires_at_utc: str


class SessionStore:
    """Bounded sessions behind one lock; sweep on mint; live entries never evicted."""

    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        capacity: int = DEFAULT_CAPACITY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._capacity = capacity
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def mint(self, client_kind: str) -> Session | None:
        """Create a session, or None when live capacity is exhausted."""
        with self._lock:
            now = self._clock()
            self._purge_locked(now)
            if len(self._sessions) >= self._capacity:
                return None
            session = Session(
                session_id=secrets.token_urlsafe(32),
                csrf_token=secrets.token_urlsafe(32),
                client_kind=client_kind,
                expires_at_monotonic=now + self._ttl_s,
                expires_at_utc=(datetime.now(UTC) + timedelta(seconds=self._ttl_s)).isoformat(),
            )
            self._sessions[session.session_id] = session
            return session

    def lookup(self, session_id: str) -> Session | None:
        """Return a live session, dropping it first if it has expired."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if self._clock() >= session.expires_at_monotonic:
                del self._sessions[session_id]
                return None
            return session

    def purge_expired(self) -> int:
        """Remove expired sessions and return how many were dropped."""
        with self._lock:
            return self._purge_locked(self._clock())

    def _purge_locked(self, now: float) -> int:
        expired = [
            key
            for key, session in self._sessions.items()
            if now >= session.expires_at_monotonic
        ]
        for key in expired:
            del self._sessions[key]
        return len(expired)


@router.post("/api/session")
def bootstrap(request: Request) -> JSONResponse:
    """Mint one session. Exempt from CSRF and the write switch, not from origin checks."""
    classification = classify_request(request)
    if isinstance(classification, JSONResponse):
        return classification

    store: SessionStore = request.app.state.sessions
    session = store.mint(classification.client_kind)
    if session is None:
        return problem(503, "session_store_full")

    request.state.client_kind = classification.client_kind
    response = JSONResponse(
        status_code=200,
        content={
            "session_id": session.session_id,
            "csrf_token": session.csrf_token,
            "expires_at": session.expires_at_utc,
            "client_kind": session.client_kind,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response
