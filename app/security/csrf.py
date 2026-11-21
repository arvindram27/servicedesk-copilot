"""X-SDC-Session / X-SDC-CSRF verification for writes (B4).

Both values are validated for exact length and charset before any comparison.
The session id is a store lookup key; the CSRF value is compared in constant
time. There is no cookie and no rotation: the token lives as long as the
session.
"""

from __future__ import annotations

import hmac
import re
from typing import Final

from fastapi import Request
from fastapi.responses import JSONResponse

from app.security import problem

TOKEN_LENGTH: Final[int] = 43
TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{43}$")


def require_csrf(request: Request) -> JSONResponse | None:
    """Return a problem response when the request is not a valid session write."""
    store = request.app.state.sessions

    session_values = request.headers.getlist("x-sdc-session")
    if len(session_values) != 1:
        return problem(403, "bad_session")
    session_id = session_values[0]
    if not TOKEN_RE.match(session_id):
        return problem(403, "bad_session")

    csrf_values = request.headers.getlist("x-sdc-csrf")
    if len(csrf_values) != 1 or "," in csrf_values[0]:
        return problem(403, "missing_csrf")
    token = csrf_values[0]
    if not token:
        return problem(403, "missing_csrf")
    if not TOKEN_RE.match(token):
        return problem(403, "bad_csrf")

    session = store.lookup(session_id)
    if session is None:
        return problem(403, "bad_session")
    if not hmac.compare_digest(session.csrf_token, token):
        return problem(403, "bad_csrf")
    return None
