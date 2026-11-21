"""Write switch: SDC_WRITES_ENABLED only (B4).

There is no degraded-mode machinery here. The three-value definition
(normal / retrieval_only / maintenance) is proposed-pending-review and is not
built: one boolean gates the write path, and a disabled switch answers 503.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.security import problem


def require_writes_enabled(request: Request) -> JSONResponse | None:
    """Return a 503 problem response when writes are disabled."""
    settings = request.app.state.settings
    if not getattr(settings, "writes_enabled", True):
        return problem(503, "writes_disabled")
    return None
