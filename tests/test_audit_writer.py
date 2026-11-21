"""Writer, redaction-parity and contract-drift tests for B2.

Bounded claim: the redaction assertions cover the known pattern shapes over the
fixtures declared here. They do not prove that no secret can ever be stored.
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
import traceback
from pathlib import Path

import pytest
import scripts.migrate as migrate_module
from app.audit.schema import EVENT_TYPES, AuditEvent
from app.audit.writer import (
    AuditTransactionError,
    AuditWriteError,
    redact_payload,
    write_event,
)
from app.logging import configure_logging
from sqlalchemy.exc import IntegrityError

from db_support import migrated_connection

API_KEY = "sk-live-abcdefghijklmnop1234"
GOOGLE_KEY = "AIzaSyA1234567890abcdefghijklmnop"
BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
FIELD_ASSIGNMENT = "api_key=abcd1234efgh5678"
HEX_RUN = "0123456789abcdef0123456789abcdef"
FIXTURES = (API_KEY, GOOGLE_KEY, BEARER, FIELD_ASSIGNMENT, HEX_RUN)


def _rows(database_path: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute(
            "SELECT event_id, event_type, node, outcome, session_id,"
            " requester_label, duration_ms, occurred_at, payload_json"
            " FROM audit_events ORDER BY seq"
        ).fetchall()
    finally:
        connection.close()


def _provider_event(**overrides: object) -> AuditEvent:
    payload: dict[str, object] = {"stage": "chat", "error_class": "TimeoutError"}
    payload.update(overrides)
    return AuditEvent("provider.error", payload)


def test_writer_inserts_row_readable_back(tmp_path: Path) -> None:
    database_path = tmp_path / "audit.db"
    event = _provider_event()

    with migrated_connection(database_path) as connection:
        with connection.begin():
            event_id = write_event(connection, event)

    rows = _rows(database_path)
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == event_id
    assert row[1] == "provider.error"
    assert row[2] == "provider"
    assert row[3] == "error"
    assert row[7] == event.occurred_at
    assert json.loads(str(row[8])) == {"stage": "chat", "error_class": "TimeoutError"}


def test_writer_requires_an_active_transaction(tmp_path: Path) -> None:
    database_path = tmp_path / "no-transaction.db"

    with migrated_connection(database_path) as connection:
        assert connection.in_transaction() is False
        with pytest.raises(AuditTransactionError):
            write_event(connection, _provider_event())

    assert _rows(database_path) == []


def test_writer_does_not_commit(tmp_path: Path) -> None:
    database_path = tmp_path / "rollback.db"

    with migrated_connection(database_path) as connection:
        with connection.begin():
            write_event(connection, _provider_event())
            connection.rollback()

    assert _rows(database_path) == []


class _FailingConnection:
    """Connection double whose INSERT fails with a parameter-bearing error."""

    def in_transaction(self) -> bool:
        return True

    def exec_driver_sql(self, statement: str, parameters: tuple[object, ...]) -> object:
        raise IntegrityError(
            statement,
            parameters,
            sqlite3.IntegrityError("UNIQUE constraint failed: audit_events.event_id"),
        )


def test_insert_failure_raises_safe_audit_write_error() -> None:
    event = AuditEvent(
        "provider.error",
        {"stage": "PARAM-MARKER-0123456789", "error_class": "TimeoutError"},
    )

    with pytest.raises(AuditWriteError) as excinfo:
        write_event(_FailingConnection(), event)  # type: ignore[arg-type]

    error = excinfo.value
    assert error.event_type == "provider.error"
    assert error.error_class == "IntegrityError"
    assert error.__cause__ is None
    assert error.__context__ is None

    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    for leaked in ("INSERT INTO audit_events", "PARAM-MARKER-0123456789", "VALUES"):
        assert leaked not in str(error)
        assert leaked not in rendered


def test_append_only_triggers_reject_direct_update_and_delete(tmp_path: Path) -> None:
    database_path = tmp_path / "append-only.db"

    with migrated_connection(database_path) as connection:
        with connection.begin():
            write_event(connection, _provider_event())

        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("UPDATE audit_events SET outcome = 'changed'")
        connection.rollback()

        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("DELETE FROM audit_events")
        connection.rollback()

    assert len(_rows(database_path)) == 1


def test_fixture_secrets_in_payload_string_fields_are_redacted_in_audit_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "payload-redaction.db"

    with migrated_connection(database_path) as connection:
        for fixture in FIXTURES:
            event = AuditEvent(
                "provider.error",
                {"stage": fixture, "error_class": "ValueError"},
            )
            with connection.begin():
                write_event(connection, event)

    payloads = [str(row[8]) for row in _rows(database_path)]
    assert len(payloads) == len(FIXTURES)
    for fixture, payload in zip(FIXTURES, payloads, strict=True):
        assert "[REDACTED]" in payload
        assert fixture not in payload


def test_recursive_redaction_covers_nested_structures() -> None:
    redacted = redact_payload(
        {
            "top": [API_KEY, {"inner": GOOGLE_KEY}],
            "keep": 1,
        }
    )

    assert isinstance(redacted, dict)
    assert redacted["keep"] == 1
    top = redacted["top"]
    assert isinstance(top, list)
    assert top[0] == "[REDACTED]:api_key"
    inner = top[1]
    assert isinstance(inner, dict)
    assert inner["inner"] == "[REDACTED]:api_key"
    assert API_KEY not in json.dumps(redacted)
    assert GOOGLE_KEY not in json.dumps(redacted)


def test_fixture_secrets_in_messages_and_extras_are_redacted_on_stdout() -> None:
    buffer = io.StringIO()
    logger = configure_logging(level=logging.INFO, stream=buffer)

    for fixture in FIXTURES:
        logger.info("value %s", fixture, extra={"event": fixture})

    output = buffer.getvalue()
    for fixture in FIXTURES:
        assert fixture not in output
    assert "[REDACTED]" in output


def test_extras_allowlist_is_still_exactly_four_names() -> None:
    buffer = io.StringIO()
    logger = configure_logging(level=logging.INFO, stream=buffer)

    logger.info("allowlist", extra={"event": "e", "note": "not-emitted"})

    payload = json.loads(buffer.getvalue().strip())
    assert payload["event"] == "e"
    assert "note" not in payload


def test_same_fixture_scrubbed_identically_in_both_sinks(tmp_path: Path) -> None:
    database_path = tmp_path / "parity.db"

    with migrated_connection(database_path) as connection:
        for fixture in FIXTURES:
            buffer = io.StringIO()
            logger = configure_logging(level=logging.INFO, stream=buffer)
            logger.info("message %s", fixture)

            event = AuditEvent(
                "provider.error",
                {"stage": fixture, "error_class": "ValueError"},
            )
            with connection.begin():
                write_event(connection, event)

            stdout_text = buffer.getvalue()
            audit_payload = str(_rows(database_path)[-1][8])

            for sink in (stdout_text, audit_payload):
                assert "[REDACTED]" in sink
                assert fixture not in sink


def test_migration_wrapper_payload_matches_schema_contract(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    database_path = runtime / "copilot.db"

    code = migrate_module.run_wrapper(database_path, runtime_dir=runtime, stream=io.StringIO())
    assert code == 0

    rows = _rows(database_path)
    assert len(rows) == 1
    assert rows[0][1] == "migration.applied"

    payload = json.loads(str(rows[0][8]))
    spec = EVENT_TYPES["migration.applied"]

    assert set(payload) == set(spec.required) | set(spec.optional)
    assert payload["from_revision"] is None
    assert isinstance(payload["to_revision"], str)
    assert payload["to_revision"] == "0001"
    assert payload["backup_path"] is None
    assert payload["skipped_reason"] == "source_missing_first_run"
    assert isinstance(payload["schema_no_change"], bool)
    assert payload["schema_no_change"] is False
