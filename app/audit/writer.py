"""Durable audit writer: one INSERT into ``audit_events`` (B2).

Transaction ownership is explicit:

- The caller begins the transaction and the caller commits. The writer never
  begins, commits or rolls back, and never opens its own connection.
- ``write_event`` refuses to run when no transaction is active on the supplied
  connection. Under SQLAlchemy autobegin this proves only that a transaction is
  active, not that the caller explicitly began one; explicit begin/commit
  discipline remains contractual and is not mechanically enforced.
- The transaction may span only the write and the caller's immediately adjacent
  work. Holding it across a network call, a model call or a long computation is
  forbidden by contract, not by code.

Failures: an INSERT failure is reported as ``AuditWriteError`` carrying only the
closed-registry event type and the exception class name. It retains no cause and
no context, so neither the SQL statement text nor bound parameter values can leak
through the exception or its formatted traceback. Nothing is retried, logged and
continued, or repaired in place.

Redaction is bounded: free-text payload strings are scrubbed with the known
pattern set from ``app.logging``. That is best-effort and is not proof that no
secret can be stored.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.audit.schema import MAX_PAYLOAD_DEPTH, AuditEvent, AuditSchemaError, to_json
from app.logging import redact

INSERT_SQL: Final[str] = (
    "INSERT INTO audit_events"
    " (event_id, occurred_at, event_type, session_id, requester_label,"
    " node, outcome, duration_ms, payload_json)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class AuditTransactionError(RuntimeError):
    """No active transaction on the caller's connection."""


class AuditWriteError(RuntimeError):
    """An INSERT failed. Only the event type and exception class are retained."""

    def __init__(self, event_type: str, error_class: str) -> None:
        self.event_type = event_type
        self.error_class = error_class
        super().__init__(f"audit write failed for {event_type}: {error_class}")


def redact_payload(value: object, *, depth: int = 0) -> object:
    """Redact every string value recursively; keys are contract-owned."""
    if depth > MAX_PAYLOAD_DEPTH:
        raise AuditSchemaError("payload_too_deep")
    if type(value) is str:
        return redact(value)
    if type(value) is dict:
        return {key: redact_payload(item, depth=depth + 1) for key, item in value.items()}
    if type(value) is list:
        return [redact_payload(item, depth=depth + 1) for item in value]
    return value


def write_event(connection: Connection, event: AuditEvent) -> str:
    """Insert one audit row in the caller's active transaction; return its id."""
    if not connection.in_transaction():
        raise AuditTransactionError(
            "no active transaction: the caller must begin one before writing"
        )

    payload_json = to_json(redact_payload(dict(event.payload)))
    parameters = (
        event.event_id,
        event.occurred_at,
        event.event_type,
        event.session_id,
        event.requester_label,
        event.node,
        event.outcome,
        event.duration_ms,
        payload_json,
    )

    failure_class: str | None = None
    try:
        connection.exec_driver_sql(INSERT_SQL, parameters)
    except (sqlite3.Error, SQLAlchemyError) as exc:
        failure_class = type(exc).__name__
    if failure_class is not None:
        # Raised after the except block, so no exception is active:
        # __cause__ and __context__ are both None.
        raise AuditWriteError(event.event_type, failure_class)

    return event.event_id
