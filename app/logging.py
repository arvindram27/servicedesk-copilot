"""Structured JSON application logging with key and token redaction (B0).

Two sinks are planned for this project: the durable ``audit_events`` table
(later gate) and these stdout application logs. Only the stdout sink exists in
B0. Stated limit: redaction covers the rendered message and formatted
exceptions; it is a filter over known key- and token-shaped patterns, not a
guarantee that every possible secret shape is caught.

Structured extras are limited to four fields (``event``, ``outcome``,
``request_id``, ``session_id``) and to exact builtin scalars: strings are
redacted, booleans, integers and finite floats pass through unchanged, and
everything else becomes the constant placeholder ``[REJECTED]``. No recursive
walk, no ``str()``/``repr()`` call on unknown objects, and no ``default=``
hook, so an arbitrary object cannot emit a secret through formatting.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from datetime import UTC, datetime
from typing import Any, Final, TextIO

REDACTION_MARKER: Final[str] = "[REDACTED]"
REJECTED_MARKER: Final[str] = "[REJECTED]"
FALLBACK_MESSAGE: Final[str] = "[LOG_FORMATTING_FAILED]"

# Pre-encoded literal. It performs no serialization and interpolates nothing
# from the record, so it cannot fail or leak even when the record's own content
# is unencodable.
FALLBACK_LINE: Final[str] = (
    '{"ts":"1970-01-01T00:00:00+00:00","level":"ERROR","logger":"sdc",'
    '"message":"[LOG_FORMATTING_FAILED]"}'
)

_REDACTION_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "api_key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{10,}"), "api_key"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}=*"), "credential"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|confirmation[_-]?token"
            r"|idempotency[_-]?key|secret)\b\s*[:=]\s*\S+"
        ),
        "field",
    ),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "opaque_id"),
)

_EXTRA_FIELDS: Final[tuple[str, ...]] = ("event", "outcome", "request_id", "session_id")


def redact(text: str) -> str:
    """Return ``text`` with key- and token-shaped substrings replaced."""
    redacted = text
    for pattern, kind in _REDACTION_PATTERNS:
        redacted = pattern.sub(f"{REDACTION_MARKER}:{kind}", redacted)
    return redacted


def _sanitize_extra(value: object) -> object:
    """Map one structured extra to a JSON-safe, non-secret value."""
    if type(value) is str:
        return redact(value)
    if type(value) is bool or type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return REJECTED_MARKER


class RedactionFilter(logging.Filter):
    """Render the record, redact it, then freeze it before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """Format one log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload: dict[str, Any] = {
                "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            for field_name in _EXTRA_FIELDS:
                value = getattr(record, field_name, None)
                if value is not None:
                    payload[field_name] = _sanitize_extra(value)
            if record.exc_info:
                payload["exception"] = redact(self.formatException(record.exc_info))
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return FALLBACK_LINE


def configure_logging(
    level: int = logging.INFO,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Attach a redacting JSON handler to the ``sdc`` logger."""
    logger = logging.getLogger("sdc")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
    return logger
