"""Audit event schema: closed registry, column contract and canonical JSON (B2).

Two sinks exist in this project. ``audit_events`` in SQLite is the durable
system of record and is part of backup/restore invariants. Structured JSON
application logs on stdout are operational, redacted and not backed up. This
module owns the durable sink's event contract only.

Bounded claims recorded here:
- The registry is closed. Unknown event types are refused; adding one is a
  reviewed change.
- Redaction of free-text payload strings is best-effort scrubbing of the known
  pattern set in ``app.logging``; it is not proof that no secret can ever be
  stored.
- Identifier columns are screened, never rewritten. A legitimate identifier
  that happens to be 32 or more contiguous hexadecimal characters is refused
  by design, because it is indistinguishable from a key-shaped value.
- No hash-chain columns exist and none are populated or depended on here.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from app.logging import redact

MAX_PAYLOAD_DEPTH: Final[int] = 4
MAX_PAYLOAD_NODES: Final[int] = 100

_KIND_STR: Final[str] = "str"
_KIND_STR_OR_NONE: Final[str] = "str_or_none"
_KIND_BOOL: Final[str] = "bool"
_KIND_INT: Final[str] = "int"

_UTC_ISO_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00$"
)


class AuditSchemaError(RuntimeError):
    """A schema/contract violation. Messages are static categories only.

    Optional ``field`` is allowed only for a key that appears in the closed
    per-type contract. Caller-supplied strings, undeclared key names and
    rejected values are never echoed.
    """

    def __init__(self, category: str, field_name: str | None = None) -> None:
        self.category = category
        self.field_name = field_name
        super().__init__(category if field_name is None else f"{category}:{field_name}")


@dataclass(frozen=True)
class EventSpec:
    """Closed contract for one event type."""

    required: Mapping[str, str]
    optional: Mapping[str, str]
    node: str
    outcome: str


EVENT_TYPES: Final[dict[str, EventSpec]] = {
    "migration.applied": EventSpec(
        required={
            "from_revision": _KIND_STR_OR_NONE,
            "to_revision": _KIND_STR,
            "backup_path": _KIND_STR_OR_NONE,
            "skipped_reason": _KIND_STR_OR_NONE,
            "schema_no_change": _KIND_BOOL,
        },
        optional={},
        node="migrate",
        outcome="ok",
    ),
    "provider.error": EventSpec(
        required={"stage": _KIND_STR, "error_class": _KIND_STR},
        optional={
            "provider": _KIND_STR,
            "model": _KIND_STR,
            "attempt": _KIND_INT,
            "retryable": _KIND_BOOL,
        },
        node="provider",
        outcome="error",
    ),
    "budget.settle_unknown": EventSpec(
        required={"run_id": _KIND_STR, "reason": _KIND_STR},
        optional={
            "calls_reserved": _KIND_INT,
            "tokens_reserved": _KIND_INT,
            "usd_reserved": _KIND_STR,
        },
        node="budget",
        outcome="unknown",
    ),
    "chat.answer": EventSpec(
        required={
            "article_id": _KIND_STR,
            "article_version": _KIND_INT,
            "section": _KIND_STR,
            "citation_ok": _KIND_BOOL,
            "top_k": _KIND_INT,
        },
        optional={"mode": _KIND_STR},
        node="chat",
        outcome="ok",
    ),
    "chat.refusal": EventSpec(
        required={"reason": _KIND_STR},
        optional={"top_k": _KIND_INT, "best_score_milli": _KIND_INT},
        node="chat",
        outcome="refused",
    ),
    "ticket.confirm": EventSpec(
        required={"draft_hash": _KIND_STR, "token_issued": _KIND_BOOL},
        optional={},
        node="ticket",
        outcome="ok",
    ),
    "ticket.create": EventSpec(
        required={"ticket_id": _KIND_STR, "draft_hash": _KIND_STR},
        optional={},
        node="itsm",
        outcome="ok",
    ),
    "ticket.replay": EventSpec(
        required={"ticket_id": _KIND_STR, "draft_hash": _KIND_STR},
        optional={},
        node="itsm",
        outcome="ok",
    ),
    "ticket.reject": EventSpec(
        required={"reason": _KIND_STR},
        optional={},
        node="itsm",
        outcome="refused",
    ),
}


def _matches_kind(value: object, kind: str) -> bool:
    if kind == _KIND_STR:
        return type(value) is str
    if kind == _KIND_STR_OR_NONE:
        return value is None or type(value) is str
    if kind == _KIND_BOOL:
        return type(value) is bool
    if kind == _KIND_INT:
        return type(value) is int
    return False


def _validate_json_value(value: object, *, depth: int, budget: list[int]) -> None:
    """Validate a JSON-shaped value without coercing anything."""
    if depth > MAX_PAYLOAD_DEPTH:
        raise AuditSchemaError("payload_too_deep")
    budget[0] -= 1
    if budget[0] < 0:
        raise AuditSchemaError("payload_too_large")
    if value is None or type(value) is bool or type(value) is str:
        return
    if type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise AuditSchemaError("invalid_scalar_type")
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item, depth=depth + 1, budget=budget)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise AuditSchemaError("invalid_key_type")
            _validate_json_value(item, depth=depth + 1, budget=budget)
        return
    raise AuditSchemaError("invalid_scalar_type")


def to_json(payload: Mapping[str, object]) -> str:
    """Canonical JSON for a validated payload (no coercion, no ``default=``)."""
    if not isinstance(payload, Mapping):
        raise AuditSchemaError("invalid_payload_type")
    _validate_json_value(dict(payload), depth=0, budget=[MAX_PAYLOAD_NODES])
    return json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


@dataclass(frozen=True)
class AuditEvent:
    """One audit row, mirroring the migrated ``audit_events`` columns."""

    event_type: str
    payload: Mapping[str, object]
    session_id: str | None = None
    requester_label: str | None = None
    node: str | None = None
    outcome: str | None = None
    duration_ms: int | None = None
    occurred_at: str | None = None
    event_id: str = field(init=False, default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        spec = EVENT_TYPES.get(self.event_type)
        if spec is None:
            raise AuditSchemaError("unknown_event_type")

        if not isinstance(self.payload, Mapping):
            raise AuditSchemaError("invalid_payload_type")
        supplied = set(self.payload.keys())
        if any(type(key) is not str for key in supplied):
            raise AuditSchemaError("invalid_key_type")

        declared = set(spec.required) | set(spec.optional)
        for required_key in spec.required:
            if required_key not in supplied:
                raise AuditSchemaError("missing_required_key", required_key)
        for _key in supplied - declared:
            raise AuditSchemaError("extra_payload_key")

        for key, kind in {**spec.required, **spec.optional}.items():
            if key in supplied and not _matches_kind(self.payload[key], kind):
                raise AuditSchemaError("invalid_scalar_type", key)

        node = spec.node if self.node is None else self.node
        outcome = spec.outcome if self.outcome is None else self.outcome
        if node != spec.node:
            raise AuditSchemaError("invalid_node")
        if outcome != spec.outcome:
            raise AuditSchemaError("invalid_outcome")
        object.__setattr__(self, "node", node)
        object.__setattr__(self, "outcome", outcome)

        if self.duration_ms is not None:
            if type(self.duration_ms) is not int or self.duration_ms < 0:
                raise AuditSchemaError("invalid_duration_ms")

        for identifier in (self.session_id, self.requester_label):
            if identifier is None:
                continue
            if type(identifier) is not str:
                raise AuditSchemaError("invalid_identifier")
            if redact(identifier) != identifier:
                raise AuditSchemaError("secret_shaped_identifier")

        if self.occurred_at is None:
            occurred_at = datetime.now(UTC).isoformat()
        else:
            if type(self.occurred_at) is not str or not _UTC_ISO_RE.match(self.occurred_at):
                raise AuditSchemaError("invalid_occurred_at")
            try:
                datetime.fromisoformat(self.occurred_at)
            except ValueError:
                raise AuditSchemaError("invalid_occurred_at") from None
            occurred_at = self.occurred_at
        object.__setattr__(self, "occurred_at", occurred_at)

        # Validate that the payload is JSON-safe; redaction happens at write time.
        to_json(self.payload)
