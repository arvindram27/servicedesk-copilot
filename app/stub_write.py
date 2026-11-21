"""Stub guarded write route (B4).

This is NOT the ITSM create: it imports nothing from ``app/itsm/``, performs no
write of any kind (no database row, no audit event, no file) and changes no
state beyond the session lookup. It exists so the B4 guards are exercised
against the production wiring. B6 replaces it with the real guarded create.

Check order: Host -> Origin/Sec-Fetch -> write switch -> CSRF -> handler.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.security.csrf import require_csrf
from app.security.origin import classify_request
from app.security.writes import require_writes_enabled

router = APIRouter()


@router.post("/api/_stub/write", response_model=None)
def stub_write(request: Request) -> JSONResponse | dict[str, str]:
    """Run the full guard chain and return an accepted marker."""
    classification = classify_request(request)
    if isinstance(classification, JSONResponse):
        return classification
    request.state.client_kind = classification.client_kind

    blocked = require_writes_enabled(request)
    if blocked is not None:
        return blocked

    blocked = require_csrf(request)
    if blocked is not None:
        return blocked

    return {"status": "accepted"}
