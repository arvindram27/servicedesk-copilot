"""Browser-surface security helpers (B4)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Final

from fastapi.responses import JSONResponse

PROBLEM_TYPE: Final[str] = "about:blank"
PROBLEM_MEDIA_TYPE: Final[str] = "application/problem+json"


def problem(status: int, code: str, detail: str | None = None) -> JSONResponse:
    """RFC 9457 problem detail with a fixed machine code.

    Only fixed vocabulary is ever placed in the body: no header value, header
    name, path, token, environment value or driver text is echoed.
    """
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE,
        "title": HTTPStatus(status).phrase,
        "status": status,
        "code": code,
    }
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_MEDIA_TYPE)
