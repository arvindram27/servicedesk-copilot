"""Constraint enforcement (B1 plan tests 10-17).

SQLAlchemy wraps DBAPI errors, so every violation is asserted as
``sqlalchemy.exc.IntegrityError``; the original ``sqlite3`` exception is checked
where it adds evidence.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError

from db_support import (
    CATEGORY_ID,
    REQUESTER_LABEL,
    TIMESTAMP,
    TOKEN_ID,
    migrated_connection,
    seed_basics,
)


def _insert_ticket(
    connection: Connection,
    *,
    ticket_id: str = "ticket-1",
    idempotency_key: str = "key-1",
    token_id: str = TOKEN_ID,
    priority: str = "normal",
) -> None:
    connection.exec_driver_sql(
        "INSERT INTO tickets (ticket_id, requester_label, title, description,"
        " category_id, priority, status, idempotency_key, confirmation_token_id, created_at)"
        " VALUES (?, ?, 'Printer', 'Broken', ?, ?, 'open', ?, ?, ?)",
        (
            ticket_id,
            REQUESTER_LABEL,
            CATEGORY_ID,
            priority,
            idempotency_key,
            token_id,
            TIMESTAMP,
        ),
    )


def _insert_key(
    connection: Connection,
    *,
    key: str = "key-1",
    status: str = "in_flight",
    completed_at: str | None = None,
    response_status: int | None = None,
    response_json: str | None = None,
) -> None:
    connection.exec_driver_sql(
        "INSERT INTO idempotency_keys (requester_label, idempotency_key,"
        " payload_fingerprint, confirmation_token_id, status, claimed_at,"
        " owner_run_id, owner_generation, completed_at, response_status, response_json)"
        " VALUES (?, ?, 'fp', ?, ?, ?, 'run-1', 1, ?, ?, ?)",
        (
            REQUESTER_LABEL,
            key,
            TOKEN_ID,
            status,
            TIMESTAMP,
            completed_at,
            response_status,
            response_json,
        ),
    )


def _insert_audit(connection: Connection, event_id: str = "evt-1") -> None:
    connection.exec_driver_sql(
        "INSERT INTO audit_events (event_id, occurred_at, event_type, payload_json)"
        " VALUES (?, ?, 'test', '{}')",
        (event_id, TIMESTAMP),
    )


def test_duplicate_requester_label_is_rejected(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "dup-user.db") as connection:
        seed_basics(connection)
        with pytest.raises(IntegrityError) as excinfo:
            connection.exec_driver_sql(
                "INSERT INTO users (user_id, requester_label, display_name, created_at)"
                " VALUES ('user-2', ?, 'Other', ?)",
                (REQUESTER_LABEL, TIMESTAMP),
            )
        assert isinstance(excinfo.value.orig, sqlite3.IntegrityError)
        connection.rollback()


def test_duplicate_idempotency_key_is_rejected(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "dup-key.db") as connection:
        seed_basics(connection)
        _insert_key(connection)
        with pytest.raises(IntegrityError):
            _insert_key(connection)
        connection.rollback()


def test_second_ticket_for_one_token_is_rejected(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "dup-token.db") as connection:
        seed_basics(connection)
        _insert_ticket(connection, ticket_id="ticket-1", idempotency_key="key-1")
        with pytest.raises(IntegrityError):
            _insert_ticket(connection, ticket_id="ticket-2", idempotency_key="key-2")
        connection.rollback()


def test_ticket_requires_an_existing_token(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "missing-token.db") as connection:
        seed_basics(connection)
        with pytest.raises(IntegrityError):
            _insert_ticket(connection, token_id="missing-token")
        connection.rollback()


def test_check_constraints_on_priority_and_status(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "checks.db") as connection:
        seed_basics(connection)
        with pytest.raises(IntegrityError):
            _insert_ticket(connection, priority="critical")
        connection.rollback()
        with pytest.raises(IntegrityError):
            _insert_key(connection, key="key-bad", status="pending")
        connection.rollback()


def test_completed_and_in_flight_field_matrix(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "matrix.db") as connection:
        seed_basics(connection)

        partial_cases = (
            {"completed_at": TIMESTAMP},
            {"response_status": 201},
            {"response_json": "{}"},
            {"completed_at": TIMESTAMP, "response_status": 201},
        )
        for index, partial in enumerate(partial_cases):
            with pytest.raises(IntegrityError):
                _insert_key(connection, key=f"key-partial-{index}", **partial)
            connection.rollback()

        with pytest.raises(IntegrityError):
            _insert_key(connection, key="key-completed-empty", status="completed")
        connection.rollback()

        _insert_key(connection, key="key-in-flight")
        _insert_key(
            connection,
            key="key-completed",
            status="completed",
            completed_at=TIMESTAMP,
            response_status=201,
            response_json="{}",
        )
        connection.commit()
        total = connection.exec_driver_sql("SELECT COUNT(*) FROM idempotency_keys").scalar()
        assert total == 2


def test_token_consumption_pairing_is_enforced(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "pairing.db") as connection:
        seed_basics(connection)
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "UPDATE confirmation_tokens SET consumed_at = ? WHERE token_id = ?",
                (TIMESTAMP, TOKEN_ID),
            )
        connection.rollback()
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "UPDATE confirmation_tokens SET ticket_id = 'ticket-1' WHERE token_id = ?",
                (TOKEN_ID,),
            )
        connection.rollback()


def test_audit_events_are_append_only(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "audit.db") as connection:
        _insert_audit(connection, "evt-1")
        connection.commit()

        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("UPDATE audit_events SET outcome = 'changed'")
        connection.rollback()
        outcome = connection.exec_driver_sql(
            "SELECT outcome FROM audit_events WHERE event_id = 'evt-1'"
        ).scalar()
        assert outcome is None

        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("DELETE FROM audit_events")
        connection.rollback()
        remaining = connection.exec_driver_sql("SELECT COUNT(*) FROM audit_events").scalar()
        assert remaining == 1


def test_audit_seq_is_strictly_increasing_and_event_id_unique(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "audit-seq.db") as connection:
        for event_id in ("evt-a", "evt-b", "evt-c"):
            _insert_audit(connection, event_id)
        connection.commit()

        seqs = connection.exec_driver_sql("SELECT seq FROM audit_events ORDER BY seq").scalars()
        values = list(seqs)
        assert len(values) == 3
        assert values[0] < values[1] < values[2]

        with pytest.raises(IntegrityError):
            _insert_audit(connection, "evt-a")
        connection.rollback()
